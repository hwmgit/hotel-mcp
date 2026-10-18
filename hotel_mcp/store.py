"""SQLite 事务同时保存业务状态和事件；身份来自服务端凭证配置。"""
import json
import sqlite3
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path


@dataclass(frozen=True)
class Principal:
    hotel: str
    role: str
    room: str = ""

    def __post_init__(self):
        if not self.hotel or self.role not in ("room", "frontdesk") or (self.role == "room" and not self.room):
            raise ValueError("invalid principal")
        if self.role == "frontdesk" and self.room:
            raise ValueError("frontdesk principal must not specify a room")


class HotelStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, hotel TEXT NOT NULL, room TEXT NOT NULL,
                    request_key TEXT NOT NULL, description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(hotel, room, request_key));
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, hotel TEXT NOT NULL,
                    role TEXT NOT NULL, room TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_events_scope ON events(hotel, role, room, seq);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def public(row):
        return {key: row[key] for key in ("id", "room", "description", "status", "note", "created_at", "updated_at")}

    @staticmethod
    def emit(db, p, role, room, order, text):
        payload = {"event_type": "hotel.request." + order["status"], "request_id": order["id"],
                   "speech_text": text, "display_text": text}
        db.execute("INSERT INTO events(hotel, role, room, payload) VALUES(?,?,?,?)",
                   (p.hotel, role, room, json.dumps(payload, ensure_ascii=False)))

    def submit(self, p, request_key, description):
        if p.role != "room":
            raise ValueError("only room agents can submit requests")
        if not isinstance(request_key, str) or not 1 <= len(request_key.strip()) <= 80:
            raise ValueError("request_key must contain 1 to 80 characters")
        if not isinstance(description, str) or not 1 <= len(description.strip()) <= 300:
            raise ValueError("description must contain 1 to 300 characters")
        request_key, description = request_key.strip(), description.strip()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM requests WHERE hotel=? AND room=? AND request_key=?",
                             (p.hotel, p.room, request_key)).fetchone()
            if old:
                if old["description"] != description:
                    raise ValueError("request_key already belongs to a different request")
                return self.public(old)
            order_id = uuid.uuid4().hex[:16]
            db.execute("INSERT INTO requests(id,hotel,room,request_key,description) VALUES(?,?,?,?,?)",
                       (order_id, p.hotel, p.room, request_key, description))
            order = self.public(db.execute("SELECT * FROM requests WHERE id=?", (order_id,)).fetchone())
            self.emit(db, p, "frontdesk", "", order, f"{p.room}号客房有新请求：{description}。工单号{order_id}。")
            return order

    def list_requests(self, p, after=""):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM requests WHERE hotel=? AND (?='frontdesk' OR room=?) "
                              "AND id>? ORDER BY id LIMIT 51", (p.hotel, p.role, p.room, after)).fetchall()
            return {"requests": [self.public(row) for row in rows[:50]],
                    "next_cursor": rows[49]["id"] if len(rows) > 50 else None}

    def transition(self, p, request_id, status, note=""):
        if p.role != "frontdesk" or status not in ("accepted", "completed"):
            raise ValueError("only frontdesk agents can process requests")
        if not isinstance(note, str) or len(note) > 160:
            raise ValueError("note must not exceed 160 characters")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM requests WHERE hotel=? AND id=?", (p.hotel, request_id)).fetchone()
            if not row:
                raise ValueError("request not found")
            if row["status"] == status:
                if row["note"] != note:
                    raise ValueError("request already processed with a different note")
                return self.public(row)
            expected = "pending" if status == "accepted" else "accepted"
            if row["status"] != expected:
                raise ValueError(f"request must be {expected} before this action")
            db.execute("UPDATE requests SET status=?,note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                       (status, note, request_id))
            order = self.public(db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone())
            action = "已受理" if status == "accepted" else "已完成"
            self.emit(db, p, "room", row["room"], order, f"前台{action}您的请求：{row['description']}。{note}")
            return order

    def inbox(self, p, after=0):
        if not isinstance(after, int) or after < 0:
            raise ValueError("invalid event cursor")
        with self.connect() as db:
            rows = db.execute("SELECT seq,payload FROM events WHERE hotel=? AND role=? AND room=? "
                              "AND seq>? ORDER BY seq LIMIT 51", (p.hotel, p.role, p.room, after)).fetchall()
        events = [{**json.loads(row["payload"]), "seq": row["seq"], "event_id": f"hotel-{row['seq']}"} for row in rows[:50]]
        return {"events": events, "next_cursor": events[-1]["seq"] if events else after, "has_more": len(rows) > 50}
