"""Core meal and fixed-slot booking workflows on the shared request transaction."""
import uuid

from .domain import Principal, fail, text, timestamp, utcnow
from .sales import business_date


SERVICE_TYPES = ("meal", "gym_booking", "meeting_booking")


class Services:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def insert_slot(db, slot):
        """Trusted catalog import only; caller must hold BEGIN IMMEDIATE's write lock."""
        required = ("id", "hotel", "resource_id", "business_date", "starts_at", "ends_at")
        values = {key: slot[key] for key in required}
        business_date(values["business_date"], "business_date")
        values["starts_at"] = timestamp(values["starts_at"], "starts_at")
        values["ends_at"] = timestamp(values["ends_at"], "ends_at")
        if values["ends_at"] <= values["starts_at"]:
            fail("VALIDATION_ERROR", "时段结束须晚于开始")
        if db.execute("SELECT 1 FROM booking_slots WHERE resource_id=? AND starts_at<? AND ends_at>?",
                      (values["resource_id"], values["ends_at"], values["starts_at"])).fetchone():
            fail("VALIDATION_ERROR", "同一资源的时段不可重叠")
        db.execute("INSERT INTO booking_slots(id,hotel,resource_id,business_date,starts_at,ends_at) VALUES(?,?,?,?,?,?)",
                   [values[key] for key in required])

    def menu(self, p):
        with self.store.connect() as db:
            self.store._authorize(db, p)
            rows = db.execute("SELECT id,name,description,price_minor,enabled FROM menu_items "
                              "WHERE hotel=? AND enabled=1 ORDER BY id", (p.hotel,)).fetchall()
        return {"items": [dict(r) for r in rows], "currency": "CNY", "settlement": "offline"}

    @staticmethod
    def _used(db, slot_id):
        return db.execute("SELECT COALESCE(SUM(b.units),0) FROM booking_details b JOIN requests r ON r.id=b.request_id "
                          "WHERE b.slot_id=? AND r.status='accepted'", (slot_id,)).fetchone()[0]

    def slots(self, p, kind, date):
        if kind not in ("gym", "meeting"):
            fail("VALIDATION_ERROR", "kind 必须为 gym/meeting")
        business_date(date, "date")
        with self.store.connect() as db:
            db.execute("BEGIN")
            self.store._authorize(db, p)
            rows = db.execute("SELECT s.*,r.name,r.kind,r.capacity,r.max_people FROM booking_slots s "
                              "JOIN booking_resources r ON r.id=s.resource_id WHERE s.hotel=? AND s.business_date=? "
                              "AND r.kind=? AND r.enabled=1 AND s.starts_at>? ORDER BY s.starts_at,r.id",
                              (p.hotel, date, kind, utcnow())).fetchall()
            slots = [{**dict(r), "remaining": max(0, r["capacity"] - self._used(db, r["id"]))} for r in rows]
        return {"slots": slots, "date": date, "kind": kind, "timezone": "Asia/Shanghai",
                "notice": "申请不占用资源，以前台确认结果为准。"}

    def detail(self, db, row):
        if row["request_type"] == "meal":
            meal = dict(db.execute("SELECT * FROM meal_orders WHERE request_id=?", (row["id"],)).fetchone())
            meal["items"] = [dict(r) for r in db.execute("SELECT item_id,name,unit_price_minor,quantity FROM meal_order_items "
                                                       "WHERE request_id=? ORDER BY item_id", (row["id"],))]
            return {"meal": meal}
        return {"booking": dict(db.execute("SELECT * FROM booking_details WHERE request_id=?", (row["id"],)).fetchone())}

    def _create(self, db, p, request_key, kind, description, note):
        if db.execute("SELECT 1 FROM requests WHERE hotel=? AND stay_id=? AND request_key=?",
                      (p.hotel, p.stay_id, request_key)).fetchone():
            fail("IDEMPOTENCY_CONFLICT", "request_key 已用于其他工单")
        ident, now = uuid.uuid4().hex[:16], utcnow()
        db.execute("INSERT INTO requests(id,hotel,room,stay_id,request_key,request_type,description,note,created_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)", (ident, p.hotel, p.room, p.stay_id, request_key, kind, description, note, now, now))
        return self.store._find(db, p, ident)

    def _submitted(self, db, p, row, note):
        self.store._record(db, p, row, "submit", None, note)
        self.store.emit(db, p, "frontdesk", "", row, f"{p.room}号客房提交{row['description']}，请前台确认。", "submit")
        return {"request": self.store._detail(db, row)}

    def submit_meal(self, p, items, request_key, note=""):
        self.store._room(p)
        note = text(note, "note", 300, True)
        if not isinstance(items, list) or not 1 <= len(items) <= 10:
            fail("VALIDATION_ERROR", "请选择1～10种菜品")
        normalized = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {"item_id", "quantity"}:
                fail("VALIDATION_ERROR", "菜品参数须包含 item_id/quantity")
            ident = text(item["item_id"], "item_id", 80)
            qty = item["quantity"]
            if type(qty) is not int or not 1 <= qty <= 20:
                fail("VALIDATION_ERROR", "每种菜品数量须为1～20")
            normalized.append((ident, qty))
        normalized.sort()
        if len({i for i, _ in normalized}) != len(normalized):
            fail("VALIDATION_ERROR", "同种菜品请合并数量")
        def perform(db):
            snapshots = []
            for ident, qty in normalized:
                item = db.execute("SELECT * FROM menu_items WHERE id=? AND hotel=? AND enabled=1", (ident, p.hotel)).fetchone()
                if not item:
                    fail("ITEM_UNAVAILABLE", "菜品不存在或已下架，请刷新菜单")
                snapshots.append((ident, item["name"], item["price_minor"], qty))
            total = sum(price * qty for _, _, price, qty in snapshots)
            description = "点餐：" + "、".join(f"{name}×{qty}" for _, name, _, qty in snapshots)
            row = self._create(db, p, request_key, "meal", description, note)
            db.execute("INSERT INTO meal_orders(request_id,total_amount_minor) VALUES(?,?)", (row["id"], total))
            db.executemany("INSERT INTO meal_order_items VALUES(?,?,?,?,?)", [(row["id"], *item) for item in snapshots])
            return self._submitted(db, p, row, note)
        return self.store._write(p, "submit_meal", request_key, [normalized, note], perform)

    def _slot(self, db, hotel, slot_id, stay):
        slot = db.execute("SELECT s.*,r.kind,r.name,r.capacity,r.max_people,r.enabled FROM booking_slots s "
                          "JOIN booking_resources r ON r.id=s.resource_id WHERE s.id=? AND s.hotel=?", (slot_id, hotel)).fetchone()
        if not slot or not slot["enabled"]:
            fail("NOT_FOUND", "预约时段不存在或场地已停用")
        if slot["starts_at"] <= utcnow():
            fail("SLOT_EXPIRED", "时段已开始，请选择未来时段")
        if slot["business_date"] >= stay["expected_checkout_date"]:
            fail("VALIDATION_ERROR", "预约须在预计退房日期之前")
        return slot

    def submit_booking(self, p, slot_id, participants, request_key, note=""):
        self.store._room(p)
        slot_id, note = text(slot_id, "slot_id", 80), text(note, "note", 300, True)
        if type(participants) is not int or not 1 <= participants <= 100:
            fail("VALIDATION_ERROR", "预约人数须为1～100")
        def perform(db):
            stay = self.store._authorize(db, p)
            slot = self._slot(db, p.hotel, slot_id, stay)
            if participants > slot["max_people"]:
                fail("VALIDATION_ERROR", "人数超过场地人数上限")
            units = participants if slot["kind"] == "gym" else 1
            if units > slot["capacity"]:
                fail("VALIDATION_ERROR", "人数超过时段容量")
            kind = "gym_booking" if slot["kind"] == "gym" else "meeting_booking"
            row = self._create(db, p, request_key, kind, f"预约：{slot['name']} · {participants}人", note)
            db.execute("INSERT INTO booking_details VALUES(?,?,?,?,?,?,?)",
                       (row["id"], slot_id, participants, units, slot["name"], slot["starts_at"], slot["ends_at"]))
            return self._submitted(db, p, row, note)
        return self.store._write(p, "submit_booking", request_key, [slot_id, participants, note], perform)

    def change(self, db, p, row, action, note, *, checkout=False):
        source = row["status"]
        rules = {"accept": (("pending",), "accepted"), "complete": (("accepted",), "completed"),
                 "reject": (("pending",), "rejected"), "add_note": (("pending", "accepted"), source),
                 "cancel": (("pending", "accepted") if p.role == "frontdesk" else ("pending",), "cancelled")}
        if action not in rules or source not in rules[action][0]:
            fail("INVALID_TRANSITION", "当前状态不能执行此操作；已确认服务请联系前台取消")
        if action == "accept":
            stay = self.store._authorize(db, Principal(p.hotel, "room", row["room"], row["stay_id"]))
            if row["request_type"] != "meal":
                details = self.detail(db, row)["booking"]
                slot = self._slot(db, p.hotel, details["slot_id"], stay)
                if details["participants"] > slot["max_people"] or self._used(db, slot["id"]) + details["units"] > slot["capacity"]:
                    fail("RESOURCE_UNAVAILABLE", "时段剩余容量不足，尚未确认；请选择其他时段或拒绝该申请")
        target = rules[action][1]
        db.execute("UPDATE requests SET status=?,note=?,updated_at=?,version=version+1 WHERE id=?", (target, note, utcnow(), row["id"]))
        updated = self.store._find(db, p, row["id"])
        self.store._record(db, p, updated, action, source, note)
        role = "frontdesk" if checkout or p.role == "room" else "room"
        label = {"accept": "已确认", "complete": "已完成", "reject": "已拒绝", "cancel": "已取消", "add_note": "有新进展"}[action]
        self.store.emit(db, p, role, "" if role == "frontdesk" else row["room"], updated,
                        f"{row['room']}号客房的{row['description']}{label}。{note}", action)
        return updated

    def on_checkout(self, db, p, stay_id):
        rows = db.execute("SELECT * FROM requests WHERE hotel=? AND stay_id=? AND "
                          "((request_type='meal' AND status='pending') OR "
                          "(request_type IN ('gym_booking','meeting_booking') AND status IN ('pending','accepted')))",
                          (p.hotel, stay_id)).fetchall()
        for row in rows:
            self.change(db, p, row, "cancel", "退房取消未履行服务", checkout=True)
        return len(rows)
