"""Explicit Coolify/RDS bootstrap. Generated credentials stay in a private volume."""
import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from .demo import DEPLOYMENT_STORES, init_full_demo
from .store import HotelStore


SEED_KEY = "deployment_demo_v1"
TABLES = ("stores", "demo_sales_orders", "store_aliases", "rooms", "stays", "requests",
          "repair_details", "stay_extension_quotes", "stay_extension_details", "menu_items",
          "meal_orders", "meal_order_items", "booking_resources", "booking_slots", "booking_details",
          "request_actions", "operation_receipts", "events")


@contextmanager
def preparation_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "prepare.lock").open("a+b") as stream:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def seed(store, source, config):
    # The seed snapshot is immutable; live tokens, stay bindings and endpoints may change.
    digest = hashlib.sha256(Path(source).read_bytes()).hexdigest()
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT value FROM service_metadata WHERE key=?", (SEED_KEY,)).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("Demo identity mismatch: restore this instance's private volume/config")
            return {"seeded": False, "is_demo": True}
        hotels = [h for h, _ in DEPLOYMENT_STORES]
        marks = ",".join("?" for _ in hotels)
        if db.execute(f"SELECT 1 FROM stores WHERE hotel IN ({marks})", hotels).fetchone():
            raise ValueError("Demo store IDs already exist; refusing to overwrite existing data")
        with sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True) as local:
            for table in TABLES:
                columns = [r[1] for r in local.execute(f"PRAGMA table_info({table})")]
                if table in ("requests", "events"):
                    columns.remove("seq")
                if table == "request_actions":
                    columns.remove("id")
                names = ",".join(f"`{c}`" for c in columns)
                rows = local.execute(f"SELECT {names} FROM {table}").fetchall()
                if table == "booking_slots":
                    for row in rows:
                        store.services.insert_slot(db, dict(zip(columns, row)))
                else:
                    db.executemany(f"INSERT INTO {table}({names}) VALUES({','.join('?' for _ in columns)})", rows)
        db.execute("INSERT INTO service_metadata(key,value) VALUES(?,?)", (SEED_KEY, digest))
    return {"seeded": True, "store_count": 5, "sales_order_count": 600, "request_count": 25, "is_demo": True}


def prepare(directory):
    root = Path(directory)
    with preparation_lock(root):
        store = HotelStore("mysql", migrate=True)
        bootstrap = root / "bootstrap"
        if not bootstrap.exists():
            with store.connect() as db:
                if db.execute("SELECT 1 FROM service_metadata WHERE key=?", (SEED_KEY,)).fetchone():
                    raise ValueError("Private bootstrap volume missing; restore it instead of regenerating identities")
            # Atomic publication: interruption cannot leave half-written credentials.
            with tempfile.TemporaryDirectory(prefix="seed-staging-", dir=root) as staging:
                created = Path(staging) / "bootstrap"
                init_full_demo(created, stores=DEPLOYMENT_STORES, days=30)
                config = json.loads((created / "config.json").read_text(encoding="utf-8"))
                # Do not replace online aliases from the historical seed on every restart.
                config.pop("store_aliases", None)
                (created / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
                (created / "config.json").chmod(0o600)
                created.rename(bootstrap)
        config_path = bootstrap / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        result = seed(store, bootstrap / "hotel.sqlite3", config)
        return config, result


def main():
    parser = argparse.ArgumentParser(description="Explicit MySQL deployment lifecycle; never uses local data/")
    parser.add_argument("action", choices=("prepare", "serve"))
    parser.add_argument("--data-dir", default=os.environ.get("HOTEL_DATA_DIR", "/data"))
    parser.add_argument("--port", type=int, default=8120)
    args = parser.parse_args()
    try:
        config, result = prepare(args.data_dir)
        if args.action == "prepare":
            print(json.dumps(result))
            return
        from .server import create_app
        uvicorn.run(create_app(config, "mysql"), host="0.0.0.0", port=args.port, access_log=False)
    except (ValueError, sqlite3.Error, OSError) as exc:
        # Never print third-party exceptions, DSNs, SQL values, or credentials.
        parser.exit(1, f"Hotel deployment failed ({type(exc).__name__}); verify DB variables, schema and private volume.\n")


if __name__ == "__main__":
    main()
