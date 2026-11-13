"""Explicit, atomic legacy upgrade. Never guesses historical stay ownership."""
import secrets
import sqlite3


# Keep the v2 definition immutable so old databases follow the same upgrade path.
SCHEMA_VERSION = 7
SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS service_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rooms (
    hotel TEXT NOT NULL, room TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
    PRIMARY KEY(hotel,room));
CREATE TABLE IF NOT EXISTS stays (
    id TEXT PRIMARY KEY, hotel TEXT NOT NULL, room TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','closed')),
    check_in_at TEXT NOT NULL, expected_checkout_date TEXT NOT NULL, closed_at TEXT,
    close_reason TEXT NOT NULL DEFAULT '', version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
    FOREIGN KEY(hotel,room) REFERENCES rooms(hotel,room), UNIQUE(hotel,room,id));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_stay ON stays(hotel,room) WHERE status='active';
CREATE TABLE IF NOT EXISTS requests (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    hotel TEXT NOT NULL, room TEXT NOT NULL, stay_id TEXT,
    request_key TEXT NOT NULL, request_type TEXT NOT NULL DEFAULT 'general' CHECK(request_type IN ('general','repair')),
    description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','accepted','completed','rejected','cancelled','closed_unresolved')),
    note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
    FOREIGN KEY(hotel,room,stay_id) REFERENCES stays(hotel,room,id));
CREATE UNIQUE INDEX IF NOT EXISTS request_stay_key ON requests(hotel,stay_id,request_key) WHERE stay_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS request_legacy_key ON requests(hotel,room,request_key) WHERE stay_id IS NULL;
CREATE INDEX IF NOT EXISTS request_scope ON requests(hotel,stay_id,seq);
CREATE INDEX IF NOT EXISTS request_worklist ON requests(hotel,status,request_type,seq);
CREATE TABLE IF NOT EXISTS repair_details (
    request_id TEXT PRIMARY KEY REFERENCES requests(id),
    category TEXT NOT NULL CHECK(category IN ('air_conditioning','plumbing_electrical','network','door_lock','facilities','other')),
    location TEXT NOT NULL DEFAULT '', urgency TEXT NOT NULL CHECK(urgency IN ('normal','urgent')),
    related_request_id TEXT REFERENCES requests(id));
CREATE TABLE IF NOT EXISTS request_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL REFERENCES requests(id),
    action TEXT NOT NULL, from_status TEXT, to_status TEXT NOT NULL, note TEXT NOT NULL,
    actor_principal_id TEXT NOT NULL, actor_role TEXT NOT NULL, created_at TEXT NOT NULL,
    request_version INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS action_request ON request_actions(request_id,id);
CREATE TABLE IF NOT EXISTS operation_receipts (
    hotel TEXT NOT NULL, principal_id TEXT NOT NULL, stay_scope TEXT NOT NULL, operation TEXT NOT NULL, request_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL, response_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(hotel,principal_id,stay_scope,operation,request_key));
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, hotel TEXT NOT NULL, role TEXT NOT NULL,
    room TEXT NOT NULL, payload TEXT NOT NULL, stay_id TEXT);
CREATE INDEX IF NOT EXISTS idx_events_scope ON events(hotel,role,room,seq);
CREATE INDEX IF NOT EXISTS event_stay ON events(hotel,role,stay_id,seq);
"""


EXTENSION_SCHEMA = """
CREATE UNIQUE INDEX one_open_extension ON requests(hotel,stay_id)
    WHERE request_type='stay_extension' AND status IN ('pending','quoted','confirmed','processing');
CREATE TABLE stay_extension_quotes (
    id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id), revision INTEGER NOT NULL CHECK(revision>0),
    base_checkout_date TEXT NOT NULL, base_stay_version INTEGER NOT NULL CHECK(base_stay_version>0),
    target_checkout_date TEXT NOT NULL, nights INTEGER NOT NULL CHECK(nights>0),
    total_amount_minor INTEGER NOT NULL CHECK(typeof(total_amount_minor)='integer' AND total_amount_minor>=0),
    currency TEXT NOT NULL CHECK(currency='CNY'), pricing_note TEXT NOT NULL,
    expires_at TEXT NOT NULL, created_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL,
    change_reason TEXT NOT NULL DEFAULT '', UNIQUE(request_id,revision), UNIQUE(request_id,id));
CREATE TABLE stay_extension_details (
    request_id TEXT PRIMARY KEY REFERENCES requests(id), original_checkout_date TEXT NOT NULL,
    requested_checkout_date TEXT NOT NULL CHECK(requested_checkout_date>original_checkout_date),
    application_note TEXT NOT NULL DEFAULT '', current_quote_id TEXT, confirmed_quote_id TEXT,
    confirmed_at TEXT, processing_started_at TEXT, result_at TEXT, result_note TEXT NOT NULL DEFAULT '',
    result_reference TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(request_id,current_quote_id) REFERENCES stay_extension_quotes(request_id,id),
    FOREIGN KEY(request_id,confirmed_quote_id) REFERENCES stay_extension_quotes(request_id,id));
