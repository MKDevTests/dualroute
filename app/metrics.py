from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import psutil

from .db import Store
from .discovery import is_managed_interface


class MetricsCollector:
    def __init__(self, store: Store):
        self.store = store
        self.latest: dict[str, dict] = {}
        self.vpn_metrics: dict[str, dict] = {}
        self._previous: dict[str, tuple] = {}
        self._pending: dict[str, list] = {}
        self._last_save = time.monotonic()
        self._last_prune = time.monotonic()
        self.running = True

    def collect(self) -> None:
        now = datetime.now(UTC)
        timestamp = now.timestamp()
        latest = {}
        for name, counter in psutil.net_io_counters(pernic=True).items():
            if name.lower() == "lo":
                continue
            previous = self._previous.get(name)
            rx_bps = tx_bps = 0.0
            if previous:
                elapsed = max(timestamp - previous[0], 0.001)
                rx_bps = max(0, counter.bytes_recv - previous[1]) * 8 / elapsed
                tx_bps = max(0, counter.bytes_sent - previous[2]) * 8 / elapsed
            self._previous[name] = (timestamp, counter.bytes_recv, counter.bytes_sent)
            latest[name] = dict(rx_bps=rx_bps, tx_bps=tx_bps, rx_bytes=counter.bytes_recv, tx_bytes=counter.bytes_sent)
        self.latest = latest
        recorded = {name: item for name, item in latest.items() if is_managed_interface(name)}
        recorded.update(self.vpn_metrics)
        for name, item in recorded.items():
            if item.get("rx_bps") is None:
                continue
            weight = max(0.001, timestamp - getattr(self, "_last_collect", timestamp - 5))
            pending = self._pending.setdefault(name, [0.0, 0.0, 0.0, 0, 0])
            pending[0] += item["rx_bps"] * weight
            pending[1] += item["tx_bps"] * weight
            pending[2] += weight
            pending[3:] = [item["rx_bytes"], item["tx_bytes"]]
        self._last_collect = timestamp
        if time.monotonic() - self._last_save >= self.store.get_setting("history_interval_seconds", 60):
            self.store.add_samples([(now.isoformat(), name, p[0]/p[2], p[1]/p[2], p[3], p[4]) for name, p in self._pending.items()])
            self._pending.clear()
            self._last_save = time.monotonic()
        if time.monotonic() - self._last_prune >= 3600:
            self.store.prune_samples(self.store.get_setting("retention_days", 30))
            self._last_prune = time.monotonic()

    async def run(self) -> None:
        while self.running:
            try:
                await asyncio.to_thread(self.collect)
            except Exception:
                logging.getLogger(__name__).exception("Collecte des interfaces interrompue, nouvelle tentative au prochain cycle")
            await asyncio.sleep(self.store.get_setting("sample_interval_seconds", 5))

    def history(self, hours: int) -> list[dict]:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        bucket = max(self.store.get_setting("history_interval_seconds", 60), hours * 3600 // 300)
        return self.store.samples(since, bucket_seconds=bucket)
