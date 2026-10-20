"""MySQL 8 storage adapter for the existing parameterized business queries.

The singleton InnoDB row lock preserves SQLite BEGIN IMMEDIATE's write ordering,
including event sequence commit order, across processes using this adapter.
"""
import os
import re
import sqlite3
from contextlib import contextmanager
from decimal import Decimal

import pymysql


def settings():
    required = ("DB_HOST", "DB_PORT", "DB_DATABASE", "DB_USERNAME", "DB_PASSWORD")
    if any(not os.environ.get(k) for k in required):
        raise ValueError("MySQL requires all five platform DB_* variables")
    try:
        port = int(os.environ["DB_PORT"])
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        raise ValueError("Invalid DB_PORT") from None
    options = dict(host=os.environ["DB_HOST"], port=port, database=os.environ["DB_DATABASE"],
                   user=os.environ["DB_USERNAME"], password=os.environ["DB_PASSWORD"],
                   charset="utf8mb4", autocommit=False, connect_timeout=5, read_timeout=15,
                   write_timeout=15, local_infile=False,
                   init_command="SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    ca = os.environ.get("DB_SSL_CA")
    if ca:
        options.update(ssl_ca=ca, ssl_verify_cert=True, ssl_verify_identity=True)
    return options


def raw_connect():
    try:
        return pymysql.connect(**settings())
    except pymysql.MySQLError:
        raise sqlite3.OperationalError("MySQL connection unavailable; check private deployment settings") from None


def query_sql(sql):
    # Only SQL tokens are adapted; user input always stays in bound parameters.
    sql = sql.replace("ON CONFLICT(hotel,room) DO UPDATE SET enabled=excluded.enabled",
                      "ON DUPLICATE KEY UPDATE enabled=VALUES(enabled)")
    tokens = r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`[^`]*`|\bkey\b|\?"
    return re.sub(tokens, lambda m: "%s" if m[0] == "?" else "`key`" if m[0] == "key" else m[0], sql)


class Row(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


class Result:
    def __init__(self, cursor):
        names = [c[0] for c in cursor.description or ()]
        self.rows = [Row(zip(names, (int(v) if isinstance(v, Decimal) and v == v.to_integral_value() else v
                                    for v in row))) for row in cursor.fetchall()] if names else []
        self.lastrowid, self.rowcount = cursor.lastrowid, cursor.rowcount
        self.offset = 0

    def fetchone(self):
        if self.offset >= len(self.rows):
            return None
        row = self.rows[self.offset]
        self.offset += 1
        return row

    def fetchall(self):
        rows = self.rows[self.offset:]
        self.offset = len(self.rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class Connection:
    def __init__(self, raw):
        self.raw, self.write_locked = raw, False

    def _lock(self):
        if not self.write_locked:
            with self.raw.cursor() as cursor:
                cursor.execute("SELECT id FROM hotel_write_lock WHERE id=1 FOR UPDATE")
                if not cursor.fetchone():
                    raise sqlite3.OperationalError("MySQL write lock missing; run deployment migration")
            self.write_locked = True

    def execute(self, sql, args=()):
        try:
            command = sql.strip().upper()
            if command == "BEGIN IMMEDIATE":
                self.raw.begin()
                self._lock()
                return None
            if command == "BEGIN":
                self.raw.begin()
                return None
            if command.startswith(("INSERT ", "UPDATE ", "DELETE ")):
                self._lock()
            with self.raw.cursor() as cursor:
                cursor.execute(query_sql(sql), tuple(args) if args else None)
                return Result(cursor)
        except pymysql.IntegrityError:
            raise sqlite3.IntegrityError("MySQL constraint violation") from None
        except pymysql.MySQLError:
            raise sqlite3.OperationalError("MySQL operation failed; retry the original request") from None

    def executemany(self, sql, rows):
        for row in rows:
            self.execute(sql, row)


@contextmanager
def connection():
    raw = raw_connect()
    try:
        yield Connection(raw)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