"""


def _execute_schema(db, schema):
    for statement in schema.split(";"):
        if statement.strip():
            db.execute(statement)


SALES_SCHEMA = """
CREATE TABLE stores (
    hotel TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE demo_sales_orders (
    id TEXT PRIMARY KEY, hotel TEXT NOT NULL REFERENCES stores(hotel), business_date TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('accommodation','dining','gym','meeting_room')),
    amount_minor INTEGER NOT NULL CHECK(typeof(amount_minor)='integer' AND amount_minor>=0),
    currency TEXT NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'));
CREATE INDEX demo_sales_scope ON demo_sales_orders(hotel,business_date,category);
"""


ENTITY_SCHEMA = """
CREATE TABLE store_aliases (
    hotel TEXT NOT NULL REFERENCES stores(hotel), alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL CHECK(length(normalized_alias)>0),
    kind TEXT NOT NULL CHECK(kind IN ('alias','asr')), PRIMARY KEY(hotel,normalized_alias));
"""


def _upgrade_v3(db):
    # Rebuild the parent in place with foreign_keys OFF; never rename the old
    # parent, which would rewrite child references to the temporary name.
    old_sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='requests'").fetchone()
    request_sql = next(s for s in SCHEMA.split(";") if "CREATE TABLE IF NOT EXISTS requests (" in s)
    request_sql = request_sql.replace("IF NOT EXISTS requests (", "requests_v3 (")
    request_sql = request_sql.replace("('general','repair')", "('general','repair','stay_extension')")
    request_sql = request_sql.replace("'cancelled','closed_unresolved'", "'cancelled','closed_unresolved','quoted','confirmed','processing','failed'")
    request_sql = request_sql.replace("FOREIGN KEY(hotel,room,stay_id)",
        "CHECK(request_type!='stay_extension' OR (stay_id IS NOT NULL AND status IN "
        "('pending','quoted','confirmed','processing','completed','failed','rejected','cancelled'))), "
        "CHECK(request_type='stay_extension' OR status NOT IN ('quoted','confirmed','processing','failed')), "
        "FOREIGN KEY(hotel,room,stay_id)")
    db.execute(request_sql)
    db.execute("INSERT INTO requests_v3 SELECT * FROM requests")
    db.execute("DROP TABLE requests")
    db.execute("ALTER TABLE requests_v3 RENAME TO requests")
    if old_sequence:
        db.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='requests'", (old_sequence[0],))
    for statement in SCHEMA.split(";"):
        if "INDEX" in statement and " ON requests(" in statement:
            db.execute(statement)
    db.execute("ALTER TABLE request_actions ADD COLUMN data TEXT NOT NULL DEFAULT '{}'")
    _execute_schema(db, EXTENSION_SCHEMA)
    db.execute("INSERT INTO schema_migrations(version) VALUES(3)")


SERVICE_SCHEMA = """
CREATE TABLE menu_items (
    id TEXT PRIMARY KEY, hotel TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    price_minor INTEGER NOT NULL CHECK(typeof(price_minor)='integer' AND price_minor>=0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)));
CREATE INDEX menu_hotel ON menu_items(hotel,id);
CREATE TABLE meal_orders (
    request_id TEXT PRIMARY KEY REFERENCES requests(id), total_amount_minor INTEGER NOT NULL CHECK(total_amount_minor>=0),
    currency TEXT NOT NULL DEFAULT 'CNY' CHECK(currency='CNY'), settlement TEXT NOT NULL DEFAULT 'offline' CHECK(settlement='offline'));
CREATE TABLE meal_order_items (
    request_id TEXT NOT NULL REFERENCES meal_orders(request_id), item_id TEXT NOT NULL REFERENCES menu_items(id),
    name TEXT NOT NULL, unit_price_minor INTEGER NOT NULL CHECK(unit_price_minor>=0),
    quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 20), PRIMARY KEY(request_id,item_id));
CREATE TABLE booking_resources (
    id TEXT PRIMARY KEY, hotel TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('gym','meeting')),
    name TEXT NOT NULL, capacity INTEGER NOT NULL CHECK(capacity BETWEEN 1 AND 100),
    max_people INTEGER NOT NULL CHECK(max_people BETWEEN 1 AND 100),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), UNIQUE(id,hotel),
    CHECK(kind!='meeting' OR capacity=1));
CREATE TABLE booking_slots (
    id TEXT PRIMARY KEY, hotel TEXT NOT NULL, resource_id TEXT NOT NULL, business_date TEXT NOT NULL,
    starts_at TEXT NOT NULL, ends_at TEXT NOT NULL CHECK(ends_at>starts_at),
    FOREIGN KEY(resource_id,hotel) REFERENCES booking_resources(id,hotel));
