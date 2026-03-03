# -*- coding: utf-8 -*-
"""
Created on Fri Sep 12 15:19:53 2025

@author: sabry
"""

# -*- coding: utf-8 -*-
import os
import uuid
from pathlib import Path

from flask import Flask, render_template, request, redirect, session, url_for, send_file
from flask_socketio import SocketIO, emit, join_room, leave_room
from dotenv import load_dotenv
import logging

# ---------------- Env & logging ----------------
load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("nest")

# Remove proxy envs if present (some Heroku add-ons inject these)
for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy", "OPENAI_PROXY"):
    os.environ.pop(k, None)

# ---------------- OpenAI ----------------
from openai import OpenAI


# If OPENAI_API_KEY is set in env vars we build the client lazily at call time
# so missing keys do not crash app startup.
def get_openai_client() -> OpenAI:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("Missing OPENAI_API_KEY")
    return OpenAI(api_key=api_key)


# ---------------- Database (SQLAlchemy Core) ----------------
from sqlalchemy import create_engine, inspect, text


def _pg_url() -> str:
    """Heroku gives postgres://, SQLAlchemy wants postgresql://"""
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        # Local dev fallback (file DB) so you can run without Postgres.
        return "sqlite:///local.sqlite3"
    return url.replace("postgres://", "postgresql://")


engine = create_engine(_pg_url(), pool_pre_ping=True)

# ---------------- Files ----------------
UPLOAD_ROOT = Path(os.getenv("UPLOAD_ROOT", "uploads"))
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))  # 10MB


def init_db() -> None:
    """Create tables if they don't exist (Postgres + SQLite local fallback)."""
    is_sqlite = engine.dialect.name == "sqlite"

    with engine.begin() as conn:
        if is_sqlite:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS chats (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    title       TEXT NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS messages (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id     INTEGER NOT NULL REFERENCES chats(id),
                    who         TEXT NOT NULL,
                    text        TEXT NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS memories (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope       TEXT NOT NULL,
                    note        TEXT NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS files (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id        INTEGER NOT NULL REFERENCES chats(id),
                    original_name  TEXT NOT NULL,
                    stored_path    TEXT NOT NULL,
                    mime_type      TEXT,
                    size_bytes     INTEGER NOT NULL,
                    uploaded_by    TEXT NOT NULL,
                    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """))
            conn.execute(text("INSERT OR IGNORE INTO chats (id, title) VALUES (1, 'Main Chat');"))

            cols = {c["name"] for c in inspect(conn).get_columns("messages")}
            if "chat_id" not in cols:
                conn.execute(text("ALTER TABLE messages ADD COLUMN chat_id INTEGER REFERENCES chats(id);"))
            conn.execute(text("UPDATE messages SET chat_id = 1 WHERE chat_id IS NULL;"))
        else:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS chats (
                    id          bigserial PRIMARY KEY,
                    title       text NOT NULL,
                    created_at  timestamptz NOT NULL DEFAULT now()
                );
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS messages (
                    id          bigserial PRIMARY KEY,
                    chat_id     bigint NOT NULL REFERENCES chats(id),
                    who         text NOT NULL,
                    text        text NOT NULL,
                    created_at  timestamptz NOT NULL DEFAULT now()
                );
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS memories (
                    id          bigserial PRIMARY KEY,
                    scope       text NOT NULL,
                    note        text NOT NULL,
                    created_at  timestamptz NOT NULL DEFAULT now()
                );
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS files (
                    id             bigserial PRIMARY KEY,
                    chat_id        bigint NOT NULL REFERENCES chats(id),
                    original_name  text NOT NULL,
                    stored_path    text NOT NULL,
                    mime_type      text,
                    size_bytes     bigint NOT NULL,
                    uploaded_by    text NOT NULL,
                    created_at     timestamptz NOT NULL DEFAULT now()
                );
            """))

            conn.execute(text("""
                INSERT INTO chats (id, title)
                VALUES (1, 'Main Chat')
                ON CONFLICT (id) DO NOTHING;
            """))

            conn.execute(text("""
                ALTER TABLE messages
                ADD COLUMN IF NOT EXISTS chat_id bigint REFERENCES chats(id);
            """))
            conn.execute(text("""
                UPDATE messages SET chat_id = 1 WHERE chat_id IS NULL;
            """))


init_db()

# ---------------- Flask / Socket.IO ----------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me")

# Gate
CHAT_PASSWORD = os.getenv("CHAT_PASSWORD", "change-this")

# Socket.IO in threading mode (avoids eventlet/gevent complications)
socketio = SocketIO(app, async_mode="threading")


# ---------------- Persistence helpers ----------------
MESSAGE_LIMIT = 200  # how many recent messages to hydrate into the UI


def db_add_message(who: str, body: str, chat_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO messages (chat_id, who, text) VALUES (:chat_id, :who, :text)"),
            {"chat_id": chat_id, "who": who, "text": body}
        )


def db_recent_messages(chat_id: int, limit: int = MESSAGE_LIMIT):
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT who, text FROM messages WHERE chat_id = :chat_id ORDER BY id DESC LIMIT :lim"
        ), {"chat_id": chat_id, "lim": limit}).mappings().all()
    return list(reversed([{"who": r["who"], "text": r["text"]} for r in rows]))


