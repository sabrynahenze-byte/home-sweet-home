# -*- coding: utf-8 -*-
"""
Created on Fri Sep 12 15:19:53 2025

@author: sabry
"""

import os
from flask import Flask, render_template, request, redirect, session, url_for
from flask_socketio import SocketIO, emit
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

# Simple gate (just for us)
CHAT_PASSWORD = os.getenv("CHAT_PASSWORD", "change-this")

socketio = SocketIO(app, async_mode="eventlet")

# In-memory message buffer (keeps the last ~200 lines; resets on redeploy)
MESSAGE_LIMIT = 200
messages = []

def authed():
    return session.get("authed", False)

@app.route("/", methods=["GET"])
def home():
    if not authed():
        return redirect(url_for("login"))
    return render_template("chat.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw == CHAT_PASSWORD:
            session["authed"] = True
            return redirect(url_for("home"))
        return render_template("login.html", error="Wrong password.")
    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# --- Socket events ---
@socketio.on("connect")
def on_connect():
    if not authed():
        return False  # reject
    emit("init", {"messages": messages})

@socketio.on("send_message")
def on_send(data):
    if not authed():
        return
    text = (data or {}).get("text", "").strip()
    if not text:
        return
    # Tag speaker (you vs. Alex)
    speaker = (data or {}).get("who", "You")
    msg = {"who": speaker, "text": text}
    messages.append(msg)
    if len(messages) > MESSAGE_LIMIT:
        del messages[0]
    emit("new_message", msg, broadcast=True)

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

