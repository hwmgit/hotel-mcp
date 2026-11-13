"""Database-backed web accounts, revocable sessions and scoped administration.

MCP credentials are machine identities; they never become web passwords/admins.
"""
import argparse
import getpass
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from collections import OrderedDict
from threading import Lock

from jsonschema import ValidationError, validate

from .domain import Principal, canonical, fail, text, utcnow, version
from .sales import display_store_name
from .toolkit import dispatch, tools_for

ADMIN_ROLES = ("super_admin", "store_admin")
ROLES = (*ADMIN_ROLES, "frontdesk", "customer")
HISTORY_TOOLS = {"hotel_query_requests", "hotel_list_requests", "hotel_get_request",
                 "hotel_list_request_actions", "hotel_list_stay_extension_quotes"}
SALES_TOOLS = {"hotel_list_stores", "hotel_query_sales", "hotel_resolve_entities"}
SESSION_SECONDS = 7 * 24 * 3600
COOKIE = "hotel_session"


def password_hash(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        fail("VALIDATION_ERROR", "密码长度须为8～128个字符")
    salt = secrets.token_hex(16)
    derived = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${derived}"


def password_matches(password, encoded):
    if not isinstance(password, str) or len(password) > 128:
        return False
    _, salt, expected = encoded.split("$")
    derived = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(derived, expected)


def username(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{2,63}", value):
        fail("VALIDATION_ERROR", "账号须为3～64位字母、数字、点、横线或下划线")
    return value.lower()


def public(row):
    return {k: row[k] for k in ("id", "username", "display_name", "role", "hotel", "enabled",
                                "must_change_password", "version", "created_at", "updated_at")}


class Accounts:
    def __init__(self, store):
        self.store = store
        self._dummy = password_hash(secrets.token_urlsafe(24))
        self._attempts = OrderedDict()
        self._attempt_lock = Lock()

    def _digest(self, value):
        return hmac.new(self.store.cursor_secret, value.encode(), hashlib.sha256).hexdigest()

    def bootstrap(self, login, password, display_name="总管理员"):
        """Local operator only, one-time creation. There is no public setup route."""
        login, encoded = username(login), password_hash(password)
        name = text(display_name, "姓名", 80)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
                fail("ALREADY_INITIALIZED", "账号系统已初始化，请由总管理员在页面添加账号")
            ident, now = uuid.uuid4().hex, utcnow()
            db.execute("INSERT INTO accounts(id,username,display_name,role,hotel,password_hash,created_at,updated_at) "
                       "VALUES(?,?,?,'super_admin','',?,?,?)", (ident, login, name, encoded, now, now))
            self._audit(db, ident, ident, "bootstrap")
            return public(db.execute("SELECT * FROM accounts WHERE id=?", (ident,)).fetchone())

    def login(self, login, password, peer):
        # Bounded, per-process rate limit fits the existing single-process deployment.
        # Charge attempts before hashing so parallel requests cannot evade the limit.
        now = int(time.time())
        normalized = login.lower() if isinstance(login, str) else ""
        keys = (self._digest("login:" + normalized[:128]), self._digest("peer:" + peer))
        with self._attempt_lock:
            for key, limit in zip(keys, (10, 60)):
                attempts = [stamp for stamp in self._attempts.get(key, []) if stamp > now - 300]
                if len(attempts) >= limit:
                    fail("RATE_LIMITED", "尝试次数过多，请5分钟后重试")
                self._attempts[key] = attempts + [now]
                self._attempts.move_to_end(key)
            while len(self._attempts) > 4096:
                self._attempts.popitem(last=False)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE username=?", (normalized[:128],)).fetchone()
        valid = password_matches(password, row["password_hash"] if row else self._dummy)
        if not row or not valid or not row["enabled"]:
            fail("UNAUTHORIZED", "账号或密码不正确，或账号已停用")
        token = secrets.token_urlsafe(32)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._live(db, row)
            db.execute("DELETE FROM web_sessions WHERE expires_at<=?", (now,))
            db.execute("INSERT INTO web_sessions VALUES(?,?,?,?)",
                       (hashlib.sha256(token.encode()).hexdigest(), row["id"], row["auth_version"], now + SESSION_SECONDS))
        with self._attempt_lock:
            self._attempts.pop(keys[0], None)
        return token

    def authenticate(self, token):
        if not isinstance(token, str) or not 20 <= len(token) <= 128:
            fail("UNAUTHORIZED", "请登录工作台")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.store.connect() as db:
            row = db.execute("SELECT a.* FROM accounts a JOIN web_sessions s ON s.account_id=a.id "
                             "WHERE s.token_hash=? AND s.expires_at>? AND a.enabled=1 "
                             "AND s.auth_version=a.auth_version", (digest, int(time.time()))).fetchone()
        if not row:
            fail("UNAUTHORIZED", "登录已失效，请重新登录")
        return dict(row)

    def csrf(self, token):
        return self._digest("csrf:" + token)

    def logout(self, token):
        with self.store.connect() as db:
            db.execute("DELETE FROM web_sessions WHERE token_hash=?", (hashlib.sha256(token.encode()).hexdigest(),))

    @staticmethod
    def _live(db, actor):
        row = db.execute("SELECT * FROM accounts WHERE id=?", (actor["id"],)).fetchone()
        if not row or not row["enabled"] or row["auth_version"] != actor["auth_version"]:
            fail("UNAUTHORIZED", "登录已失效，请重新登录")
        return row

    @staticmethod
    def _admin(actor):
        if actor["role"] not in ADMIN_ROLES:
            fail("FORBIDDEN", "仅管理员可管理账号")

    @staticmethod
    def _target(actor, row):
        if not row or (actor["role"] != "super_admin" and
                       (row["hotel"] != actor["hotel"] or row["role"] not in ("frontdesk", "customer"))):
            fail("NOT_FOUND", "找不到可管理的账号")

    @staticmethod
    def _audit(db, actor, target, action):
        db.execute("INSERT INTO account_audit VALUES(?,?,?,?,?)", (uuid.uuid4().hex, actor, target, action, utcnow()))

    def _write(self, actor, body, action, target, perform, *, allow_password_change=False):
        if not isinstance(body, dict):
            fail("VALIDATION_ERROR", "请求须为对象")
        key = text(body.get("request_key"), "request_key", 80)
        digest = self._digest(canonical([action, target, body]))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            live = self._live(db, actor)
            if live["must_change_password"] and not allow_password_change:
                fail("PASSWORD_CHANGE_REQUIRED", "请先修改初始密码")
            receipt = db.execute("SELECT * FROM account_operations WHERE actor_id=? AND request_key=?",
                                 (actor["id"], key)).fetchone()
            if receipt:
                if receipt["payload_hash"] != digest:
                    fail("IDEMPOTENCY_CONFLICT", "请求编号已用于其他操作")
                return {**json.loads(receipt["response_json"]), "replayed": True}
            result = perform(db)
            result["replayed"] = False
            self._audit(db, actor["id"], target or result["account"]["id"], action)
            db.execute("INSERT INTO account_operations VALUES(?,?,?,?,?)",
                       (actor["id"], key, digest, canonical(result), utcnow()))
            return result

    def change_password(self, actor, body):
        if not isinstance(body, dict) or set(body) != {"current_password", "password", "request_key"}:
            fail("VALIDATION_ERROR", "请填写当前密码与新密码")
        if not password_matches(body["current_password"], actor["password_hash"]):
            fail("VALIDATION_ERROR", "当前密码不正确")
        if body["current_password"] == body["password"]:
            fail("VALIDATION_ERROR", "新密码不能与当前密码相同")
        encoded = password_hash(body["password"])
        def perform(db):
            db.execute("UPDATE accounts SET password_hash=?,must_change_password=0,auth_version=auth_version+1,"
                       "version=version+1,updated_at=? WHERE id=?", (encoded, utcnow(), actor["id"]))
            db.execute("DELETE FROM web_sessions WHERE account_id=?", (actor["id"],))
            return {"changed": True}
        return self._write(actor, body, "change_password", actor["id"], perform, allow_password_change=True)

    def stores(self, actor):
        with self.store.connect() as db:
            rows = db.execute("SELECT hotel,name FROM stores ORDER BY hotel").fetchall() if actor["role"] == "super_admin" else db.execute(
                "SELECT hotel,name FROM stores WHERE hotel=?", (actor["hotel"],)).fetchall()
        return [{"hotel": row["hotel"], "name": display_store_name(row["name"])} for row in rows]

    def context(self, actor, hotel="", stay_id=""):
        stores = self.stores(actor)
        hotels = [s["hotel"] for s in stores]
        if hotel and hotel not in hotels:
            fail("FORBIDDEN", "无权访问此门店")
        kwargs = {"principal_id": "account-" + actor["id"], "account_id": actor["id"],
                  "auth_version": actor["auth_version"], "account_role": actor["role"]}
        stays = []
        if actor["role"] == "customer":
            with self.store.connect() as db:
                stays = [dict(r) for r in db.execute(
                    "SELECT s.id,s.hotel,s.room,s.status,s.check_in_at,s.expected_checkout_date FROM stays s "
                    "JOIN account_stays a ON a.stay_id=s.id WHERE a.account_id=? "
                    "ORDER BY s.check_in_at DESC,s.id DESC", (actor["id"],))]
            selected = next((s for s in stays if s["id"] == stay_id), None) if stay_id else next(
                (s for s in stays if s["status"] == "active"), stays[0] if stays else None)
            if stay_id and not selected:
                fail("FORBIDDEN", "无权访问此入住记录")
            p = Principal(selected["hotel"], "room", selected["room"], selected["id"],
                          history_readonly=selected["status"] != "active", **kwargs) if selected else None
        elif actor["role"] == "super_admin" and not hotel:
            p = Principal("", "manager", allowed_hotels=tuple(hotels), **kwargs) if hotels else None
        else:
            selected_hotel = hotel if actor["role"] == "super_admin" else actor["hotel"]
            p = Principal(selected_hotel, "frontdesk", **kwargs)
        schemas = tools_for(p) if p else []
        if actor["role"] == "frontdesk":
            schemas = [t for t in schemas if t.name not in SALES_TOOLS]
        # The old general-request shortcuts lack request_key/version. Web users
        # use the transactional, versioned hotel_process_request contract.
        schemas = [t for t in schemas if t.name not in {"hotel_accept_request", "hotel_complete_request"}]
        if p and p.history_readonly:
            schemas = [t for t in schemas if t.name in HISTORY_TOOLS]
        if actor["must_change_password"]:
            schemas = []
        return p, schemas, stores, stays

    def session(self, actor, hotel="", stay_id="", *, connections=False):
        p, schemas, stores, stays = self.context(actor, hotel, stay_id)
        return {"account": public(actor), "principal": {"id": "account-" + actor["id"],
                "role": p.role if p else "room" if actor["role"] == "customer" else "manager",
                "hotel": p.hotel if p else "", "room": p.room if p else "", "stay_id": p.stay_id if p else ""},
                "tools": [t.name for t in schemas], "stores": stores, "stays": stays,
                "history_readonly": bool(p and p.history_readonly),
                "capabilities": {"manage_accounts": actor["role"] in ADMIN_ROLES and not actor["must_change_password"],
                                 "manage_connections": connections and actor["role"] in ADMIN_ROLES
                                 and bool(p and p.hotel) and not actor["must_change_password"]}}

    def execute(self, actor, hotel, stay_id, name, arguments):
        p, schemas, _, _ = self.context(actor, hotel, stay_id)
        schema = next((t.inputSchema for t in schemas if t.name == name), None)
        if not schema:
            fail("FORBIDDEN", "当前角色或门店范围无权执行此操作")
        try:
            validate(arguments, schema)
        except ValidationError:
            fail("VALIDATION_ERROR", "请检查必填项、类型和范围")
        self.store.authorize(p)
        return dispatch(self.store, p, name, arguments)

    def list_accounts(self, actor, hotel="", offset=0, query=""):
        self._admin(actor)
        if hotel and hotel not in {s["hotel"] for s in self.stores(actor)}:
            fail("FORBIDDEN", "无权访问此门店")
        where, args = ["1=1"], []
        if actor["role"] == "store_admin":
            where.append("hotel=? AND role IN ('frontdesk','customer')")
            args.append(actor["hotel"])
        elif hotel:
            where.append("hotel=?")
            args.append(hotel)
        if query:
            where.append("(username LIKE ? OR display_name LIKE ?)")
            args.extend(["%" + query[:80] + "%"] * 2)
        with self.store.connect() as db:
            rows = db.execute(f"SELECT * FROM accounts WHERE {' AND '.join(where)} ORDER BY created_at,id LIMIT 51 OFFSET ?",
                              (*args, offset)).fetchall()
        return {"accounts": [public(r) for r in rows[:50]], "next_offset": offset + 50 if len(rows) > 50 else None}

    def stay_options(self, actor, target, offset=0):
        self._admin(actor)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (target,)).fetchone()
            self._target(actor, row)
            if row["role"] != "customer":
                fail("VALIDATION_ERROR", "只有客户账号可以关联入住")
            bound = db.execute("SELECT s.id,s.room,s.status,s.check_in_at FROM stays s JOIN account_stays a ON a.stay_id=s.id "
                               "WHERE a.account_id=? ORDER BY s.check_in_at DESC", (target,)).fetchall()
            available = db.execute("SELECT s.id,s.room,s.status,s.check_in_at FROM stays s WHERE s.hotel=? "
                                   "AND NOT EXISTS (SELECT 1 FROM account_stays a WHERE a.stay_id=s.id) "
                                   "ORDER BY s.check_in_at DESC,s.id DESC LIMIT 51 OFFSET ?", (row["hotel"], offset)).fetchall()
        return {"bound": [dict(s) for s in bound], "available": [dict(s) for s in available[:50]],
                "next_offset": offset + 50 if len(available) > 50 else None, "version": row["version"]}

    def mutate(self, actor, body, target="", action="create"):
        self._admin(actor)
        allowed = {"create": {"username", "display_name", "role", "hotel", "password", "request_key"},
                   "update": {"display_name", "role", "hotel", "enabled", "expected_version", "request_key"},
                   "reset_password": {"password", "expected_version", "request_key"},
                   "bind_stay": {"stay_id", "expected_version", "request_key"}}
        if action not in allowed or not isinstance(body, dict) or set(body) != allowed[action]:
            fail("VALIDATION_ERROR", "账号操作参数不完整或含未知字段")
        encoded = password_hash(body["password"]) if "password" in body else None
        expected = version(body["expected_version"]) if action != "create" else None
        if action in ("create", "update"):
            name = text(body["display_name"], "姓名", 80)
            new_role, hotel = body["role"], body["hotel"]
            if not isinstance(new_role, str) or new_role not in ROLES or not isinstance(hotel, str):
                fail("VALIDATION_ERROR", "请选择有效角色和门店")
            if actor["role"] != "super_admin" and (new_role not in ("frontdesk", "customer") or hotel != actor["hotel"]):
                fail("FORBIDDEN", "门店管理员只能维护本店前台和客户账号")
            if new_role == "super_admin":
                if hotel:
                    fail("VALIDATION_ERROR", "总管理员不绑定单个门店")
            elif hotel not in {s["hotel"] for s in self.stores(actor)}:
                fail("FORBIDDEN", "请选择可管理的门店")
        if action == "create":
            login = username(body["username"])
        if action == "update" and type(body["enabled"]) is not bool:
            fail("VALIDATION_ERROR", "启用状态须为布尔值")

        def perform(db):
            now = utcnow()
            if action == "create":
                if db.execute("SELECT 1 FROM accounts WHERE username=?", (login,)).fetchone():
                    fail("USERNAME_EXISTS", "账号名称已被使用")
                ident = uuid.uuid4().hex
                db.execute("INSERT INTO accounts(id,username,display_name,role,hotel,password_hash,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?)", (ident, login, name, new_role, hotel, encoded, now, now))
            else:
                ident = target
                row = db.execute("SELECT * FROM accounts WHERE id=?", (ident,)).fetchone()
                self._target(actor, row)
                if row["version"] != expected:
                    fail("VERSION_CONFLICT", "账号已更新，请刷新后重试")
                if action == "bind_stay":
                    stay_id = text(body["stay_id"], "入住编号", 80)
                    if row["role"] != "customer" or not db.execute("SELECT 1 FROM stays WHERE id=? AND hotel=?",
                                                                 (stay_id, row["hotel"])).fetchone():
                        fail("NOT_FOUND", "找不到可关联的入住")
                    if db.execute("SELECT 1 FROM account_stays WHERE stay_id=?", (stay_id,)).fetchone():
                        fail("VERSION_CONFLICT", "此入住已关联客户，不能重新分配历史工单")
                    db.execute("INSERT INTO account_stays VALUES(?,?,?)", (ident, stay_id, now))
                    db.execute("UPDATE accounts SET version=version+1,updated_at=? WHERE id=?", (now, ident))
                elif action == "reset_password":
                    db.execute("UPDATE accounts SET password_hash=?,must_change_password=1,auth_version=auth_version+1,"
                               "version=version+1,updated_at=? WHERE id=?", (encoded, now, ident))
                else:
                    if ident == actor["id"] and (not body["enabled"] or new_role != row["role"] or hotel != row["hotel"]):
                        fail("FORBIDDEN", "不能停用自己或修改自己的角色、门店")
                    if row["role"] == "super_admin" and row["enabled"] and (new_role != "super_admin" or not body["enabled"]):
                        if db.execute("SELECT COUNT(*) FROM accounts WHERE role='super_admin' AND enabled=1").fetchone()[0] <= 1:
                            fail("FORBIDDEN", "必须保留至少一位有效总管理员")
                    if (new_role != row["role"] or hotel != row["hotel"]) and db.execute(
                            "SELECT 1 FROM account_stays WHERE account_id=?", (ident,)).fetchone():
                        fail("FORBIDDEN", "已关联入住的客户须保留原角色和门店，以保护历史归属")
                    db.execute("UPDATE accounts SET display_name=?,role=?,hotel=?,enabled=?,auth_version=auth_version+1,"
                               "version=version+1,updated_at=? WHERE id=?",
                               (name, new_role, hotel, int(body["enabled"]), now, ident))
                if action != "bind_stay":
                    db.execute("DELETE FROM web_sessions WHERE account_id=?", (ident,))
            return {"account": public(db.execute("SELECT * FROM accounts WHERE id=?", (ident,)).fetchone())}
        return self._write(actor, body, action, target, perform)


def main():
    parser = argparse.ArgumentParser(description="新建首位总管理员；无公开注册入口")
    parser.add_argument("--database", required=True, help="mysql 或明确的本地数据库路径，须先迁移")
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", default="总管理员")
    args = parser.parse_args()
    from .store import HotelStore
    password = getpass.getpass("设置初始密码（8～128位，隐藏输入）：")
    if password != getpass.getpass("再次输入："):
        parser.error("两次密码不一致")
    Accounts(HotelStore(args.database)).bootstrap(args.username, password, args.display_name)
    print("首位总管理员已创建；首次登录须修改密码。")


if __name__ == "__main__":
    main()