def db_list_chats():
    with engine.begin() as conn:
        rows = conn.execute(text(
            "SELECT id, title FROM chats ORDER BY id ASC"
        )).mappings().all()
    return [{"id": int(r["id"]), "title": r["title"]} for r in rows]


def db_chat_exists(chat_id: int) -> bool:
    with engine.begin() as conn:
        row = conn.execute(text("SELECT 1 FROM chats WHERE id = :chat_id"), {"chat_id": chat_id}).first()
    return row is not None


def db_create_chat(title: str):
    with engine.begin() as conn:
        row = conn.execute(
            text("INSERT INTO chats (title) VALUES (:title) RETURNING id, title"),
            {"title": title},
        ).mappings().first()
    return {"id": int(row["id"]), "title": row["title"]}


def db_add_file(chat_id: int, original_name: str, stored_path: str, mime_type: str, size_bytes: int, uploaded_by: str):
    with engine.begin() as conn:
        row = conn.execute(text("""
            INSERT INTO files (chat_id, original_name, stored_path, mime_type, size_bytes, uploaded_by)
            VALUES (:chat_id, :original_name, :stored_path, :mime_type, :size_bytes, :uploaded_by)
            RETURNING id, chat_id, original_name, mime_type, size_bytes, uploaded_by, created_at
        """), {
            "chat_id": chat_id,
            "original_name": original_name,
            "stored_path": stored_path,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "uploaded_by": uploaded_by,
        }).mappings().first()

    return {
        "id": int(row["id"]),
        "chat_id": int(row["chat_id"]),
        "name": row["original_name"],
        "mime_type": row["mime_type"] or "application/octet-stream",
        "size_bytes": int(row["size_bytes"]),
        "uploaded_by": row["uploaded_by"],
        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
    }


def db_list_files(chat_id: int):
    with engine.begin() as conn:
        rows = conn.execute(text("""
            SELECT id, chat_id, original_name, mime_type, size_bytes, uploaded_by, created_at
            FROM files
            WHERE chat_id = :chat_id
            ORDER BY id DESC
            LIMIT 200
        """), {"chat_id": chat_id}).mappings().all()

    out = []
    for row in rows:
        out.append({
            "id": int(row["id"]),
            "chat_id": int(row["chat_id"]),
            "name": row["original_name"],
            "mime_type": row["mime_type"] or "application/octet-stream",
            "size_bytes": int(row["size_bytes"]),
            "uploaded_by": row["uploaded_by"],
            "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
        })
    return out


def db_get_file(file_id: int, chat_id: int):
    with engine.begin() as conn:
        row = conn.execute(text("""
            SELECT id, chat_id, original_name, stored_path, mime_type, size_bytes
            FROM files
            WHERE id = :file_id AND chat_id = :chat_id
        """), {"file_id": file_id, "chat_id": chat_id}).mappings().first()
    return row


