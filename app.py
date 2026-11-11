import os
import math
import secrets
import string
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template, request, jsonify, session
from flask_socketio import SocketIO, join_room, leave_room
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "replace-this-secret-before-deployment")
app.config["GEMINI_API_KEY"] = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///typeflow.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "0") == "1"
db = SQLAlchemy(app)
local_socket_origins = ["http://localhost:5000", "http://127.0.0.1:5000"]
configured_socket_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv("SOCKETIO_CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
socketio = SocketIO(
    app,
    async_mode="threading",
    cors_allowed_origins=list(dict.fromkeys(local_socket_origins + configured_socket_origins)),
)

challenge_rooms = {}
challenge_sid_rooms = {}
challenge_lock = threading.RLock()
CHALLENGE_MAX_PLAYERS = 8

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class TypingResult(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    mode = db.Column(db.String(20), nullable=False)
    duration = db.Column(db.Integer, nullable=False)
    wpm = db.Column(db.Float, nullable=False)
    raw_wpm = db.Column(db.Float, nullable=False)
    accuracy = db.Column(db.Float, nullable=False)
    correct_chars = db.Column(db.Integer, default=0)
    incorrect_chars = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

def current_user():
    uid = session.get("user_id")
    return db.session.get(User, uid) if uid else None

def _log_gemini_failure(stage, model, error, api_key):
    """Log actionable provider details while ensuring credentials never enter logs."""
    detail = str(error or "").replace("\r", " ").replace("\n", " ")
    if api_key:
        detail = detail.replace(api_key, "[REDACTED]")
    detail = detail[:600] or "No provider error message was supplied."
    status = getattr(error, "code", None) or getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    status = status or getattr(response, "status_code", None)
    app.logger.error(
        "Gemini coach %s failed: model=%s exception=%s status=%s detail=%s",
        stage,
        model,
        type(error).__name__,
        status if status is not None else "unknown",
        detail,
    )

def _gemini_finish_reason(response):
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "unknown"
    reason = getattr(candidates[0], "finish_reason", None)
    return getattr(reason, "name", None) or str(reason or "unknown")

def _gemini_response_is_truncated(response):
    return "MAX_TOKEN" in _gemini_finish_reason(response).upper()

def _gemini_status_code(error):
    status = getattr(error, "code", None) or getattr(error, "status_code", None)
    response = getattr(error, "response", None)
    status = status or getattr(response, "status_code", None)
    try:
        return int(status)
    except (TypeError, ValueError):
        return None

def _challenge_name(value):
    name = "".join(char for char in str(value or "") if char.isprintable()).strip()
    return name[:24] or "Player"

def _challenge_settings(data):
    mode = str(data.get("mode", "time"))
    try:
        config_value = int(data.get("config_value", 30))
    except (TypeError, ValueError):
        return None
    allowed = {"time": {15, 30, 60, 120}, "words": {10, 25, 50, 100}, "text": {60}}
    if mode not in allowed or config_value not in allowed[mode]:
        return None
    raw_words = data.get("words")
    if not isinstance(raw_words, list) or len(raw_words) > 100:
        return None
    words = [str(word).strip()[:32] for word in raw_words if str(word).strip()]
    expected_count = config_value if mode == "words" else (60 if mode == "text" else 100)
    if len(words) != expected_count or sum(len(word) for word in words) > 3000:
        return None
    return {"mode": mode, "config_value": config_value, "words": words}

def _challenge_snapshot(room):
    players = sorted(room["players"].values(), key=lambda player: (-player["progress"], -player["wpm"], player["name"].casefold()))
    return {
        "room_code": room["code"],
        "players": [{
            "name": player["name"], "progress": player["progress"],
            "wpm": player["wpm"], "accuracy": player["accuracy"],
            "finished": player["finished"], "connected": player["connected"]
        } for player in players]
    }

def _broadcast_challenge(room):
    socketio.emit("challenge:update", _challenge_snapshot(room), to=room["code"])

def _publish_challenge_leaderboard(room):
    if room["leaderboard"] is not None or room["participant_count"] < 2:
        return
    if any(player["connected"] and not player["finished"] for player in room["players"].values()):
        return
    finished = sorted(
        (player for player in room["players"].values() if player["finished"]),
        key=lambda player: (-player["wpm"], -player["accuracy"], player["name"].casefold())
    )
    rankings = [{
        "rank": index + 1, "name": player["name"],
        "wpm": player["wpm"], "accuracy": player["accuracy"]
    } for index, player in enumerate(finished[:2])]
    room["leaderboard"] = rankings
    socketio.emit("challenge:leaderboard", {"room_code": room["code"], "rankings": rankings}, to=room["code"])

def _detach_challenge_sid(sid, disconnected=False):
    code = challenge_sid_rooms.pop(sid, None)
    room = challenge_rooms.get(code)
    if not room:
        return
    player = room["players"].get(sid)
    if disconnected and player:
        player["connected"] = False
    else:
        room["players"].pop(sid, None)
        room["participant_count"] = len(room["players"])
        leave_room(code, sid=sid)
    if not room["players"]:
        challenge_rooms.pop(code, None)
        return
    _broadcast_challenge(room)
    _publish_challenge_leaderboard(room)

@socketio.on("challenge:create")
def create_challenge(data):
    data = data if isinstance(data, dict) else {}
    settings = _challenge_settings(data)
    if not settings:
        socketio.emit("challenge:error", {"message": "Choose a valid test setup before creating a room."}, to=request.sid)
        return
    with challenge_lock:
        _detach_challenge_sid(request.sid)
        code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        while code in challenge_rooms:
            code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        room = {
            "code": code, "settings": settings, "players": {},
            "participant_count": 0, "leaderboard": None, "created_at": time.time()
        }
        challenge_rooms[code] = room
        player = {
            "sid": request.sid, "name": _challenge_name(data.get("name")),
            "progress": 0, "wpm": 0, "accuracy": 100,
            "finished": False, "connected": True
        }
        room["players"][request.sid] = player
        room["participant_count"] = 1
        challenge_sid_rooms[request.sid] = code
        join_room(code)
        socketio.emit("challenge:ready", {"room_code": code, "settings": settings}, to=request.sid)
        _broadcast_challenge(room)

@socketio.on("challenge:join")
def join_challenge(data):
    data = data if isinstance(data, dict) else {}
    code = str(data.get("room_code", "")).strip().upper()
    with challenge_lock:
        room = challenge_rooms.get(code)
        if not room or room["leaderboard"] is not None:
            socketio.emit("challenge:error", {"message": "That room is unavailable or has already finished."}, to=request.sid)
            return
        if request.sid not in room["players"] and len(room["players"]) >= CHALLENGE_MAX_PLAYERS:
            socketio.emit("challenge:error", {"message": "That room is full."}, to=request.sid)
            return
        _detach_challenge_sid(request.sid)
        room = challenge_rooms.get(code)
        if not room or room["leaderboard"] is not None:
            socketio.emit("challenge:error", {"message": "That room is no longer available."}, to=request.sid)
            return
        room["players"][request.sid] = {
            "sid": request.sid, "name": _challenge_name(data.get("name")),
            "progress": 0, "wpm": 0, "accuracy": 100,
            "finished": False, "connected": True
        }
        room["participant_count"] += 1
        challenge_sid_rooms[request.sid] = code
        join_room(code)
        socketio.emit("challenge:ready", {"room_code": code, "settings": room["settings"]}, to=request.sid)
        _broadcast_challenge(room)

@socketio.on("challenge:progress")
def challenge_progress(data):
    data = data if isinstance(data, dict) else {}
    try:
        progress = float(data.get("progress", 0))
        wpm = float(data.get("wpm", 0))
        accuracy = float(data.get("accuracy", 100))
    except (TypeError, ValueError):
        return
    if not all(math.isfinite(value) for value in (progress, wpm, accuracy)):
        return
    with challenge_lock:
        code = challenge_sid_rooms.get(request.sid)
        room = challenge_rooms.get(code)
        player = room["players"].get(request.sid) if room else None
        if not player or player["finished"]:
            return
        player["progress"] = round(max(0, min(100, progress)), 1)
        player["wpm"] = round(max(0, min(300, wpm)), 1)
        player["accuracy"] = round(max(0, min(100, accuracy)), 1)
        _broadcast_challenge(room)

@socketio.on("challenge:finish")
def challenge_finish(data):
    data = data if isinstance(data, dict) else {}
    try:
        wpm = float(data.get("wpm", 0))
        accuracy = float(data.get("accuracy", 0))
    except (TypeError, ValueError):
        return
    if not all(math.isfinite(value) for value in (wpm, accuracy)):
        return
    with challenge_lock:
        code = challenge_sid_rooms.get(request.sid)
        room = challenge_rooms.get(code)
        player = room["players"].get(request.sid) if room else None
        if not player:
            return
        player["progress"] = 100
        player["wpm"] = round(max(0, min(300, wpm)), 1)
        player["accuracy"] = round(max(0, min(100, accuracy)), 1)
        player["finished"] = True
        _broadcast_challenge(room)
        _publish_challenge_leaderboard(room)

@socketio.on("challenge:leave")
def leave_challenge():
    with challenge_lock:
        _detach_challenge_sid(request.sid)

@socketio.on("connect")
def challenge_connect(auth=None):
    app.logger.debug("Socket.IO client connected: %s", request.sid)
    return True

@socketio.on("disconnect")
def challenge_disconnect(reason=None):
    with challenge_lock:
        _detach_challenge_sid(request.sid, disconnected=True)

@app.get("/")
def home():
    return render_template("index.html")

@app.get("/api/me")
def me():
    user = current_user()
    return jsonify({"user": {"id": user.id, "username": user.username} if user else None})

@app.post("/api/coach-feedback")
@app.post("/api/ai-feedback")
def coach_feedback():
    data = request.get_json(silent=True) or {}
    try:
        wpm = float(data["wpm"])
        accuracy = float(data["accuracy"])
        duration = int(data["duration"])
        missed_keys_input = data.get("missed_keys", [])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Invalid typing performance data."), 400

    if not all(math.isfinite(value) for value in (wpm, accuracy)):
        return jsonify(error="Invalid typing performance data."), 400
    if not (0 <= wpm <= 300 and 0 <= accuracy <= 100 and 1 <= duration <= 3600):
        return jsonify(error="Typing performance data is outside allowed ranges."), 400
    if not isinstance(missed_keys_input, list):
        return jsonify(error="Invalid missed-key data."), 400

    missed_keys = []
    for item in missed_keys_input[:3]:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", ""))[:1]
        try:
            count = int(item.get("count", 0))
        except (TypeError, ValueError):
            continue
        if key and 1 <= count <= 20000:
            missed_keys.append({"key": key, "count": count})

    api_key = next((value.strip() for value in (
        app.config.get("GEMINI_API_KEY"),
        os.getenv("GEMINI_API_KEY"),
        os.getenv("GOOGLE_API_KEY"),
    ) if isinstance(value, str) and value.strip()), "")
    if not api_key:
        return jsonify(error="Gemini API key is missing. Set GEMINI_API_KEY or GOOGLE_API_KEY in the Flask environment, then restart Flask."), 503
    now = time.time()
    if now - float(session.get("coach_feedback_at", 0)) < 3:
        return jsonify(error="Please wait a moment before requesting coach feedback again."), 429
    session["coach_feedback_at"] = now

    model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
    fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.6-flash").strip() or "gemini-3.6-flash"
    system_prompt = (
        "You are a friendly typing coach. Use only these aggregate stats: WPM, accuracy, test duration, "
        "and up to three most-missed keys. Return at most 3 very short actionable bullets, under 200 tokens total. "
        "Mention a strength and one targeted practice tip. Never invent missed keys; add no heading or filler."
    )
    keys_text = ", ".join(f"{item['key']} ({item['count']})" for item in missed_keys) or "none recorded"
    user_prompt = (
        f"WPM: {wpm:.1f}\nAccuracy: {accuracy:.1f}%\nDuration: {duration}s\n"
        f"Top error keys: {keys_text}"
    )
    try:
        from google import genai
        from google.genai import types
    except ImportError as error:
        app.logger.error("Gemini SDK import failed: %s", error)
        return jsonify(error="Gemini SDK is not installed. Run pip install -r requirements.txt and restart Flask."), 503

    current_model = model
    try:
        client = genai.Client(api_key=api_key)

        def generate_feedback(model_name):
            # Gemini 3.8 does not support "minimal"; the 3.6 fallback does.
            thinking_level = "minimal" if model_name == fallback_model or "flash-lite" in model_name else "low"
            generation_config = types.GenerateContentConfig(
                system_instruction=system_prompt,
                max_output_tokens=250,
                thinking_config=types.ThinkingConfig(thinking_level=thinking_level),
            )
            return client.models.generate_content(
                model=model_name,
                contents=user_prompt,
                config=generation_config,
            )

        try:
            response = generate_feedback(model)
        except Exception as primary_error:
            status = _gemini_status_code(primary_error)
            if status not in {404, 429, 500, 502, 503, 504} or fallback_model == model:
                raise
            _log_gemini_failure("primary model request", model, primary_error, api_key)
            current_model = fallback_model
            response = generate_feedback(fallback_model)
        else:
            if fallback_model != model and _gemini_response_is_truncated(response):
                app.logger.warning(
                    "Gemini model %s truncated coach feedback (%s); retrying with %s",
                    model,
                    _gemini_finish_reason(response),
                    fallback_model,
                )
                current_model = fallback_model
                response = generate_feedback(fallback_model)
        feedback = (response.text or "").strip()
        if not feedback:
            app.logger.warning(
                "Gemini model %s returned no coach text (finish_reason=%s)",
                current_model,
                _gemini_finish_reason(response),
            )
            return jsonify(error="The AI coach returned no feedback. Please try again."), 502
        return jsonify(feedback=feedback)
    except Exception as error:
        _log_gemini_failure("request", current_model, error, api_key)
        status = _gemini_status_code(error)
        detail = str(error).casefold()
        if status == 429 or "resource_exhausted" in detail or "quota exceeded" in detail:
            return jsonify(error="AI Coach is catching its breath. Try again in a moment.", code="gemini_rate_limited"), 429
        if status in {401, 403} or "api_key_invalid" in detail or "api key not valid" in detail:
            return jsonify(error="Gemini rejected the API key. Verify GEMINI_API_KEY in .env and restart Flask."), 503
        if status == 404 or "model not found" in detail:
            return jsonify(error=f"Gemini model '{current_model}' is unavailable. Check GEMINI_MODEL in .env."), 502
        return jsonify(error="Gemini could not generate feedback right now. Check the API key and model, then try again."), 502

@app.post("/api/register")
def register():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    if not 3 <= len(username) <= 32:
        return jsonify(error="Username must be 3–32 characters."), 400
    if "@" not in email or len(email) > 120:
        return jsonify(error="Enter a valid email address."), 400
    if len(password) < 10:
        return jsonify(error="Password must be at least 10 characters."), 400
    if User.query.filter(func.lower(User.username) == username.lower()).first():
        return jsonify(error="That username is already taken."), 409
    if User.query.filter(func.lower(User.email) == email).first():
        return jsonify(error="That email is already registered."), 409
    user = User(username=username, email=email, password_hash=generate_password_hash(password))
    db.session.add(user)
    db.session.commit()
    session.clear()
    session["user_id"] = user.id
    return jsonify(message="Account created. You are now signed in.", user=username), 201

@app.post("/api/login")
def login():
    data = request.get_json(silent=True) or {}
    identity = str(data.get("identity", "")).strip().lower()
    password = str(data.get("password", ""))
    user = User.query.filter(
        (func.lower(User.username) == identity) | (func.lower(User.email) == identity)
    ).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify(error="Incorrect username/email or password."), 401
    session.clear()
    session["user_id"] = user.id
    return jsonify(message="Signed in successfully.", user=user.username)

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(message="Signed out.")

@app.post("/api/results")
def save_result():
    user = current_user()
    if not user:
        return jsonify(error="Sign in to save your results."), 401
    data = request.get_json(silent=True) or {}
    try:
        mode = str(data["mode"])
        duration = int(data["duration"])
        wpm = float(data["wpm"])
        raw_wpm = float(data.get("raw_wpm", wpm))
        accuracy = float(data["accuracy"])
        correct = int(data.get("correct_chars", 0))
        incorrect = int(data.get("incorrect_chars", 0))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Invalid result payload."), 400
    if mode not in {"time", "words", "text"} or not 1 <= duration <= 3600:
        return jsonify(error="Invalid test mode or duration."), 400
    if not (0 <= wpm <= 300 and 0 <= raw_wpm <= 400 and 0 <= accuracy <= 100):
        return jsonify(error="Result values are outside allowed ranges."), 400
    if min(correct, incorrect) < 0 or correct + incorrect > 20000:
        return jsonify(error="Invalid character counts."), 400
    db.session.add(TypingResult(
        user_id=user.id, mode=mode, duration=duration, wpm=wpm,
        raw_wpm=raw_wpm, accuracy=accuracy,
        correct_chars=correct, incorrect_chars=incorrect
    ))
    db.session.commit()
    return jsonify(ok=True), 201

@app.get("/api/history")
def history():
    user = current_user()
    if not user:
        return jsonify(error="Sign in to view history."), 401
    rows = TypingResult.query.filter_by(user_id=user.id).order_by(TypingResult.created_at.desc()).limit(100).all()
    return jsonify([{
        "date": row.created_at.isoformat(), "mode": row.mode,
        "duration": row.duration, "wpm": row.wpm, "accuracy": row.accuracy
    } for row in rows])

@app.get("/api/leaderboard")
def leaderboard():
    period = request.args.get("period", "all")
    query = db.session.query(User.username, func.max(TypingResult.wpm).label("best")).join(
        TypingResult, User.id == TypingResult.user_id
    )
    now = datetime.utcnow()
    if period == "daily":
        query = query.filter(TypingResult.created_at >= now - timedelta(days=1))
    elif period == "weekly":
        query = query.filter(TypingResult.created_at >= now - timedelta(days=7))
    elif period == "monthly":
        query = query.filter(TypingResult.created_at >= now - timedelta(days=30))
    elif period != "all":
        return jsonify(error="Invalid period."), 400
    rows = query.group_by(User.id, User.username).order_by(func.max(TypingResult.wpm).desc()).limit(100).all()
    return jsonify([{"rank": i + 1, "username": r.username, "wpm": round(r.best, 1)} for i, r in enumerate(rows)])

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    socketio.run(app, debug=os.getenv("FLASK_DEBUG", "1") == "1", allow_unsafe_werkzeug=True)
