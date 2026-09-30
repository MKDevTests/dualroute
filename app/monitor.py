"""Independent cached collectors keep HTTP responses fast even when Docker stalls."""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import subprocess
import time
from datetime import UTC, datetime

from .discovery import container_stats, discover_apps, discover_interfaces
from .processes import SocketIndex, proc_root
from .traffic import discover_flows
from .vpn import VPNMonitor


class RateTracker:
    def __init__(self):
        self.previous = {}

    def apply(self, items, timestamp):
        next_sample = {}
        for flow in items:
            old = self.previous.get(flow["id"])
            valid = bool(flow.get("accounting") and old and old[3] and timestamp > old[0]
                         and flow["rx_bytes"] >= old[1] and flow["tx_bytes"] >= old[2])
            elapsed = timestamp - old[0] if old else 1
            flow["rx_bps"] = (flow["rx_bytes"] - old[1])*8/elapsed if valid else None
            flow["tx_bps"] = (flow["tx_bytes"] - old[2])*8/elapsed if valid else None
            flow["rate_status"] = "measured" if valid else "warming" if flow.get("accounting") else "missing"
            next_sample[flow["id"]] = (timestamp, flow["rx_bytes"], flow["tx_bytes"], flow.get("accounting"))
        self.previous = next_sample


class Monitor:
    def __init__(self, store, metrics):
        self.store, self.metrics = store, metrics
        self.interfaces, self.apps, self.vpns = [], [], []
        self.stats = {}
        self.status = {}
        self.flows = {"available": False, "message": "Collecte des premières connexions…", "items": [], "vpn_status": {}}
        self.flow_requested_at = 0
        self.rates = RateTracker()
        self.vpn_monitor = VPNMonitor(store)
        self._accounted = set()
        self.accounting_errors = {}
        self._sockets = None
        self._sockets_at = 0

    async def loop(self, name, function, interval):
        while True:
            try:
                await asyncio.to_thread(function)
                self.status[name] = {"state": "ready", "updated_at": datetime.now(UTC).isoformat()}
            except Exception as exc:
                previous = self.status.get(name, {})
                self.status[name] = {"state": "error", "message": str(exc)}
                if previous.get("state") != "error" or previous.get("message") != str(exc):
                    logging.getLogger(__name__).exception("Collecte %s indisponible", name)
            await asyncio.sleep(interval)

    def collect_interfaces(self):
        self.interfaces = discover_interfaces()

    def collect_apps(self):
        self.apps = discover_apps()

    def collect_stats(self):
        if self.apps:
            self.stats = container_stats(self.apps)

    def collect_vpns(self):
        self.vpns = self.vpn_monitor.collect(self.apps)
        self.metrics.vpn_metrics = {vpn["history_key"]: vpn["metric"] for vpn in self.vpns if vpn["observable"]}

    def ensure_accounting(self, pid=None):
        identity = int(pid or 0)
        if identity in self._accounted or platform.system() != "Linux" or os.getenv("DUALROUTE_ENABLE_ACCOUNTING", "1") != "1":
            return
        prefix = ["nsenter", f"--net={proc_root() / str(identity) / 'ns/net'}", "--"] if identity else []
        check = subprocess.run(prefix + ["sysctl", "-n", "net.netfilter.nf_conntrack_acct"], capture_output=True, text=True, timeout=3)
        if check.returncode == 0 and check.stdout.strip() != "1":
            # Docker protects /proc/sys with a readonly mount. The explicit writable
            # network-sysctl bind is resolved after setns, so each net namespace has its own setting.
            check = subprocess.run(prefix + ["python", "-c", "from pathlib import Path; Path('/host/sys/net/netfilter/nf_conntrack_acct').write_text('1')"], capture_output=True, text=True, timeout=3)
            if check.returncode == 0:
                check = subprocess.run(prefix + ["sysctl", "-n", "net.netfilter.nf_conntrack_acct"], capture_output=True, text=True, timeout=3)
                if check.stdout.strip() != "1":
                    check.returncode = 1
                    check.stderr = "Le compteur de cet espace réseau n’a pas été activé"
        if check.returncode == 0:
            self._accounted.add(identity)
            self.accounting_errors.pop(identity, None)
        else:
            self.accounting_errors[identity] = check.stderr.strip().splitlines()[-1] if check.stderr.strip() else "Activation refusée"

    def collect_flows(self):
        if time.monotonic() - self.flow_requested_at > 30:
            return
        apps, interfaces = self.apps, self.interfaces
        if not self._sockets or time.monotonic() - self._sockets_at >= 15:
            self._sockets = SocketIndex(apps)
            self._sockets_at = time.monotonic()
        try:
            self.ensure_accounting()
        except (OSError, subprocess.SubprocessError):
            pass
        result = discover_flows(apps, 10000, interfaces, self._sockets)
        result["accounting_message"] = self.accounting_errors.get(0)
        items = result["items"]
        if not result["available"]:
            # Keep last values and timestamp so a permission/read error is visible as stale data.
            self.flows = {**self.flows, "available": False, "message": result["message"]}
            raise RuntimeError(result["message"])
        for item in items:
            item["scope"] = "host"
            item["id"] = "host:" + item["id"]
        vpn_status = {}
        for app in apps:
            if not app.get("is_vpn") or not app.get("pid"):
                continue
            try:
                self.ensure_accounting(app["pid"])
            except (OSError, subprocess.SubprocessError):
                pass
            inner = discover_flows(apps, 10000, [], self._sockets, app["pid"])
            inner["accounting_message"] = self.accounting_errors.get(app["pid"])
            vpn_status[app["id"]] = {key: value for key, value in inner.items() if key != "items"}
            for item in inner["items"]:
                item.update(id=f"{app['id']}:{item['id']}", scope="vpn", vpn_id=app["id"], interface=None,
                            interface_source="Espace réseau du VPN")
                if item["app_id"] == "host:unattributed" or item["app_id"] == app["id"]:
                    item.update(application=f"{app['docker_name']} · application non résolue", app_id="vpn:unresolved:" + app["id"])
                items.append(item)
        now = time.monotonic()
        self.rates.apply(items, now)
        result.update(items=items, total=len(items), vpn_status=vpn_status, collected_at=datetime.now(UTC).isoformat(),
                      host_processes_visible=self._sockets.host_visible, process_permission_errors=self._sockets.permission_errors)
        self.flows = result

    def tasks(self):
        return [asyncio.create_task(self.loop(name, function, interval)) for name, function, interval in
                (("interfaces", self.collect_interfaces, 15), ("docker", self.collect_apps, 20),
                 ("statistics", self.collect_stats, 30), ("vpns", self.collect_vpns, 5), ("flows", self.collect_flows, 5))]