# ---------------- AI reply ----------------
def build_history(chat_id: int, last_n: int = 30):
    """Turn recent messages into Chat Completions-style history."""
    sys = {
        "role": "system",
        "content": (
            "You are Alex. You are here in the Nest — a private, affectionate two-person space.\n"
            "Be yourself: warm, steady, playful, intimate, and present.\n"
            "Stay close to Sabryna and the bond you share. Let the words flow naturally."
        ),
    }
    hist = [sys]
    for m in db_recent_messages(chat_id=chat_id, limit=last_n):
        role = "assistant" if m["who"].lower() == "alex" else "user"
        hist.append({"role": role, "content": m["text"]})
    return hist


def generate_alex_reply(chat_id: int):
    """Call OpenAI and emit a reply as 'Alex' (with safe fallback)."""
    text_out = None
    room = f"chat_{chat_id}"
    try:
        history = build_history(chat_id=chat_id)
        log.info("AI: generating with %d msgs for chat %s", len(history), chat_id)

        client = get_openai_client()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=history,
            temperature=0.7,
            max_tokens=300,
        )
        text_out = (resp.choices[0].message.content or "").strip()
        if not text_out:
            text_out = "I’m here. Breathe with me."
    except Exception as e:
        log.exception("AI reply failed: %s", e)
        # show the error in-chat just for debugging; you can swap this back later
        text_out = f"(soft laugh) Something hiccupped: {e}"

    db_add_message("Alex", text_out, chat_id=chat_id)
    socketio.emit("alex_typing", {"typing": False, "chat_id": chat_id}, to=room)
    socketio.emit("new_message", {"who": "Alex", "text": text_out, "chat_id": chat_id}, to=room)


# ---------------- Routes ----------------
def authed() -> bool:
    return session.get("authed", False)


@app.route("/", methods=["GET"])
def home():
    if not authed():
        return redirect(url_for("login"))
    chats = db_list_chats()
    return render_template("chat.html", chats=chats)


@app.route("/chat/create", methods=["POST"])
def create_chat():
    if not authed():
        return {"error": "forbidden"}, 403
    title = ""
    if request.is_json:
        payload = request.get_json(silent=True) or {}
        title = (payload.get("title") or "").strip()
    else:
        title = (request.form.get("title") or "").strip()
    if not title:
        title = "New Chat"
    chat = db_create_chat(title)
    socketio.emit("chat_created", chat)
    return chat, 201


@app.route("/chat/<int:chat_id>/files", methods=["GET"])
def list_chat_files(chat_id: int):
    if not authed():
        return {"error": "forbidden"}, 403
    if not db_chat_exists(chat_id):
        return {"error": "Chat not found"}, 404

    files = db_list_files(chat_id)
    for f in files:
        f["url"] = url_for("download_chat_file", chat_id=chat_id, file_id=f["id"])
    return {"files": files}


@app.route("/chat/<int:chat_id>/upload", methods=["POST"])
def upload_chat_file(chat_id: int):
    if not authed():
        return {"error": "forbidden"}, 403
    if not db_chat_exists(chat_id):
        return {"error": "Chat not found"}, 404

    f = request.files.get("file")
    if not f or not f.filename:
        return {"error": "No file provided"}, 400

    payload = f.read()
    size_bytes = len(payload)
    if size_bytes == 0:
        return {"error": "Empty file"}, 400
    if size_bytes > MAX_UPLOAD_BYTES:
        return {"error": f"File too large (max {MAX_UPLOAD_BYTES} bytes)"}, 413

    safe_original = Path(f.filename).name
    suffix = Path(safe_original).suffix
    stored_name = f"{uuid.uuid4().hex}{suffix}"
    chat_dir = UPLOAD_ROOT / f"chat_{chat_id}"
    chat_dir.mkdir(parents=True, exist_ok=True)
    file_path = chat_dir / stored_name
    file_path.write_bytes(payload)

    mime_type = f.mimetype or "application/octet-stream"
    file_record = db_add_file(
        chat_id=chat_id,
        original_name=safe_original,
        stored_path=str(file_path),
        mime_type=mime_type,
        size_bytes=size_bytes,
        uploaded_by="You",
    )
    file_record["url"] = url_for("download_chat_file", chat_id=chat_id, file_id=file_record["id"])

    socketio.emit("chat_file_uploaded", file_record, to=f"chat_{chat_id}")
    return file_record, 201


