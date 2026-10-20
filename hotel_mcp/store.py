"""Hotel business transactions, authorization scopes, history and durable events."""
import base64
import hashlib
import hmac
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .domain import (ACTIONS, CATEGORIES, STATUSES, REQUEST_TYPES, BusinessError, Principal, canonical,
                     checkout_date, fail, text, timestamp, utcnow, version)
from .migrations import ensure_schema
from .stay_extensions import StayExtensions
from .sales import Sales
from .entities import Entities
from .services import Services, SERVICE_TYPES


class HotelStore:
    def __init__(self, path, *, migrate=False):
        self.path = str(path)
        self.is_mysql = self.path == "mysql"
        if self.is_mysql:
            from .mysql_schema import ensure_schema as ensure_mysql
            ensure_mysql(allow_upgrade=migrate)
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with self.connect() as db:
                ensure_schema(db, allow_upgrade=migrate)
        with self.connect() as db:
            if not self.is_mysql:
                db.execute("PRAGMA journal_mode=WAL")
            self.cursor_secret = bytes.fromhex(db.execute(
                "SELECT value FROM service_metadata WHERE key='cursor_secret'").fetchone()[0])
        self.extensions = StayExtensions(self)
        self.sales = Sales(self)
        self.entities = Entities(self)
        self.services = Services(self)

    @contextmanager
    def connect(self):
        if self.is_mysql:
            from .mysql import connection
            with connection() as db:
                yield db
            return
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def configure_rooms(self, rooms):
        """Local configuration only; not exposed as a guest-controlled tool."""
        normalized = [(text(r["hotel"], "hotel", 80), text(r["room"], "room", 80),
                       int(r.get("enabled", True))) for r in rooms]
        with self.connect() as db:
            db.executemany("INSERT INTO rooms(hotel,room,enabled) VALUES(?,?,?) "
                           "ON CONFLICT(hotel,room) DO UPDATE SET enabled=excluded.enabled", normalized)

    @staticmethod
    def _desk(p):
        if p.role != "frontdesk":
            fail("FORBIDDEN", "仅前台可执行此操作")

    @staticmethod
    def _room(p):
        if p.role != "room":
            fail("FORBIDDEN", "仅客房可提交请求")

    @staticmethod
    def _authorize(db, p):
        if p.role == "manager":
            fail("FORBIDDEN", "管理身份仅可查询授权门店销售数据")
        if p.role == "frontdesk":
            return
        row = db.execute("SELECT s.* FROM stays s JOIN rooms r ON r.hotel=s.hotel AND r.room=s.room "
                         "WHERE s.id=? AND s.hotel=? AND s.room=? AND s.status='active' AND r.enabled=1",
                         (p.stay_id, p.hotel, p.room)).fetchone()
        if not row:
            fail("STAY_INACTIVE", "客房凭据未绑定有效入住，请联系前台")
        return row

    def authorize(self, p):
        if p.role == "manager":
            return
        with self.connect() as db:
            self._authorize(db, p)

    @staticmethod
    def _scope(p):
        if p.role == "frontdesk":
            return "hotel=?", [p.hotel]
        return "hotel=? AND room=? AND stay_id=?", [p.hotel, p.room, p.stay_id]

    def _find(self, db, p, request_id):
        scope, args = self._scope(p)
        row = db.execute(f"SELECT * FROM requests WHERE {scope} AND id=?", (*args, request_id)).fetchone()
        if not row:
            fail("NOT_FOUND", "工单不存在")
        return row

    @staticmethod
    def public(row):
        return {key: row[key] for key in ("id", "room", "description", "status", "note", "created_at", "updated_at")}

    def _detail(self, db, row):
        value = {**self.public(row), **{k: row[k] for k in ("stay_id", "request_type", "version")}}
        if row["request_type"] == "repair":
            detail = db.execute("SELECT category,location,urgency,related_request_id FROM repair_details WHERE request_id=?", (row["id"],)).fetchone()
            value["repair"] = dict(detail)
        elif row["request_type"] == "stay_extension":
            value["extension"] = self.extensions.detail(db, row)
        elif row["request_type"] in SERVICE_TYPES:
            value.update(self.services.detail(db, row))
        return value

    def _write(self, p, operation, request_key, payload, perform):
        request_key = text(request_key, "request_key", 80)
        digest = hashlib.sha256(canonical(payload).encode()).hexdigest()
        stay_scope = p.stay_id if p.role == "room" else ""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorize(db, p)
            receipt = db.execute("SELECT * FROM operation_receipts WHERE hotel=? AND principal_id=? "
                                 "AND stay_scope=? AND operation=? AND request_key=?",
                                 (p.hotel, p.actor_id, stay_scope, operation, request_key)).fetchone()
            if receipt:
                if receipt["payload_hash"] != digest:
                    fail("IDEMPOTENCY_CONFLICT", "request_key 已用于其他参数，请勿覆盖原操作")
                return {**json.loads(receipt["response_json"]), "replayed": True}
            result = perform(db)
            result.setdefault("replayed", False)
            db.execute("INSERT INTO operation_receipts VALUES(?,?,?,?,?,?,?,?)",
                       (p.hotel, p.actor_id, stay_scope, operation, request_key, digest, canonical(result), utcnow()))
            return result

    def current_stay(self, p):
        self._room(p)
        with self.connect() as db:
            return {"stay": dict(self._authorize(db, p))}

    def start_stay(self, p, room, expected_checkout_date, request_key):
        self._desk(p)
        room = text(room, "room", 80)
        expected_checkout_date = checkout_date(expected_checkout_date)
        def perform(db):
            if not db.execute("SELECT 1 FROM rooms WHERE hotel=? AND room=? AND enabled=1", (p.hotel, room)).fetchone():
                fail("NOT_FOUND", "房间不存在或已停用")
            if db.execute("SELECT 1 FROM stays WHERE hotel=? AND room=? AND status='active'", (p.hotel, room)).fetchone():
                fail("INVALID_TRANSITION", "房间已有有效入住")
            stay_id = uuid.uuid4().hex
            db.execute("INSERT INTO stays(id,hotel,room,status,check_in_at,expected_checkout_date) VALUES(?,?,?,'active',?,?)",
                       (stay_id, p.hotel, room, utcnow(), expected_checkout_date))
            return {"stay": dict(db.execute("SELECT * FROM stays WHERE id=?", (stay_id,)).fetchone())}
        return self._write(p, "start_stay", request_key, [room, expected_checkout_date], perform)

    def end_stay(self, p, stay_id, expected_version, reason, request_key):
        self._desk(p)
        reason, expected_version = text(reason, "reason", 500), version(expected_version)
        def perform(db):
            row = db.execute("SELECT * FROM stays WHERE hotel=? AND id=?", (p.hotel, stay_id)).fetchone()
            if not row:
                fail("NOT_FOUND", "入住记录不存在")
            self._check_version(row, expected_version)
            if row["status"] != "active":
                fail("INVALID_TRANSITION", "入住已结束")
            cancelled = self.extensions.on_checkout(db, p, stay_id, reason)
            cancelled_services = self.services.on_checkout(db, p, stay_id)
            db.execute("UPDATE stays SET status='closed',closed_at=?,close_reason=?,version=version+1 WHERE id=?",
                       (utcnow(), reason, stay_id))
            count = db.execute("SELECT COUNT(*) FROM requests WHERE hotel=? AND stay_id=? AND status IN ('pending','accepted')",
                               (p.hotel, stay_id)).fetchone()[0]
            return {"stay": dict(db.execute("SELECT * FROM stays WHERE id=?", (stay_id,)).fetchone()),
                    "open_request_count": count, "cancelled_extension_count": cancelled,
                    "cancelled_service_count": cancelled_services}
        return self._write(p, "end_stay", request_key, [stay_id, expected_version, reason], perform)

    @staticmethod
    def _check_version(row, expected):
        if row["version"] != expected:
            fail("VERSION_CONFLICT", "记录已被其他操作更新，请重新查询")

    @staticmethod
    def _record(db, p, row, action, previous, note, data=None):
        db.execute("INSERT INTO request_actions(request_id,action,from_status,to_status,note,actor_principal_id,"
                   "actor_role,created_at,request_version,data) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (row["id"], action, previous, row["status"], note, p.actor_id, p.role, utcnow(), row["version"], canonical(data or {})))

    @staticmethod
    def emit(db, p, role, room, order, message, action=None, event_data=None):
        stay_id = order["stay_id"]
        if role == "room" and (not stay_id or not db.execute(
                "SELECT 1 FROM stays s JOIN rooms r ON r.hotel=s.hotel AND r.room=s.room "
                "WHERE s.id=? AND s.status='active' AND r.enabled=1", (stay_id,)).fetchone()):
            return
        event_type = "hotel.request." + order["status"]
        if order["request_type"] in ("repair", "stay_extension", *SERVICE_TYPES):
            suffix = "submitted" if action == "submit" else "updated" if action == "add_note" else order["status"]
            event_type = "hotel." + order["request_type"] + "." + suffix
        payload = {"schema_version": 2, "event_type": event_type, "request_id": order["id"],
                   "request_type": order["request_type"], "request_version": order["version"],
                   "status": order["status"], "speech_text": message, "display_text": message}
        payload.update(event_data or {})
        db.execute("INSERT INTO events(hotel,role,room,payload,stay_id) VALUES(?,?,?,?,?)",
                   (p.hotel, role, room, canonical(payload), stay_id))

    def _submit(self, p, request_key, description, repair=None):
        self._room(p)
        request_key = text(request_key, "request_key", 80)
        description = text(description, "description", 300)
        kind = "repair" if repair is not None else "general"
        def perform(db):
            if repair and repair["related_request_id"]:
                related = self._find(db, p, repair["related_request_id"])
                if related["request_type"] != "repair":
                    fail("VALIDATION_ERROR", "只能关联本次入住的报障工单")
            old = db.execute("SELECT * FROM requests WHERE hotel=? AND stay_id=? AND request_key=?",
                             (p.hotel, p.stay_id, request_key)).fetchone()
            if old:
                detail = self._detail(db, old)
                if old["request_type"] != kind or old["description"] != description or detail.get("repair") != repair:
                    fail("IDEMPOTENCY_CONFLICT", "request_key 已用于其他工单")
                return {"request": detail, "replayed": True}
            request_id, now = uuid.uuid4().hex[:16], utcnow()
            db.execute("INSERT INTO requests(id,hotel,room,stay_id,request_key,request_type,description,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,?,?,?,?)", (request_id, p.hotel, p.room, p.stay_id, request_key, kind, description, now, now))
            if repair is not None:
                db.execute("INSERT INTO repair_details VALUES(?,?,?,?,?)",
                           (request_id, repair["category"], repair["location"], repair["urgency"], repair["related_request_id"]))
            row = self._find(db, p, request_id)
            self._record(db, p, row, "submit", None, description)
            self.emit(db, p, "frontdesk", "", row, f"{p.room}号客房有新{'报障' if repair else '请求'}：{description}。工单号{request_id}。", "submit")
            return {"request": self._detail(db, row)}
        return self._write(p, "submit_" + kind, request_key, [description, repair], perform)

    def submit(self, p, request_key, description):
        submitted = self._submit(p, request_key, description)
        # Legacy submit retries historically returned the current order state.
        return self.public(self.get_request(p, submitted["request"]["id"])["request"])

    def submit_repair(self, p, request_key, category, description, location="", urgency="normal", related_request_id=None):
        if category not in CATEGORIES or urgency not in ("normal", "urgent"):
            fail("VALIDATION_ERROR", "无效的故障分类或紧急程度")
        if related_request_id is not None:
            related_request_id = text(related_request_id, "related_request_id", 80)
        return self._submit(p, request_key, description, {"category": category, "location": text(location, "location", 80, True),
                                                        "urgency": urgency, "related_request_id": related_request_id})

    def get_request(self, p, request_id):
        with self.connect() as db:
            self._authorize(db, p)
            return {"request": self._detail(db, self._find(db, p, request_id))}

    def _change(self, db, p, row, action, note):
        if row["request_type"] == "stay_extension":
            return self.extensions.common_change(db, p, row, action, note)
        if row["request_type"] in SERVICE_TYPES:
            return self.services.change(db, p, row, action, note)
        source = row["status"]
        targets = {"accept": ("pending", "accepted"), "complete": ("accepted", "completed"),
                   "reject": ("pending", "rejected"), "close_unresolved": ("accepted", "closed_unresolved"),
                   "add_note": ("accepted", "accepted")}
        if action == "cancel":
            allowed = ("pending", "accepted") if p.role == "frontdesk" else ("pending",)
            if source not in allowed:
                fail("INVALID_TRANSITION", "当前状态不能取消；已受理工单请联系前台")
            target = "cancelled"
        else:
            expected, target = targets[action]
            if source != expected:
                fail("INVALID_TRANSITION", f"此操作要求状态为 {expected}")
        db.execute("UPDATE requests SET status=?,note=?,updated_at=?,version=version+1 WHERE id=?",
                   (target, note, utcnow(), row["id"]))
        updated = self._find(db, p, row["id"])
        self._record(db, p, updated, action, source, note)
        labels = {"accept": "已受理", "complete": "已完成", "reject": "已拒绝", "cancel": "已取消",
                  "close_unresolved": "未解决并已结案", "add_note": "更新了处理进展"}
        role, room = ("frontdesk", "") if p.role == "room" else ("room", row["room"])
        message = f"{row['room']}号客房工单{labels[action]}：{row['description']}。{note}"
        self.emit(db, p, role, room, updated, message, action)
        return updated

    def process_request(self, p, request_id, action, note, expected_version, request_key):
        self._desk(p)
        if action not in ACTIONS:
            fail("VALIDATION_ERROR", "无效的处理动作")
        return self._process(p, request_id, action, note, expected_version, request_key)

    def cancel_request(self, p, request_id, reason, expected_version, request_key):
        return self._process(p, request_id, "cancel", reason, expected_version, request_key)

    def _process(self, p, request_id, action, note, expected_version, request_key):
        note, expected_version = text(note, "note", 500), version(expected_version)
        def perform(db):
            row = self._find(db, p, request_id)
            self._check_version(row, expected_version)
            return {"request": self._detail(db, self._change(db, p, row, action, note))}
        return self._write(p, "process_request", request_key, [request_id, action, note, expected_version], perform)

    def transition(self, p, request_id, status, note=""):
        """Legacy general-only contract, including same-state/note idempotence."""
        self._desk(p)
        if status not in ("accepted", "completed") or not isinstance(note, str) or len(note) > 160:
            fail("VALIDATION_ERROR", "无效状态或备注超过160字符")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._find(db, p, request_id)
            if row["request_type"] != "general":
                fail("VALIDATION_ERROR", "此旧接口仅支持一般工单；请使用对应业务的处理工具")
            if row["status"] == status:
                if row["note"] != note:
                    fail("IDEMPOTENCY_CONFLICT", "工单已使用其他备注处理")
                return self.public(row)
            return self.public(self._change(db, p, row, "accept" if status == "accepted" else "complete", note))

    def list_requests(self, p, after=""):
        with self.connect() as db:
            self._authorize(db, p)
            scope, args = self._scope(p)
            rows = db.execute(f"SELECT * FROM requests WHERE {scope} AND id>? ORDER BY id LIMIT 51", (*args, after)).fetchall()
            return {"requests": [self.public(row) for row in rows[:50]], "next_cursor": rows[49]["id"] if len(rows) > 50 else None}

    @staticmethod
    def _limit(limit):
        if type(limit) is not int or not 1 <= limit <= 50:
            fail("VALIDATION_ERROR", "limit 必须为1～50")

    def _cursor_context(self, p, kind, filters):
        return hashlib.sha256(canonical([p.hotel, p.role, p.room, p.stay_id, p.actor_id, kind, filters]).encode()).hexdigest()

    def _encode_cursor(self, context, upper, last):
        raw = canonical({"context": context, "upper": upper, "last": last}).encode()
        signature = hmac.digest(self.cursor_secret, raw, "sha256")
        return base64.urlsafe_b64encode(signature + raw).decode()

    def _decode_cursor(self, value, context):
        try:
            if not isinstance(value, str) or len(value) > 2048:
                raise ValueError()
            raw = base64.b64decode(value, altchars=b"-_", validate=True)
            signature, body = raw[:32], raw[32:]
            if not hmac.compare_digest(signature, hmac.digest(self.cursor_secret, body, "sha256")):
                raise ValueError()
            data = json.loads(body)
            if data["context"] != context or type(data["upper"]) is not int or type(data["last"]) is not int:
                raise ValueError()
            if not 0 < data["last"] <= data["upper"]:
                raise ValueError()
            return data["upper"], data["last"]
        except (ValueError, TypeError, KeyError):
            fail("VALIDATION_ERROR", "游标无效或与当前身份、筛选条件不匹配")

    def query_requests(self, p, request_type=None, statuses=None, created_from=None, created_to=None,
                       room=None, stay_id=None, cursor=None, limit=20):
        self._limit(limit)
        if request_type is not None and request_type not in REQUEST_TYPES:
            fail("VALIDATION_ERROR", "无效工单类型")
        if statuses is not None and (not isinstance(statuses, list) or not statuses or any(s not in STATUSES for s in statuses)):
            fail("VALIDATION_ERROR", "无效状态筛选")
        if p.role == "room" and (room is not None or stay_id is not None):
            fail("FORBIDDEN", "客房不能指定查询身份范围")
        statuses = sorted(set(statuses)) if statuses else None
        created_from = timestamp(created_from, "created_from") if created_from is not None else None
        created_to = timestamp(created_to, "created_to") if created_to is not None else None
        if created_from and created_to and created_from > created_to:
            fail("VALIDATION_ERROR", "开始时间不能晚于结束时间")
        filters = [request_type, statuses, created_from, created_to, room, stay_id]
        context = self._cursor_context(p, "requests", filters)
        with self.connect() as db:
            self._authorize(db, p)
            scope, args = self._scope(p)
            clauses = [scope]
            for field, op, value in (("request_type", "=", request_type), ("created_at", ">=", created_from),
                                     ("created_at", "<=", created_to), ("room", "=", room), ("stay_id", "=", stay_id)):
                if value is not None:
                    clauses.append(f"{field}{op}?")
                    args.append(value)
            if statuses:
                clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
                args.extend(statuses)
            where = " AND ".join(clauses)
            if cursor is not None:
                upper, last = self._decode_cursor(cursor, context)
            else:
                upper = db.execute(f"SELECT COALESCE(MAX(seq),0) FROM requests WHERE {where}", args).fetchone()[0]
                last = upper + 1
            rows = db.execute(f"SELECT * FROM requests WHERE {where} AND seq<=? AND seq<? ORDER BY seq DESC LIMIT ?",
                              (*args, upper, last, limit + 1)).fetchall()
            return {"requests": [self._detail(db, row) for row in rows[:limit]],
                    "next_cursor": self._encode_cursor(context, upper, rows[limit-1]["seq"]) if len(rows) > limit else None}

    def list_actions(self, p, request_id, cursor=None, limit=20):
        self._limit(limit)
        context = self._cursor_context(p, "actions", [request_id])
        with self.connect() as db:
            self._authorize(db, p)
            self._find(db, p, request_id)
            if cursor is not None:
                upper, last = self._decode_cursor(cursor, context)
            else:
                upper = db.execute("SELECT COALESCE(MAX(id),0) FROM request_actions WHERE request_id=?", (request_id,)).fetchone()[0]
                last = upper + 1
            rows = db.execute("SELECT * FROM request_actions WHERE request_id=? AND id<=? AND id<? ORDER BY id DESC LIMIT ?",
                              (request_id, upper, last, limit + 1)).fetchall()
            items = [dict(row) for row in rows[:limit]]
            for item in items:
                item["data"] = json.loads(item["data"])
            if p.role == "room":
                for item in items:
                    item.pop("actor_principal_id")
            return {"actions": items, "next_cursor": self._encode_cursor(context, upper, rows[limit-1]["id"]) if len(rows) > limit else None}

    def inbox(self, p, after=0):
        if type(after) is not int or after < 0:
            fail("VALIDATION_ERROR", "无效事件游标")
        with self.connect() as db:
            self._authorize(db, p)
            scope = "hotel=? AND role=? AND room=?"
            args = [p.hotel, p.role, p.room]
            if p.role == "room":
                scope += " AND stay_id=?"
                args.append(p.stay_id)
            rows = db.execute(f"SELECT seq,payload FROM events WHERE {scope} AND seq>? ORDER BY seq LIMIT 51", (*args, after)).fetchall()
        events = [{**json.loads(row["payload"]), "seq": row["seq"], "event_id": f"hotel-{row['seq']}"} for row in rows[:50]]
        return {"events": events, "next_cursor": events[-1]["seq"] if events else after, "has_more": len(rows) > 50}

    def latest_event_seq(self, p):
        with self.connect() as db:
            self._authorize(db, p)
            query, args = "SELECT COALESCE(MAX(seq),0) FROM events WHERE hotel=? AND role=? AND room=?", [p.hotel, p.role, p.room]
            if p.role == "room":
                query += " AND stay_id=?"
                args.append(p.stay_id)
            return db.execute(query, args).fetchone()[0]
