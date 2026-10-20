"""Explicit MySQL 8.0.16+ schema. No SQLite database or credentials are imported."""
import hashlib
import secrets
import sqlite3

import pymysql

from .mysql import raw_connect


VERSION = 6
SCHEMA = [
"""CREATE TABLE IF NOT EXISTS service_metadata (
 `key` VARCHAR(80) PRIMARY KEY, value LONGTEXT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS schema_migrations (
 version INT PRIMARY KEY, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
"""CREATE TABLE IF NOT EXISTS hotel_write_lock (id INT PRIMARY KEY)""",
"""CREATE TABLE IF NOT EXISTS rooms (
 hotel VARCHAR(80) NOT NULL, room VARCHAR(80) NOT NULL, enabled INT NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 PRIMARY KEY(hotel,room))""",
"""CREATE TABLE IF NOT EXISTS stays (
 id VARCHAR(80) PRIMARY KEY, hotel VARCHAR(80) NOT NULL, room VARCHAR(80) NOT NULL,
 status VARCHAR(32) NOT NULL CHECK(status IN ('active','closed')),
 check_in_at VARCHAR(32) NOT NULL, expected_checkout_date VARCHAR(10) NOT NULL, closed_at VARCHAR(32),
 close_reason VARCHAR(500) NOT NULL DEFAULT '', version INT NOT NULL DEFAULT 1 CHECK(version>0),
 active_room VARCHAR(80) GENERATED ALWAYS AS (IF(status='active',room,NULL)) STORED,
 UNIQUE KEY one_active_stay(hotel,active_room), UNIQUE KEY stay_scope(hotel,room,id),
 FOREIGN KEY(hotel,room) REFERENCES rooms(hotel,room))""",
"""CREATE TABLE IF NOT EXISTS requests (
 seq BIGINT PRIMARY KEY AUTO_INCREMENT, id VARCHAR(80) NOT NULL UNIQUE,
 hotel VARCHAR(80) NOT NULL, room VARCHAR(80) NOT NULL, stay_id VARCHAR(80),
 request_key VARCHAR(80) NOT NULL,
 request_type VARCHAR(32) NOT NULL DEFAULT 'general'
 CHECK(request_type IN ('general','repair','stay_extension','meal','gym_booking','meeting_booking')),
 description TEXT NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'pending'
 CHECK(status IN ('pending','accepted','completed','rejected','cancelled','closed_unresolved','quoted','confirmed','processing','failed')),
 note TEXT NOT NULL DEFAULT (''), created_at VARCHAR(32) NOT NULL, updated_at VARCHAR(32) NOT NULL,
 version INT NOT NULL DEFAULT 1 CHECK(version>0),
 legacy_room VARCHAR(80) GENERATED ALWAYS AS (IF(stay_id IS NULL,room,NULL)) STORED,
 open_extension VARCHAR(80) GENERATED ALWAYS AS
 (IF(request_type='stay_extension' AND status IN ('pending','quoted','confirmed','processing'),stay_id,NULL)) STORED,
 UNIQUE KEY request_stay_key(hotel,stay_id,request_key),
 UNIQUE KEY request_legacy_key(hotel,legacy_room,request_key),
 UNIQUE KEY one_open_extension(hotel,open_extension),
 KEY request_scope(hotel,stay_id,seq), KEY request_worklist(hotel,status,request_type,seq),
 CHECK(request_type!='stay_extension' OR (stay_id IS NOT NULL AND status IN
 ('pending','quoted','confirmed','processing','completed','failed','rejected','cancelled'))),
 CHECK(request_type='stay_extension' OR status NOT IN ('quoted','confirmed','processing','failed')),
 CHECK(request_type NOT IN ('meal','gym_booking','meeting_booking') OR
 (stay_id IS NOT NULL AND status IN ('pending','accepted','completed','rejected','cancelled'))),
 FOREIGN KEY(hotel,room,stay_id) REFERENCES stays(hotel,room,id))""",
"""CREATE TABLE IF NOT EXISTS repair_details (
 request_id VARCHAR(80) PRIMARY KEY, category VARCHAR(32) NOT NULL
 CHECK(category IN ('air_conditioning','plumbing_electrical','network','door_lock','facilities','other')),
 location VARCHAR(80) NOT NULL DEFAULT '', urgency VARCHAR(16) NOT NULL CHECK(urgency IN ('normal','urgent')),
 related_request_id VARCHAR(80), FOREIGN KEY(request_id) REFERENCES requests(id),
 FOREIGN KEY(related_request_id) REFERENCES requests(id))""",
"""CREATE TABLE IF NOT EXISTS request_actions (
 id BIGINT PRIMARY KEY AUTO_INCREMENT, request_id VARCHAR(80) NOT NULL,
 action VARCHAR(40) NOT NULL, from_status VARCHAR(32), to_status VARCHAR(32) NOT NULL, note TEXT NOT NULL,
 actor_principal_id VARCHAR(180) NOT NULL, actor_role VARCHAR(16) NOT NULL, created_at VARCHAR(32) NOT NULL,
 request_version INT NOT NULL, data LONGTEXT NOT NULL,
 KEY action_request(request_id,id), FOREIGN KEY(request_id) REFERENCES requests(id))""",
"""CREATE TABLE IF NOT EXISTS operation_receipts (
 hotel VARCHAR(80) NOT NULL, principal_id VARCHAR(180) NOT NULL, stay_scope VARCHAR(80) NOT NULL,
 operation VARCHAR(80) NOT NULL, request_key VARCHAR(80) NOT NULL,
 payload_hash VARCHAR(64) NOT NULL, response_json LONGTEXT NOT NULL, created_at VARCHAR(32) NOT NULL,
 PRIMARY KEY(hotel,principal_id,stay_scope,operation,request_key))""",
"""CREATE TABLE IF NOT EXISTS events (
 seq BIGINT PRIMARY KEY AUTO_INCREMENT, hotel VARCHAR(80) NOT NULL, role VARCHAR(16) NOT NULL,
 room VARCHAR(80) NOT NULL, payload LONGTEXT NOT NULL, stay_id VARCHAR(80),
 KEY idx_events_scope(hotel,role,room,seq), KEY event_stay(hotel,role,stay_id,seq))""",
"""CREATE TABLE IF NOT EXISTS stay_extension_quotes (
 id VARCHAR(80) PRIMARY KEY, request_id VARCHAR(80) NOT NULL, revision INT NOT NULL CHECK(revision>0),
 base_checkout_date VARCHAR(10) NOT NULL, base_stay_version INT NOT NULL CHECK(base_stay_version>0),
 target_checkout_date VARCHAR(10) NOT NULL, nights INT NOT NULL CHECK(nights>0),
 total_amount_minor BIGINT NOT NULL CHECK(total_amount_minor>=0), currency VARCHAR(3) NOT NULL CHECK(currency='CNY'),
 pricing_note TEXT NOT NULL, expires_at VARCHAR(32) NOT NULL, created_at VARCHAR(32) NOT NULL,
 actor_principal_id VARCHAR(180) NOT NULL, change_reason VARCHAR(500) NOT NULL DEFAULT '',
 UNIQUE KEY quote_revision(request_id,revision), UNIQUE KEY quote_scope(request_id,id),
 FOREIGN KEY(request_id) REFERENCES requests(id))""",
"""CREATE TABLE IF NOT EXISTS stay_extension_details (
 request_id VARCHAR(80) PRIMARY KEY, original_checkout_date VARCHAR(10) NOT NULL,
 requested_checkout_date VARCHAR(10) NOT NULL,
 application_note VARCHAR(300) NOT NULL DEFAULT '', current_quote_id VARCHAR(80), confirmed_quote_id VARCHAR(80),
 confirmed_at VARCHAR(32), processing_started_at VARCHAR(32), result_at VARCHAR(32),
 result_note VARCHAR(500) NOT NULL DEFAULT '', result_reference VARCHAR(160) NOT NULL DEFAULT '',
 CHECK(requested_checkout_date>original_checkout_date),
 FOREIGN KEY(request_id) REFERENCES requests(id),
 FOREIGN KEY(request_id,current_quote_id) REFERENCES stay_extension_quotes(request_id,id),
 FOREIGN KEY(request_id,confirmed_quote_id) REFERENCES stay_extension_quotes(request_id,id))""",
"""CREATE TABLE IF NOT EXISTS stores (hotel VARCHAR(80) PRIMARY KEY, name VARCHAR(180) NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS demo_sales_orders (
 id VARCHAR(180) PRIMARY KEY, hotel VARCHAR(80) NOT NULL, business_date VARCHAR(10) NOT NULL,
 category VARCHAR(32) NOT NULL CHECK(category IN ('accommodation','dining','gym','meeting_room')),
 amount_minor BIGINT NOT NULL CHECK(amount_minor>=0), currency VARCHAR(3) NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'),
 KEY demo_sales_scope(hotel,business_date,category), FOREIGN KEY(hotel) REFERENCES stores(hotel))""",
"""CREATE TABLE IF NOT EXISTS store_aliases (
 hotel VARCHAR(80) NOT NULL, alias VARCHAR(80) NOT NULL, normalized_alias VARCHAR(240) NOT NULL CHECK(length(normalized_alias)>0),
 kind VARCHAR(8) NOT NULL CHECK(kind IN ('alias','asr')), PRIMARY KEY(hotel,normalized_alias),
 FOREIGN KEY(hotel) REFERENCES stores(hotel))""",
"""CREATE TABLE IF NOT EXISTS menu_items (
 id VARCHAR(80) PRIMARY KEY, hotel VARCHAR(80) NOT NULL, name VARCHAR(180) NOT NULL,
 description VARCHAR(500) NOT NULL DEFAULT '', price_minor BIGINT NOT NULL CHECK(price_minor>=0),
 enabled INT NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), KEY menu_hotel(hotel,id))""",
"""CREATE TABLE IF NOT EXISTS meal_orders (
 request_id VARCHAR(80) PRIMARY KEY, total_amount_minor BIGINT NOT NULL CHECK(total_amount_minor>=0),
 currency VARCHAR(3) NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'),
 settlement VARCHAR(16) NOT NULL DEFAULT 'offline' CHECK(settlement='offline'),
 FOREIGN KEY(request_id) REFERENCES requests(id))""",
"""CREATE TABLE IF NOT EXISTS meal_order_items (
 request_id VARCHAR(80) NOT NULL, item_id VARCHAR(80) NOT NULL, name VARCHAR(180) NOT NULL,
 unit_price_minor BIGINT NOT NULL CHECK(unit_price_minor>=0), quantity INT NOT NULL CHECK(quantity BETWEEN 1 AND 20),
 PRIMARY KEY(request_id,item_id), FOREIGN KEY(request_id) REFERENCES meal_orders(request_id),
 FOREIGN KEY(item_id) REFERENCES menu_items(id))""",
"""CREATE TABLE IF NOT EXISTS booking_resources (
 id VARCHAR(80) PRIMARY KEY, hotel VARCHAR(80) NOT NULL, kind VARCHAR(16) NOT NULL CHECK(kind IN ('gym','meeting')),
 name VARCHAR(180) NOT NULL, capacity INT NOT NULL CHECK(capacity BETWEEN 1 AND 100),
 max_people INT NOT NULL CHECK(max_people BETWEEN 1 AND 100), enabled INT NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 UNIQUE KEY resource_scope(id,hotel), CHECK(kind!='meeting' OR capacity=1))""",
"""CREATE TABLE IF NOT EXISTS booking_slots (
 id VARCHAR(80) PRIMARY KEY, hotel VARCHAR(80) NOT NULL, resource_id VARCHAR(80) NOT NULL, business_date VARCHAR(10) NOT NULL,
 starts_at VARCHAR(32) NOT NULL, ends_at VARCHAR(32) NOT NULL, CHECK(ends_at>starts_at),
 KEY slots_scope(hotel,business_date,resource_id), UNIQUE KEY slot_start(resource_id,starts_at),
 FOREIGN KEY(resource_id,hotel) REFERENCES booking_resources(id,hotel))""",
"""CREATE TABLE IF NOT EXISTS booking_details (
 request_id VARCHAR(80) PRIMARY KEY, slot_id VARCHAR(80) NOT NULL, participants INT NOT NULL CHECK(participants BETWEEN 1 AND 100),
 units INT NOT NULL CHECK(units>0), resource_name VARCHAR(180) NOT NULL, starts_at VARCHAR(32) NOT NULL, ends_at VARCHAR(32) NOT NULL,
 KEY bookings_slot(slot_id,request_id), FOREIGN KEY(request_id) REFERENCES requests(id),
 FOREIGN KEY(slot_id) REFERENCES booking_slots(id))""",
]


def ensure_schema(allow_upgrade=False):
    raw = raw_connect()
    lock = None
    stage = "preflight"
    try:
        with raw.cursor() as c:
            c.execute("SELECT VERSION(), DATABASE()")
            version, database = c.fetchone()
            if "MariaDB" in version or not version.startswith("8."):
                raise ValueError("Deployment requires MySQL 8, not MariaDB")
            if tuple(int(n) for n in version.split("-")[0].split(".")[:3]) < (8, 0, 16):
                raise ValueError("MySQL 8.0.16+ is required for CHECK constraints")
            c.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
            tables = {r[0] for r in c.fetchall()}
            if not allow_upgrade:
                if "schema_migrations" not in tables or "service_metadata" not in tables:
                    raise ValueError("MySQL requires explicit deployment prepare")
                c.execute("SELECT MAX(version) FROM schema_migrations")
                if c.fetchone()[0] != VERSION:
                    raise ValueError("Unsupported MySQL schema; run explicit deployment prepare")
                c.execute("SELECT value FROM service_metadata WHERE `key`='storage_backend'")
                if c.fetchone() != ("mysql8",):
                    raise ValueError("Database is not owned by hotel-mcp MySQL storage")
                return
            lock = "hotel-schema-" + hashlib.sha256(database.encode()).hexdigest()[:40]
            c.execute("SELECT GET_LOCK(%s,15)", (lock,))
            if c.fetchone()[0] != 1:
                raise sqlite3.OperationalError("MySQL migration lock unavailable")
            # Recheck under the migration lock, including concurrent first starts.
            c.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()")
            tables = {r[0] for r in c.fetchall()}
            if tables:
                if "service_metadata" not in tables:
                    raise ValueError("Refusing to initialize a nonempty unowned database")
                c.execute("SELECT value FROM service_metadata WHERE `key`='storage_backend'")
                if c.fetchone() != ("mysql8",):
                    raise ValueError("Refusing to initialize a nonempty unowned database")
                if "schema_migrations" in tables:
                    c.execute("SELECT MAX(version) FROM schema_migrations")
                    current = c.fetchone()[0]
                    if current not in (None, VERSION):
                        raise ValueError("Unsupported MySQL schema version")
            for index, statement in enumerate(SCHEMA):
                stage = f"table-{index}"
                c.execute(statement + " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin")
                if index == 0:
                    c.execute("INSERT INTO service_metadata(`key`,value) VALUES('storage_backend','mysql8') "
                              "ON DUPLICATE KEY UPDATE value=value")
                    raw.commit()
            # RDS business accounts must not need SUPER or a global binlog change.
            # Fixed-slot imports use Services.insert_slot under the shared write lock.
            stage = "version-record"
            c.execute("INSERT INTO hotel_write_lock(id) VALUES(1) ON DUPLICATE KEY UPDATE id=id")
            c.execute("INSERT INTO service_metadata(`key`,value) VALUES('cursor_secret',%s) ON DUPLICATE KEY UPDATE value=value",
                      (secrets.token_hex(32),))
            c.execute("INSERT INTO schema_migrations(version) VALUES(%s) ON DUPLICATE KEY UPDATE version=version", (VERSION,))
            raw.commit()
    except pymysql.MySQLError as exc:
        raw.rollback()
        code = exc.args[0] if exc.args and isinstance(exc.args[0], int) else 0
        raise sqlite3.OperationalError(f"MySQL schema preparation failed at {stage} (code {code})") from None
    finally:
        if lock:
            with raw.cursor() as c:
                c.execute("SELECT RELEASE_LOCK(%s)", (lock,))
        raw.close()
