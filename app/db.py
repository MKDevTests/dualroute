from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._init()

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id TEXT NOT NULL,
                    app_name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    primary_interface TEXT NOT NULL,
                    fallback_interface TEXT,
                    qos INTEGER NOT NULL DEFAULT 3,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    download_limit_mbps INTEGER,
                    upload_limit_mbps INTEGER,
                    ports_json TEXT NOT NULL DEFAULT '[]',
                    weight_primary INTEGER NOT NULL DEFAULT 50,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS rules_app_id ON rules(app_id);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS samples (
                    ts TEXT NOT NULL,
                    interface TEXT NOT NULL,
                    rx_bps REAL NOT NULL,
                    tx_bps REAL NOT NULL,
                    rx_bytes INTEGER NOT NULL,
                    tx_bytes INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS network_configs (
                    interface TEXT PRIMARY KEY,
                    address TEXT NOT NULL,
                    prefix INTEGER NOT NULL,
                    gateway TEXT NOT NULL,
                    dns_json TEXT NOT NULL,
                    mtu INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
        defaults = {
            "enforcement_enabled": False,
            "sample_interval_seconds": 5,
            "history_interval_seconds": 60,
            "retention_days": 30,
            "display_rate_unit": "mbps",
            "dashboard_managed_only": True,
        }
        for key, value in defaults.items():
            if self.get_setting(key) is None:
                self.set_setting(key, value)
        if self.get_setting("monitoring_schema", 0) < 1:
            self.set_setting("sample_interval_seconds", max(5, self.get_setting("sample_interval_seconds", 5)))
            self.set_setting("monitoring_schema", 1)

    @staticmethod
    def _rule(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["ports"] = json.loads(item.pop("ports_json"))
        item["enabled"] = bool(item["enabled"])
        return item

    def list_rules(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM rules ORDER BY id").fetchall()
        return [self._rule(row) for row in rows]

    def save_rule(self, payload: dict[str, Any], rule_id: int | None = None) -> dict[str, Any]:
        values = dict(payload)
        values["ports_json"] = json.dumps(values.pop("ports", []))
        values["enabled"] = int(values.get("enabled", True))
        values["updated_at"] = now_iso()
        columns = [
            "app_id", "app_name", "direction", "strategy", "primary_interface",
            "fallback_interface", "qos", "enabled", "download_limit_mbps",
            "upload_limit_mbps", "ports_json", "weight_primary", "updated_at",
        ]
        with self._lock, self.connect() as db:
            if rule_id is None:
                placeholders = ",".join("?" for _ in columns)
                db.execute(
                    f"INSERT INTO rules ({','.join(columns)}) VALUES ({placeholders}) "
                    "ON CONFLICT(app_id) DO UPDATE SET "
                    + ",".join(f"{column}=excluded.{column}" for column in columns if column != "app_id"),
                    [values.get(column) for column in columns],
                )
                row = db.execute("SELECT * FROM rules WHERE app_id=?", (values["app_id"],)).fetchone()
            else:
                assignments = ",".join(f"{column}=?" for column in columns)
                db.execute(
                    f"UPDATE rules SET {assignments} WHERE id=?",
                    [values.get(column) for column in columns] + [rule_id],
                )
                row = db.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        if row is None:
            raise KeyError(rule_id)
        return self._rule(row)

    def delete_rule(self, rule_id: int) -> bool:
        with self._lock, self.connect() as db:
            cursor = db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        return cursor.rowcount > 0

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO settings(key,value_json) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, json.dumps(value)),
            )

    def settings(self) -> dict[str, Any]:
        with self.connect() as db:
            rows = db.execute("SELECT key,value_json FROM settings").fetchall()
        return {row["key"]: json.loads(row["value_json"]) for row in rows if not row["key"].startswith("secret:")}

    def add_samples(self, rows: list[tuple[str, str, float, float, int, int]]) -> None:
        if not rows:
            return
        with self._lock, self.connect() as db:
            db.executemany(
                "INSERT INTO samples(ts,interface,rx_bps,tx_bps,rx_bytes,tx_bytes) VALUES (?,?,?,?,?,?)",
                rows,
            )

    def samples(self, since: str, limit: int = 5000, bucket_seconds: int = 60) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT strftime('%Y-%m-%dT%H:%M:%S+00:00',
                       CAST(strftime('%s', ts) AS INTEGER)/?*?, 'unixepoch') AS ts,
                       interface, AVG(rx_bps) AS rx_bps, AVG(tx_bps) AS tx_bps,
                       MAX(rx_bytes) AS rx_bytes, MAX(tx_bytes) AS tx_bytes
                   FROM samples WHERE ts>=? AND
                       (interface IN ('eth0','eth1','tailscale','tailscale0') OR interface LIKE 'vpn:%')
                   GROUP BY interface, CAST(strftime('%s', ts) AS INTEGER)/?
                   ORDER BY ts DESC LIMIT ?""", (bucket_seconds, bucket_seconds, since, bucket_seconds, limit)
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def prune_samples(self, retention_days: int) -> None:
        cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self._lock, self.connect() as db:
            db.execute("DELETE FROM samples WHERE ts<?", (cutoff,))

    def event(self, level: str, source: str, message: str, details: dict[str, Any] | None = None) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT INTO events(ts,level,source,message,details_json) VALUES (?,?,?,?,?)",
                (now_iso(), level, source, message, json.dumps(details or {})),
            )

    def events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result

    def save_network_config(self, payload: dict[str, Any]) -> None:
        with self._lock, self.connect() as db:
            db.execute(
                """INSERT INTO network_configs(interface,address,prefix,gateway,dns_json,mtu,updated_at)
                   VALUES (?,?,?,?,?,?,?) ON CONFLICT(interface) DO UPDATE SET
                   address=excluded.address,prefix=excluded.prefix,gateway=excluded.gateway,
                   dns_json=excluded.dns_json,mtu=excluded.mtu,updated_at=excluded.updated_at""",
                (
                    payload["interface"], payload["address"], payload["prefix"], payload["gateway"],
                    json.dumps(payload.get("dns", [])), payload.get("mtu", 1500), now_iso(),
                ),
            )

    def network_configs(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM network_configs ORDER BY interface").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["dns"] = json.loads(item.pop("dns_json"))
            result.append(item)
        return result
