from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import psutil

from .db import Store


class MetricsCollector:
    def __init__(self, store: Store):
        self.store = store
        self.latest: dict[str, dict[str, float | int]] = {}
        self._previous: dict[str, tuple[float, int, int]] = {}
        self.running = True

    async def run(self) -> None:
        prune_counter = 0
        while self.running:
            interval = int(self.store.get_setting("sample_interval_seconds", 2))
            now = datetime.now(UTC)
            timestamp = now.timestamp()
            rows = []
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
                self.latest[name] = {
                    "rx_bps": rx_bps,
                    "tx_bps": tx_bps,
                    "rx_bytes": counter.bytes_recv,
                    "tx_bytes": counter.bytes_sent,
                }
                rows.append((now.isoformat(), name, rx_bps, tx_bps, counter.bytes_recv, counter.bytes_sent))
            self.store.add_samples(rows)
            prune_counter += 1
            if prune_counter >= max(30, 3600 // max(interval, 1)):
                self.store.prune_samples(int(self.store.get_setting("retention_days", 30)))
                prune_counter = 0
            await asyncio.sleep(interval)

    def history(self, hours: int) -> list[dict]:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        return self.store.samples(since)
