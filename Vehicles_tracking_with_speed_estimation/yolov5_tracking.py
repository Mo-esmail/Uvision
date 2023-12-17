
import torch
import math
import time
import sys
sys.path.insert(0, './yolov5')

import torch.backends.cudnn as cudnn

import cv2
from yolov5.utils.datasets import LoadImages, LoadStreams
from yolov5.utils.general import  check_img_size
from yolov5.models.common import DetectMultiBackend
from yolov5.utils.general import (non_max_suppression, scale_coords, xyxy2xywh)
from yolov5.utils.torch_utils import select_device
from yolov5.utils.plots import Annotator, colors
from deep_sort.utils.parser import get_config
from deep_sort.deep_sort import DeepSort



class Tracker():
    def __init__(self, pth_yolo_model, model_attr, classes, aws_connector):

        self.aws_connector = aws_connector
        # Model attributes
        (self.frame_size, self.conf_thres, self.iou_thres, self.device, self.max_detections, self.half, self.livecam) = model_attr
        self.classes = classes

        # initialize detection model
        self.device = select_device(self.device)
        self.model = DetectMultiBackend(pth_yolo_model, device=self.device)
        self.stride, self.names, self.pt = self.model.stride, self.model.names, self.model.pt

        # initialize deepsort
        pth_deepsort_cfg = "deep_sort/configs/deep_sort.yaml"
        pth_deep_sort_model = "osnet_x0_25"
        cfg = get_config()
        cfg.merge_from_file(pth_deepsort_cfg)
        self.deepsort = DeepSort(pth_deep_sort_model, max_dist=cfg.DEEPSORT.MAX_DIST,
                                 max_iou_distance=cfg.DEEPSORT.MAX_IOU_DISTANCE, max_age=cfg.DEEPSORT.MAX_AGE,
                                 n_init=cfg.DEEPSORT.N_INIT, nn_budget=cfg.DEEPSORT.NN_BUDGET,
                                 use_cuda=False)

        # speed estimation
        self.speed = {}
        self.t = {}
        self.t1 = {}
        self.t2 = {}

        # half precision only supported by PyTorch on CUDA
        self.half &= self.pt and self.device.type != 'cpu'
        if self.pt:
            self.model.model.half() if self.half else self.model.model.float()

        if self.pt and self.device.type != 'cpu':
            self.model(torch.zeros(1, 3, *self.frame_size).to(self.device).type_as(next(self.model.model.parameters())))

            # Get names and colors for different classes
        self.names = self.model.module.names if hasattr(self.model, 'module') else self.model.names

    def track(self, dataset, thread):

        self.window = thread.window
        self.thread = thread
        self.source = self.window.src
        self.new_video = True
        self.save_vid = thread.window.save_vid

        start = time.time()

        for num_frames, (_, frame, frames_src, vid_cap, _) in enumerate(dataset):

            if self.new_video and self.save_vid:
                if vid_cap:  # video
                    fps = vid_cap.get(cv2.CAP_PROP_FPS)
                    w = int(vid_cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(vid_cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                else:  # stream
                    fps, h, w = 25, len(frames_src[0]), len(frames_src[0][0])

                self.vid_writer = cv2.VideoWriter("output/out.avi", cv2.VideoWriter_fourcc(*'XVID'), fps, (w, h))
                self.new_video = False

            # Preprocess the input
            frame = self.preprocess_input(frame)

            # Inference using yolov5
            detections = self.model(frame, augment=False)

            # Apply NMS
            detections = non_max_suppression(detections, self.conf_thres, self.iou_thres, self.classes,
                                             max_det=self.max_detections)

            # postprocess the output

            self.postprocess_out(frames_src, frame, detections)
            

            # calc. FPS
            if num_frames % 10 == 0 and num_frames != 0:

                time_elapsed = time.time() - start

                self.fps = math.ceil(10 / time_elapsed)

                start = time.time()

                avg_speed = 0
                # calc. avg speed
                if len(self.speed.keys())>0:

                    speeds_sum = sum(self.speed.values())
                    speed_num = len(self.speed.keys())
                    avg_speed = int(speeds_sum / speed_num)

                # update GUI
                self.thread.update_avg_speed.emit(str(avg_speed))
                self.thread.update_num_vehicles.emit(str(self.num_vechiles))
                self.thread.update_fps.emit(str(self.fps))

                # send to cloud
                if self.window.flag_snd2aws:
                    self.aws_connector.send_message(self.num_vechiles, avg_speed)


    def preprocess_input(self, frame):

        # Preprocess the input
        frame = torch.from_numpy(frame).to(self.device)
        frame = frame.half() if self.half else frame.float()  # uint8 to fp16/32
        frame /= 255.0  # 0 - 255 to 0.0 - 1.0
        if frame.ndimension() == 3:
            frame = frame.unsqueeze(0)
        return frame

    def postprocess_out(self, frames_src, frame, detections):

        for i, det in enumerate(detections):

            if self.livecam:  # Livecam >> batch_size >= 1
                frame_src = frames_src[i].copy()
            else:
                frame_src = frames_src.copy()


            if det is not None and len(det):
                # Rescale boxes to source image size
                det[:, :4] = scale_coords(frame.shape[2:], det[:, :4], frame_src.shape).round()

                # pass detections to deepsort
                deepsort_outputs = self.update(det, frame_src)

                cv2.line(frame_src, (0, 176), (frame_src.shape[1], 176), (0, 255, 0), 1)
                cv2.line(frame_src, (0, 196), (frame_src.shape[1], 196), (0, 255, 0), 1)

                cv2.line(frame_src, (0, 256), (frame_src.shape[1], 256), (0, 255, 0), 1)
                cv2.line(frame_src, (0, 276), (frame_src.shape[1], 276), (0, 255, 0), 1)

                # remove speed history
                for id in list(self.speed.keys()):
                    if id not in deepsort_outputs[:, 4]:
                        self.speed.pop(id)

                self.annotate(frame_src, deepsort_outputs)

                if self.save_vid:
                    self.vid_writer.write(frame_src)

                # send frame to GUI
                self.thread.update_stream.emit(frame_src)

            else:
                self.deepsort.increment_ages()




    def update(self, det, frame_src):
        # separate bboxes and confdences and classes
        xywh = xyxy2xywh(det[:, 0:4])
        confs = det[:, 4]
        clss = det[:, 5]

        # pass detections to deepsort
        return self.deepsort.update(xywh.cpu(), confs.cpu(), clss.cpu(), frame_src)


    def annotate(self, frame_src, outputs):
        annotator = Annotator(frame_src, line_width=2, pil=not ascii)
        self.num_vechiles = 0

        # draw boxes for visualization
        if len(outputs) > 0:

            for output in outputs:

                bboxes = output[0:4]
                id = output[4]
                cls = output[5]

                #cx = (bboxes[0] + bboxes[2]) // 2
                cy = (bboxes[1] + bboxes[3]) // 2

                ###
                if (cy >= 256 and cy <= 276):
                    self.t1[id] = time.time()

                if (cy >= 176 and cy <= 196):
                    self.t2[id] = time.time()

                if id in self.t1.keys() and id in self.t2.keys():
                    self.t[id] = self.t2[id] - self.t1[id]
                    self.t1.pop(id)
                    self.t2.pop(id)


                # vechiles counter
                self.num_vechiles += 1
                c = int(cls)  # integer class


                # Update vechile speed
                if id in self.t.keys() and id not in self.speed.keys():
                    self.speed[id] = (4.5 * self.fps * 3600) / (self.t[id] * 1000)
                    self.t.pop(id)

                # Annotate speed
                if id in self.speed.keys():
                    label = f'{self.names[c]} ID.{id} {int(self.speed[id])}km/hr'
                    annotator.box_label(bboxes, label, color=colors(c, True))
                else:
                    label = f'{self.names[c]}-{id}'
                    annotator.box_label(bboxes, label, color=colors(c, True))

        return annotator


def yolov5_mot(thread):
    
    # files paths
    pth_yolo_model = "yolov5s.pt"
    window = thread.window
    pth_source = window.src
    


    #classes to bet tracked 
    classes = [0,1,2,3,5,6,7] # person,bicycle,car,motorbike,bus,truck
    # model attributes
    device = thread.window.device
    webcam= pth_source=='0' or pth_source.startswith( 'rtsp') or pth_source.startswith('http') or pth_source.endswith('.txt')
    conf_thres = thread.window.conf_thres
    iou_thres = thread.window.iou_thres
    max_detections = 100 # maximum detections per image
    half = True
    frame_size = [320]
    frame_size *= 2 if len(frame_size) == 1 else 1

    # aggregate attributes to send to tracker class
    model_attr = (frame_size, conf_thres, iou_thres, device, max_detections, half, webcam)

    tracker = Tracker(pth_yolo_model,model_attr,classes,thread.window.aws_connector)

    # check new size ration
    frame_size = check_img_size(frame_size, s=tracker.stride)  

    # Dataloader
    if webcam:
        cudnn.benchmark = True  # set True to speed up constant image size inference
        dataset = LoadStreams(pth_source, img_size=frame_size, stride=tracker.stride)
    else:
        dataset = LoadImages(pth_source, img_size=frame_size, stride=tracker.stride)


    tracker.track(dataset, thread)
   
