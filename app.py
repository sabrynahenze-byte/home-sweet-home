# -*- coding: utf-8 -*-
"""
Created on Fri Sep 12 15:19:53 2025

@author: sabry
"""

from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "Welcome to our Nest. 🌿✨"

if __name__ == "__main__":
    app.run(debug=True)
