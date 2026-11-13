"""Additive v7 account schema shared by SQLite and MySQL 8."""

ACCOUNT_SCHEMA = [
"""CREATE TABLE IF NOT EXISTS accounts (
 id VARCHAR(64) PRIMARY KEY, username VARCHAR(64) NOT NULL UNIQUE,
 display_name VARCHAR(80) NOT NULL, role VARCHAR(24) NOT NULL
 CHECK(role IN ('super_admin','store_admin','frontdesk','customer')),
 hotel VARCHAR(80) NOT NULL DEFAULT '', password_hash VARCHAR(256) NOT NULL,
 enabled INT NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 must_change_password INT NOT NULL DEFAULT 1 CHECK(must_change_password IN (0,1)),
 auth_version INT NOT NULL DEFAULT 1, version INT NOT NULL DEFAULT 1,
 created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS account_stays (
 account_id VARCHAR(64) NOT NULL, stay_id VARCHAR(80) NOT NULL UNIQUE,
 created_at VARCHAR(40) NOT NULL, PRIMARY KEY(account_id,stay_id),
 FOREIGN KEY(account_id) REFERENCES accounts(id), FOREIGN KEY(stay_id) REFERENCES stays(id))""",
"""CREATE TABLE IF NOT EXISTS web_sessions (
 token_hash VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) NOT NULL,
 auth_version INT NOT NULL, expires_at BIGINT NOT NULL,
 FOREIGN KEY(account_id) REFERENCES accounts(id))""",
"""CREATE TABLE IF NOT EXISTS account_operations (
 actor_id VARCHAR(64) NOT NULL, request_key VARCHAR(80) NOT NULL,
 payload_hash VARCHAR(64) NOT NULL, response_json LONGTEXT NOT NULL,
 created_at VARCHAR(40) NOT NULL, PRIMARY KEY(actor_id,request_key))""",
"""CREATE TABLE IF NOT EXISTS account_audit (
 id VARCHAR(64) PRIMARY KEY, actor_id VARCHAR(64) NOT NULL,
 target_id VARCHAR(64) NOT NULL, action VARCHAR(40) NOT NULL, created_at VARCHAR(40) NOT NULL)""",
]
