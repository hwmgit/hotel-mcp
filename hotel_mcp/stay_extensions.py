"""Manual stay-extension workflow. Never calls PMS, payment or hardware APIs."""
import uuid
from datetime import date, datetime, timedelta

from .domain import BusinessError, checkout_date, fail, text, utcnow, version


OPEN_STATES = ("pending", "quoted", "confirmed", "processing")
MAX_AMOUNT_MINOR = 9_007_199_254_740_991  # also exactly representable by JSON/JS clients


class StayExtensions:
    def __init__(self, store):
        self.store = store

    def _find(self, db, p, request_id):
        row = self.store._find(db, p, request_id)
        if row["request_type"] != "stay_extension":
            fail("VALIDATION_ERROR", "该工单不是续房申请")
        return row

    @staticmethod
    def _details(db, request_id):
        return db.execute("SELECT * FROM stay_extension_details WHERE request_id=?", (request_id,)).fetchone()

    @staticmethod
    def _stay(db, row, details, quote=None):
        stay = db.execute("SELECT s.* FROM stays s JOIN rooms r ON r.hotel=s.hotel AND r.room=s.room "
                          "WHERE s.id=? AND s.hotel=? AND s.room=? AND s.status='active' AND r.enabled=1",
                          (row["stay_id"], row["hotel"], row["room"])).fetchone()
        if not stay:
            fail("STAY_INACTIVE", "入住已结束或房间停用，不能继续办理续房")
        if stay["expected_checkout_date"] != details["original_checkout_date"]:
            fail("STAY_CHANGED", "入住退房日期已变化，请前台核实后重新申请")
        if quote and (stay["version"] != quote["base_stay_version"] or stay["expected_checkout_date"] != quote["base_checkout_date"]):
            fail("STAY_CHANGED", "入住信息已变化，请前台重新核实报价")
        return stay

    @staticmethod
    def _public_quote(quote):
        result = dict(quote)
        result.pop("actor_principal_id")
        return result

    def detail(self, db, row):
        detail = dict(self._details(db, row["id"]))
        quote = db.execute("SELECT * FROM stay_extension_quotes WHERE id=?", (detail["current_quote_id"],)).fetchone()
        detail["current_quote"] = self._public_quote(quote) if quote else None
        detail["quote_status"] = "none"
        if quote:
            if row["status"] in ("confirmed", "processing", "completed"):
                detail["quote_status"] = "confirmed"
            elif row["status"] == "quoted":
                detail["quote_status"] = "expired" if utcnow() >= quote["expires_at"] else "valid"
            else:
                detail["quote_status"] = "inactive"
        detail["available_actions"] = {"room": [], "frontdesk": []}
        if row["status"] in OPEN_STATES:
            # Hints reflect current eligibility; writes always validate again in their transaction.
            room_actions = ["cancel"] if row["status"] in ("pending", "quoted") else []
            desk_actions = ["add_note"]
            if row["status"] in ("pending", "quoted", "confirmed"):
                desk_actions.append("cancel")
            if row["status"] in ("pending", "quoted"):
                desk_actions.append("reject")
            try:
                self._stay(db, row, detail)
            except BusinessError:
                room_actions = []
            else:
                if row["status"] in ("pending", "quoted", "confirmed"):
                    desk_actions.append("quote")
                if quote:
                    try:
                        self._stay(db, row, detail, quote)
                    except BusinessError:
                        pass
                    else:
                        if row["status"] == "quoted" and detail["quote_status"] == "valid":
                            room_actions.append("confirm")
                        elif row["status"] == "confirmed":
                            desk_actions.append("begin")
                        elif row["status"] == "processing":
                            desk_actions.extend(["finish_success", "finish_failed"])
            detail["available_actions"] = {"room": room_actions, "frontdesk": desk_actions}
        return detail

    def _event(self, db, p, row, message, action, audience=None):
        detail = self._details(db, row["id"])
        extra = {"target_checkout_date": detail["requested_checkout_date"]}
        if detail["current_quote_id"]:
            quote = db.execute("SELECT * FROM stay_extension_quotes WHERE id=?", (detail["current_quote_id"],)).fetchone()
            extra.update({"quote_id": quote["id"], "quote_revision": quote["revision"],
                          "total_amount_minor": quote["total_amount_minor"], "currency": quote["currency"]})
        role = audience or ("frontdesk" if p.role == "room" else "room")
        self.store.emit(db, p, role, "" if role == "frontdesk" else row["room"], row, message, action, extra)

    def _transition(self, db, p, row, target, action, note, data=None, audience=None):
        db.execute("UPDATE requests SET status=?,note=?,version=version+1,updated_at=? WHERE id=?",
                   (target, note, utcnow(), row["id"]))
        updated = self.store._find(db, p, row["id"])
        self.store._record(db, p, updated, action, row["status"], note, data)
        labels = {"quote": "续房报价已发布，请核对后确认；尚未办理续房", "confirm": "住客已确认续房报价，等待前台办理",
                  "begin": "续房正在由前台办理", "finish_success": "前台确认续房已办理成功", "finish_failed": "续房办理失败",
                  "reject": "续房申请已拒绝", "cancel": "续房申请已取消", "add_note": "续房处理进展已更新"}
        summary = labels[action]
        if action == "finish_success":
            summary += f"，预计退房日期已更新为{data['target_checkout_date']}"
        self._event(db, p, updated, f"{row['room']}号客房{summary}。{note}", action, audience)
        return updated

    def submit(self, p, target_checkout_date, request_key, note=""):
        self.store._room(p)
        target_checkout_date = checkout_date(target_checkout_date)
        request_key, note = text(request_key, "request_key", 80), text(note, "note", 300, True)
        def perform(db):
            old = db.execute("SELECT * FROM requests WHERE hotel=? AND stay_id=? AND request_key=?",
                             (p.hotel, p.stay_id, request_key)).fetchone()
            if old:
                detail = self._details(db, old["id"]) if old["request_type"] == "stay_extension" else None
                if not detail or detail["requested_checkout_date"] != target_checkout_date or detail["application_note"] != note:
                    fail("IDEMPOTENCY_CONFLICT", "request_key 已用于其他工单或参数")
                return {"request": self.store._detail(db, old), "replayed": True}
            stay = self.store._authorize(db, p)
            if target_checkout_date <= stay["expected_checkout_date"]:
                fail("VALIDATION_ERROR", "目标退房日期必须晚于当前预计退房日期")
            if db.execute("SELECT 1 FROM requests WHERE hotel=? AND stay_id=? AND request_type='stay_extension' "
                          "AND status IN ('pending','quoted','confirmed','processing')", (p.hotel, p.stay_id)).fetchone():
                fail("ACTIVE_EXTENSION_EXISTS", "当前入住已有未结案续房申请，请先查询处理")
            request_id, now = uuid.uuid4().hex[:16], utcnow()
            description = f"申请从{stay['expected_checkout_date']}续住至{target_checkout_date}"
            db.execute("INSERT INTO requests(id,hotel,room,stay_id,request_key,request_type,description,note,created_at,updated_at) "
                       "VALUES(?,?,?,?,?,'stay_extension',?,?,?,?)",
                       (request_id, p.hotel, p.room, p.stay_id, request_key, description, note, now, now))
            db.execute("INSERT INTO stay_extension_details(request_id,original_checkout_date,requested_checkout_date,application_note) VALUES(?,?,?,?)",
                       (request_id, stay["expected_checkout_date"], target_checkout_date, note))
            row = self.store._find(db, p, request_id)
            self.store._record(db, p, row, "submit", None, note, {"target_checkout_date": target_checkout_date})
            self._event(db, p, row, f"{p.room}号客房{description}，请前台人工核实房态及报价。{note}", "submit")
            return {"request": self.store._detail(db, row)}
        return self.store._write(p, "submit_stay_extension", request_key, [target_checkout_date, note], perform)

    def quote(self, p, request_id, total_amount_minor, pricing_note, expected_version, request_key,
              valid_for_minutes=30, change_reason=""):
        self.store._desk(p)
        if type(total_amount_minor) is not int or not 0 <= total_amount_minor <= MAX_AMOUNT_MINOR:
            fail("VALIDATION_ERROR", "总金额必须是安全范围内的非负整数分")
        if type(valid_for_minutes) is not int or not 1 <= valid_for_minutes <= 1440:
            fail("VALIDATION_ERROR", "报价有效期必须为1～1440分钟")
        pricing_note = text(pricing_note, "pricing_note", 500)
        change_reason, expected_version = text(change_reason, "change_reason", 500, True), version(expected_version)
        def perform(db):
            row = self._find(db, p, request_id)
            self.store._check_version(row, expected_version)
            if row["status"] not in ("pending", "quoted", "confirmed"):
                fail("INVALID_TRANSITION", "当前状态不允许报价")
            detail = self._details(db, request_id)
            stay = self._stay(db, row, detail)
            if detail["current_quote_id"] and not change_reason:
                fail("VALIDATION_ERROR", "修订报价必须填写变更原因，住客需要重新确认")
            revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM stay_extension_quotes WHERE request_id=?", (request_id,)).fetchone()[0]
            quote_id, now = uuid.uuid4().hex, utcnow()
            expires = (datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(minutes=valid_for_minutes)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            nights = (date.fromisoformat(detail["requested_checkout_date"]) - date.fromisoformat(stay["expected_checkout_date"])).days
            db.execute("INSERT INTO stay_extension_quotes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (quote_id, request_id, revision, stay["expected_checkout_date"], stay["version"],
                        detail["requested_checkout_date"], nights, total_amount_minor, "CNY", pricing_note, expires, now, p.actor_id, change_reason))
            db.execute("UPDATE stay_extension_details SET current_quote_id=?,confirmed_quote_id=NULL,confirmed_at=NULL WHERE request_id=?",
                       (quote_id, request_id))
            amount = f"{total_amount_minor // 100}.{total_amount_minor % 100:02d}"
            summary = f"续住至{detail['requested_checkout_date']}，共{nights}晚，合计人民币{amount}元。{pricing_note}。报价有效至{expires}。"
            data = {"quote_id": quote_id, "quote_revision": revision, "total_amount_minor": total_amount_minor,
                    "currency": "CNY", "target_checkout_date": detail["requested_checkout_date"], "change_reason": change_reason}
            updated = self._transition(db, p, row, "quoted", "quote", summary, data)
            return {"request": self.store._detail(db, updated)}
        return self.store._write(p, "quote_stay_extension", request_key,
                                 [request_id, total_amount_minor, pricing_note, expected_version, valid_for_minutes, change_reason], perform)

    def _quoted_operation(self, p, request_id, quote_id, expected_version, request_key, action, note="", outcome=None, result_reference=""):
        quote_id, expected_version = text(quote_id, "quote_id", 80), version(expected_version)
        def perform(db):
            row = self._find(db, p, request_id)
            self.store._check_version(row, expected_version)
            required = {"confirm": "quoted", "begin": "confirmed", "finish": "processing"}[action]
            if row["status"] != required:
                fail("INVALID_TRANSITION", f"此操作要求状态为 {required}")
            detail = self._details(db, request_id)
            if quote_id != detail["current_quote_id"] or (action != "confirm" and quote_id != detail["confirmed_quote_id"]):
                fail("QUOTE_SUPERSEDED", "该报价不是当前待确认或已确认的报价")
            quote = db.execute("SELECT * FROM stay_extension_quotes WHERE id=? AND request_id=?", (quote_id, request_id)).fetchone()
            stay = self._stay(db, row, detail, quote)
            now = utcnow()
            if action == "confirm":
                if now >= quote["expires_at"]:
                    fail("QUOTE_EXPIRED", "报价已过期，请前台重新报价")
                db.execute("UPDATE stay_extension_details SET confirmed_quote_id=?,confirmed_at=? WHERE request_id=?", (quote_id, now, request_id))
                target, event_action = "confirmed", "confirm"
            elif action == "begin":
                db.execute("UPDATE stay_extension_details SET processing_started_at=? WHERE request_id=?", (now, request_id))
                target, event_action = "processing", "begin"
            else:
                if outcome == "success":
                    db.execute("UPDATE stays SET expected_checkout_date=?,version=version+1 WHERE id=?",
                               (quote["target_checkout_date"], stay["id"]))
                db.execute("UPDATE stay_extension_details SET result_at=?,result_note=?,result_reference=? WHERE request_id=?",
                           (now, note, result_reference, request_id))
                target = "completed" if outcome == "success" else "failed"
                event_action = "finish_success" if outcome == "success" else "finish_failed"
            data = {"quote_id": quote_id, "quote_revision": quote["revision"], "total_amount_minor": quote["total_amount_minor"],
                    "currency": quote["currency"], "target_checkout_date": quote["target_checkout_date"]}
            if action == "finish":
                data.update({"outcome": outcome, "result_reference": result_reference})
            updated = self._transition(db, p, row, target, event_action, note, data)
            result = {"request": self.store._detail(db, updated)}
            if action == "finish":
                result["stay"] = dict(db.execute("SELECT * FROM stays WHERE id=?", (stay["id"],)).fetchone())
            return result
        return self.store._write(p, action + "_stay_extension", request_key,
                                 [request_id, quote_id, expected_version, note, outcome, result_reference], perform)

    def confirm(self, p, request_id, quote_id, expected_version, request_key):
        self.store._room(p)
        return self._quoted_operation(p, request_id, quote_id, expected_version, request_key, "confirm")

    def begin(self, p, request_id, quote_id, note, expected_version, request_key):
        self.store._desk(p)
        return self._quoted_operation(p, request_id, quote_id, expected_version, request_key, "begin", text(note, "note", 500))

    def finish(self, p, request_id, quote_id, outcome, note, expected_version, request_key, result_reference=""):
        self.store._desk(p)
        if outcome not in ("success", "failed"):
            fail("VALIDATION_ERROR", "outcome 必须为 success 或 failed")
        return self._quoted_operation(p, request_id, quote_id, expected_version, request_key, "finish", text(note, "note", 500),
                                      outcome, text(result_reference, "result_reference", 160, True))

    def reject(self, p, request_id, reason, expected_version, request_key):
        self.store._desk(p)
        reason, expected_version = text(reason, "reason", 500), version(expected_version)
        def perform(db):
            row = self._find(db, p, request_id)
            self.store._check_version(row, expected_version)
            if row["status"] not in ("pending", "quoted"):
                fail("INVALID_TRANSITION", "只有待核实或待确认的续房可以拒绝")
            return {"request": self.store._detail(db, self._transition(db, p, row, "rejected", "reject", reason))}
        return self.store._write(p, "reject_stay_extension", request_key, [request_id, reason, expected_version], perform)

    def common_change(self, db, p, row, action, note):
        if action == "cancel":
            allowed = ("pending", "quoted", "confirmed") if p.role == "frontdesk" else ("pending", "quoted")
            if row["status"] not in allowed:
                fail("INVALID_TRANSITION", "当前续房不能取消；办理中请前台先核实实际结果")
            return self._transition(db, p, row, "cancelled", action, note)
        if action == "add_note" and p.role == "frontdesk" and row["status"] in OPEN_STATES:
            return self._transition(db, p, row, row["status"], action, note)
        fail("INVALID_TRANSITION", "续房必须使用专属报价、确认和办理工具，通用接口仅可追加说明或按规则取消")

    def on_checkout(self, db, p, stay_id, reason):
        rows = db.execute("SELECT * FROM requests WHERE hotel=? AND stay_id=? AND request_type='stay_extension' "
                          "AND status IN ('pending','quoted','confirmed','processing')", (p.hotel, stay_id)).fetchall()
        if any(row["status"] == "processing" for row in rows):
            fail("STAY_HAS_PROCESSING_EXTENSION", "续房正在办理，请先核实办理结果再退房")
        for row in rows:
            self._transition(db, p, row, "cancelled", "cancel", "入住结束，取消尚未办理的续房：" + reason,
                             {"reason_code": "stay_closed"}, audience="frontdesk")
        return len(rows)

    def list_quotes(self, p, request_id, cursor=None, limit=20):
        self.store._limit(limit)
        context = self.store._cursor_context(p, "extension_quotes", [request_id])
        with self.store.connect() as db:
            self.store._authorize(db, p)
            self._find(db, p, request_id)
            if cursor is not None:
                upper, last = self.store._decode_cursor(cursor, context)
            else:
                upper = db.execute("SELECT COALESCE(MAX(revision),0) FROM stay_extension_quotes WHERE request_id=?", (request_id,)).fetchone()[0]
                last = upper + 1
            rows = db.execute("SELECT * FROM stay_extension_quotes WHERE request_id=? AND revision<=? AND revision<? ORDER BY revision DESC LIMIT ?",
                              (request_id, upper, last, limit + 1)).fetchall()
            return {"quotes": [dict(row) if p.role == "frontdesk" else self._public_quote(row) for row in rows[:limit]],
                    "next_cursor": self.store._encode_cursor(context, upper, rows[limit-1]["revision"]) if len(rows) > limit else None}
