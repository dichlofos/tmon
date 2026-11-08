"""Disposable local cache of successful read-only API responses."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time


class ApiCache:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, expires REAL NOT NULL, body TEXT NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
        return connection

    @staticmethod
    def key(namespace, service, method, payload):
        # Store only a digest: neither authentication nor account IDs are in keys.
        raw = json.dumps([namespace, service, method, payload], sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key):
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT body FROM responses WHERE key = ? AND expires > ?", (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, body, ttl):
        encoded = json.dumps(body)
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM responses WHERE expires <= ?", (time.time(),))
            db.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?)", (key, time.time() + ttl, encoded))

    def get_setting(self, key):
        with closing(self.connect()) as db, db:
            row = db.execute("SELECT body FROM settings WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_setting(self, key, value):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))