CREATE INDEX slots_scope ON booking_slots(hotel,business_date,resource_id);
CREATE TABLE booking_details (
    request_id TEXT PRIMARY KEY REFERENCES requests(id), slot_id TEXT NOT NULL REFERENCES booking_slots(id),
    participants INTEGER NOT NULL CHECK(participants BETWEEN 1 AND 100), units INTEGER NOT NULL CHECK(units>0),
    resource_name TEXT NOT NULL, starts_at TEXT NOT NULL, ends_at TEXT NOT NULL);
CREATE INDEX bookings_slot ON booking_details(slot_id,request_id);
"""


def _upgrade_v6(db):
    sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='requests'").fetchone()[0]
    sql = sql.replace('CREATE TABLE "requests"', 'CREATE TABLE requests_v6', 1)
    sql = sql.replace('CREATE TABLE requests (', 'CREATE TABLE requests_v6 (', 1)
    sql = sql.replace("('general','repair','stay_extension')", "('general','repair','stay_extension','meal','gym_booking','meeting_booking')")
    sql = sql.replace("FOREIGN KEY(hotel,room,stay_id)",
        "CHECK(request_type NOT IN ('meal','gym_booking','meeting_booking') OR "
        "(stay_id IS NOT NULL AND status IN ('pending','accepted','completed','rejected','cancelled'))), "
        "FOREIGN KEY(hotel,room,stay_id)")
    indexes = [r[0] for r in db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='requests' AND sql IS NOT NULL")]
    sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='requests'").fetchone()
    db.execute(sql)
    db.execute("INSERT INTO requests_v6 SELECT * FROM requests")
    db.execute("DROP TABLE requests")
    db.execute("ALTER TABLE requests_v6 RENAME TO requests")
    if sequence:
        db.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='requests'", (sequence[0],))
    for index in indexes:
        db.execute(index)
    _execute_schema(db, SERVICE_SCHEMA)
    # Fixed slots for one physical resource must not overlap, including direct local edits.
    for operation in ('INSERT', 'UPDATE'):
        db.execute(f"""CREATE TRIGGER slots_no_overlap_{operation.lower()} BEFORE {operation} ON booking_slots
            WHEN EXISTS(SELECT 1 FROM booking_slots WHERE resource_id=NEW.resource_id AND id!=NEW.id
                        AND starts_at<NEW.ends_at AND ends_at>NEW.starts_at)
            BEGIN SELECT RAISE(ABORT,'overlapping resource slots'); END""")
    db.execute("INSERT INTO schema_migrations(version) VALUES(6)")


def ensure_schema(db, allow_upgrade=False):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    current = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] if "schema_migrations" in tables else None
    if current == SCHEMA_VERSION:
        return
    if current not in (None, 2, 3, 4, 5, 6):
        raise ValueError(f"unsupported database schema version: {current}")
    legacy = "requests" in tables and current is None
    if (legacy or current in (2, 3, 4, 5, 6)) and not allow_upgrade:
        raise ValueError("legacy database requires explicit --migrate; back up and stop its writer first")
    db.execute("PRAGMA foreign_keys=OFF")
    try:
        db.execute("BEGIN IMMEDIATE")
        if current is None:
            if legacy:
                db.execute("ALTER TABLE requests RENAME TO requests_legacy")
                db.execute("ALTER TABLE events ADD COLUMN stay_id TEXT")
            _execute_schema(db, SCHEMA)
            if legacy:
                db.execute("""INSERT INTO requests(id,hotel,room,request_key,description,status,note,created_at,updated_at)
                    SELECT id,hotel,room,request_key,description,status,note,
                           strftime('%Y-%m-%dT%H:%M:%f',created_at)||'000Z',strftime('%Y-%m-%dT%H:%M:%f',updated_at)||'000Z'
                    FROM requests_legacy ORDER BY created_at,id""")
                db.execute("INSERT OR IGNORE INTO rooms(hotel,room) SELECT DISTINCT hotel,room FROM requests_legacy")
                db.execute("DROP TABLE requests_legacy")
            db.execute("INSERT INTO service_metadata VALUES('cursor_secret',?)", (secrets.token_hex(32),))
            db.execute("INSERT INTO schema_migrations(version) VALUES(2)")
        if current in (None, 2):
            _upgrade_v3(db)
        if current in (None, 2, 3):
            _execute_schema(db, SALES_SCHEMA)
            db.execute("INSERT INTO schema_migrations(version) VALUES(4)")
        if current in (None, 2, 3, 4):
            _execute_schema(db, ENTITY_SCHEMA)
            db.execute("INSERT INTO schema_migrations(version) VALUES(5)")
        if current != 6:
            _upgrade_v6(db)
        from .account_schema import ACCOUNT_SCHEMA
        for statement in ACCOUNT_SCHEMA:
            db.execute(statement)
        db.execute("INSERT INTO schema_migrations(version) VALUES(7)")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("foreign key check failed during migration")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute("PRAGMA foreign_keys=ON")
