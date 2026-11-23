from flask import Flask

from flaskapp import auth

app = Flask(__name__)
app.register_blueprint(auth)
