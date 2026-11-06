from contextlib import closing
from datetime import datetime
import json
import math
import os
import sqlite3
import time
from zoneinfo import ZoneInfo

from store import BASE

DB = BASE / "usage.sqlite3"
TIMEZONE = ZoneInfo("Asia/Taipei")
_connection = None


def initialize():
    global _connection
    _connection = sqlite3.connect(DB)
    _connection.execute("PRAGMA journal_mode=WAL")
    _connection.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            token TEXT NOT NULL,
            user_agent TEXT NOT NULL,
            last_seen REAL NOT NULL,
            PRIMARY KEY (token, user_agent)
        ) WITHOUT ROWID
    """)
    _connection.execute("CREATE INDEX IF NOT EXISTS client_recent ON clients (token, last_seen DESC)")
    _connection.commit()
    DB.chmod(0o600)


def record(headers):
    if headers.get("X-Usage-Method") not in {"GET", "HEAD"}:
        return
    token = headers.get("X-Usage-Token", "")
    folder = headers.get("X-Usage-Folder", "")
    users = json.loads((BASE / "current/users.json").read_bytes())
    user = next((u for u in users if u["token"] == token and u["folder"] == folder), None)
    if user is None:
        return
    key = user.get("usage_key", token)
    try:
        timestamp = float(headers.get("X-Usage-Time", ""))
    except ValueError:
        return
    if not math.isfinite(timestamp) or abs(time.time() - timestamp) > 120:
        return
    # Exact User-Agent strings are unique within each subscription.
    user_agent = headers.get("X-Usage-Agent", "")
    _connection.execute("""
        INSERT INTO clients (token, user_agent, last_seen) VALUES (?, ?, ?)
        ON CONFLICT (token, user_agent)
        DO UPDATE SET last_seen = MAX(clients.last_seen, excluded.last_seen)
    """, (key, user_agent, timestamp))
    _connection.commit()


def by_subscription():
    result = {}
    with closing(sqlite3.connect(DB)) as connection:
        for token, agent, timestamp in connection.execute(
            "SELECT token, user_agent, last_seen FROM clients ORDER BY last_seen DESC"
        ):
            result.setdefault(token, []).append((agent, timestamp))
    return result


def display_time(timestamp):
    return datetime.fromtimestamp(timestamp, TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def summaries():
    with closing(sqlite3.connect(DB)) as connection:
        return {
            token: (count, timestamp)
            for token, count, timestamp in connection.execute(
                "SELECT token, COUNT(*), MAX(last_seen) FROM clients GROUP BY token"
            )
        }


def matching_tokens(query):
    with closing(sqlite3.connect(DB)) as connection:
        return {
            token for token, in connection.execute(
                "SELECT DISTINCT token FROM clients WHERE instr(lower(user_agent), lower(?)) > 0", (query,)
            )
        }


def page(token, query="", page_number=1):
    query = query.strip()
    try:
        page_number = int(page_number)
    except (TypeError, ValueError):
        page_number = 1
    condition = "token = ? AND instr(lower(user_agent), lower(?)) > 0"
    with closing(sqlite3.connect(DB)) as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM clients WHERE " + condition, (token, query)
        ).fetchone()[0]
        page_count = max(1, (total + 49) // 50)
        page_number = min(max(page_number, 1), page_count)
        records = connection.execute(
            "SELECT user_agent, last_seen FROM clients WHERE " + condition
            + " ORDER BY last_seen DESC, user_agent LIMIT 50 OFFSET ?",
            (token, query, (page_number - 1) * 50),
        ).fetchall()
    return {
        "page": page_number,
        "pages": page_count,
        "total": total,
        "items": [{"user_agent": agent or "未提供 User-Agent", "value": agent, "last_seen": display_time(timestamp)}
                  for agent, timestamp in records],
    }


def delete(key, agent):
    with closing(sqlite3.connect(DB)) as connection:
        with connection:
            connection.execute("DELETE FROM clients WHERE token = ? AND user_agent = ?", (key, agent))


def delete_subscription(key):
    with closing(sqlite3.connect(DB)) as connection:
        with connection:
            connection.execute("DELETE FROM clients WHERE token = ?", (key,))


def summary(key):
    with closing(sqlite3.connect(DB)) as connection:
        count, latest = connection.execute(
            "SELECT COUNT(*), MAX(last_seen) FROM clients WHERE token = ?", (key,)
        ).fetchone()
    return {"count": count, "last_request": display_time(latest) if latest is not None else "暂无请求"}
