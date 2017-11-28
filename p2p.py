import socketio
import eventlet
import eventlet.wsgi
from pymongo import MongoClient
from flask import Flask, render_template
from blockchainutils import BU


mongo_client = MongoClient()
db = mongo_client.yadacointest
BU.collection = db.blocks
sio = socketio.Server()
app = Flask(__name__)

@app.route('/')
def index():
    """Serve the client-side application."""
    return render_template('index.html')

@sio.on('connect', namespace='/chat')
def connect(sid, environ):
    print("connect ", sid)

@sio.on('new block', namespace='/chat')
def newblock(sid, data):
    print("new block ", data)
    sio.emit('reply', room=sid, namespace='/chat')

@sio.on('getblocks', namespace='/chat')
def getblocks(sid):
    print("getblocks ")
    sio.emit('getblocksreply', data=[x for x in BU.get_blocks()], room=sid, namespace='/chat')

@sio.on('disconnect', namespace='/chat')
def disconnect(sid):
    print('disconnect ', sid)

if __name__ == '__main__':
    # wrap Flask application with engineio's middleware
    app = socketio.Middleware(sio, app)

    # deploy as an eventlet WSGI server
    eventlet.wsgi.server(eventlet.listen(('', 8000)), app)