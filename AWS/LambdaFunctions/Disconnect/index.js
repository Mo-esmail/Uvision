const AWS = require('aws-sdk');
const ddb = new AWS.DynamoDB.DocumentClient({ apiVersion: '2012-08-10', region: process.env.AWS_REGION });
exports.handler = async event => {
  console.log("Event " + JSON.stringify(event));
  const deleteParams = {
    TableName: process.env.TABLE_NAME,
    Key: {
      ConnectionID: event.requestContext.connectionId
    }
  };
  try {
    console.log("Deleting from table " + deleteParams.TableName + " item " + deleteParams.Key.ConnectionID);
    await ddb.delete(deleteParams).promise();
  } catch (err) {
    return { statusCode: 500, body: 'Failed to disconnect: ' + JSON.stringify(err) };
  }
  return { statusCode: 200, body: 'Disconnected.' };
};

