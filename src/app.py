from flask import Flask
from flask_restful import Resource, Api
import datetime
import socket

app = Flask(__name__)

api = Api(app)

class ReturnDetails(Resource):
    def get(self):
        data = {
            "time": datetime.datetime.now().strftime("%I:%M:%S%p on %B %d, %Y"),
            "hostname": socket.gethostname(),
            "message": "You are welcome to the python-app!!"
        }
        return data

api.add_resource(ReturnDetails, '/api/v1/details')

class ReturnHealth(Resource):
    def get(self):
        data = {
            "status": "up"
        }
        return data, 200

api.add_resource(ReturnHealth, '/api/v1/healthz')

# @app.route('/api/v1/details', methods=['GET'])

# def details():
#     if request.method == 'GET':
#         data = {"message":"Hello World"}
#     return jsonify(data)

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)


# '/api/v1/details'
# '/api/v1/healthz'