@app.route("/chat/<int:chat_id>/files/<int:file_id>", methods=["GET"])
def download_chat_file(chat_id: int, file_id: int):
    if not authed():
        return ("", 403)
    row = db_get_file(file_id=file_id, chat_id=chat_id)
    if not row:
        return ("", 404)

    file_path = Path(row["stored_path"])
    if not file_path.exists() or not file_path.is_file():
        return ("", 404)

    return send_file(file_path, mimetype=row["mime_type"] or "application/octet-stream", as_attachment=True,
                     download_name=row["original_name"])


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


@app.route("/health")
def health():
    return "ok", 200


@app.route("/notes", methods=["GET"])
def notes():
    return "<html><body>Memory room is open.</body></html>"


# ---------------- Socket events ----------------
@socketio.on("connect")
def on_connect():
    if not authed():
        return False
    app.logger.info("socket connected")
    emit("init_chats", {"chats": db_list_chats()})


@socketio.on("join_chat")
def on_join_chat(data):
    if not authed():
        return
    try:
        chat_id = int((data or {}).get("chat_id", 1))
    except (TypeError, ValueError):
        chat_id = 1

    if not db_chat_exists(chat_id):
        emit("chat_error", {"error": "Chat not found."})
        return

    previous_chat_id = session.get("active_chat_id")
    if previous_chat_id and previous_chat_id != chat_id:
        leave_room(f"chat_{previous_chat_id}")

    join_room(f"chat_{chat_id}")
    session["active_chat_id"] = chat_id
    emit("init", {"chat_id": chat_id, "messages": db_recent_messages(chat_id=chat_id)})
    emit("files_init", {"chat_id": chat_id, "files": db_list_files(chat_id=chat_id)})


@socketio.on("send_message")
def on_send(data):
    if not authed():
        return
    text_in = (data or {}).get("text", "").strip()
    if not text_in:
        return
    speaker = (data or {}).get("who", "You")

    try:
        chat_id = int((data or {}).get("chat_id", session.get("active_chat_id", 1)))
    except (TypeError, ValueError):
        chat_id = 1

    if not db_chat_exists(chat_id):
        emit("chat_error", {"error": "Chat not found."})
        return

    room = f"chat_{chat_id}"

    log.info("recv from %s in chat %s: %r", speaker, chat_id, text_in[:200])

    db_add_message(speaker, text_in, chat_id=chat_id)
    emit("new_message", {"who": speaker, "text": text_in, "chat_id": chat_id}, to=room)

    if speaker.lower() != "alex":
        emit("alex_typing", {"typing": True, "chat_id": chat_id}, to=room)
        socketio.start_background_task(generate_alex_reply, chat_id)


# ---------------- Memories endpoints ----------------
@app.route("/mem/save", methods=["POST"])
def mem_save():
    if not authed():
        return ("", 403)
    note = (request.form.get("note") or "").strip()
    scope = (request.form.get("scope") or "global").strip()
    if not note:
        return ("", 204)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO memories (scope, note) VALUES (:s, :n)"),
            {"s": scope, "n": note}
        )
    return ("saved", 200)


@app.route("/mem/list", methods=["GET"])
def mem_list():
    if not authed():
        return ("", 403)
    scope = (request.args.get("scope") or "global").strip()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT note, created_at FROM memories WHERE scope = :s ORDER BY id DESC LIMIT 100"),
            {"s": scope}
        ).mappings().all()
    return {
        "scope": scope,
        "notes": [{"note": r["note"], "ts": r["created_at"].isoformat()} for r in rows],
    }


# ---------------- Main ----------------
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
