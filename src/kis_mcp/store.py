import json
import sqlite3
import time
from contextlib import contextmanager

from filelock import FileLock

from .models import TradingError


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(directory / "server.lock"))
        self.lock.acquire(timeout=0)
        try:
            self.db = sqlite3.connect(directory / "trading.sqlite3", isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, doc TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, message TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    sent_at REAL);
            """)
            initialized = self.db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if initialized is None:
                count = sum(
                    self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("meta", "orders", "requests")
                )
                if count:
                    raise RuntimeError("Unrecognized state; restore a valid backup")
                with self.transaction():
                    self.db.execute("INSERT INTO meta VALUES ('schema','1')")
                    self.db.execute("INSERT INTO meta VALUES ('mode','real')")
            elif initialized[0] != "1":
                raise RuntimeError("Unsupported state schema")
            self.mode()
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.release()
            raise

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def close(self):
        self.db.close()
        self.lock.release()

    def mode(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='mode'").fetchone()
        if row is None or row[0] not in ("real", "demo"):
            raise RuntimeError("Persisted mode is invalid; restore a valid backup")
        return row[0]

    def set_mode(self, mode):
        if mode not in ("real", "demo"):
            raise TradingError("Mode must be real or demo")
        self.db.execute("UPDATE meta SET value=? WHERE key='mode'", (mode,))

    def get(self, order_id):
        row = self.db.execute("SELECT doc FROM orders WHERE id=?", (order_id,)).fetchone()
        if row is None:
            raise TradingError("Unknown MCP order ID")
        return json.loads(row[0])

    def orders(self):
        return [
            json.loads(r[0]) for r in self.db.execute("SELECT doc FROM orders ORDER BY rowid DESC")
        ]

    def put(self, order):
        self.db.execute(
            "INSERT INTO orders VALUES (?,?) ON CONFLICT(id) DO UPDATE SET doc=excluded.doc",
            (order["id"], encoded(order)),
        )

    def request(self, request_id, kind, payload):
        row = self.db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["kind"] != kind or row["payload"] != encoded(payload):
            raise TradingError("client_request_id was already used with different input")
        return json.loads(row["result"])

    def save_request(self, request_id, kind, payload, result):
        self.db.execute(
            "INSERT INTO requests VALUES (?,?,?,?)",
            (request_id, kind, encoded(payload), encoded(result)),
        )

    def update_request(self, request_id, result):
        self.db.execute("UPDATE requests SET result=? WHERE id=?", (encoded(result), request_id))

    def cancellation_broker_ids(self, order_id):
        ids = []
        for row in self.db.execute("SELECT result FROM requests WHERE kind='cancel'"):
            result = json.loads(row[0])
            if result.get("order_id") != order_id:
                continue
            broker_id = result.get("cancellation_broker_id")
            if broker_id is not None and str(broker_id).strip():
                ids.append(str(broker_id))
        return ids

    def event(self, event_id, event, message):
        self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?)", (event_id, encoded(event)))
        self.db.execute(
            "INSERT OR IGNORE INTO outbox(id,message) VALUES (?,?)", (event_id, message)
        )

    def pending_messages(self):
        return self.db.execute(
            "SELECT * FROM outbox WHERE sent_at IS NULL AND next_attempt<=? ORDER BY rowid LIMIT 20",
            (time.time(),),
        ).fetchall()

    def delivered(self, event_id):
        self.db.execute("UPDATE outbox SET sent_at=? WHERE id=?", (time.time(), event_id))

    def defer(self, event_id, attempts, retry_after=0):
        delay = max(retry_after, min(3600, 2 ** min(attempts + 1, 12)))
        self.db.execute(
            "UPDATE outbox SET attempts=attempts+1,next_attempt=? WHERE id=?",
            (time.time() + delay, event_id),
        )

    def pending_count(self):
        return self.db.execute("SELECT count(*) FROM outbox WHERE sent_at IS NULL").fetchone()[0]
