"""Observe Gluetun without changing its configuration or restarting its tunnel."""
from __future__ import annotations

import base64
import ipaddress
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .processes import network_counters


class VPNMonitor:
    def __init__(self, store):
        self.store = store
        self.previous = {}
        self.api_cache = {}
        self.http = build_opener(ProxyHandler({}))

    def read_api(self, app):
        key = self.store.get_setting("secret:vpn:" + app["docker_name"], "")
        auth = app.get("_vpn_auth")
        auth = auth if isinstance(auth, dict) else {}
        headers = {}
        if key or auth.get("auth") == "apikey":
            headers["X-API-Key"] = key or auth.get("apikey", "")
        elif auth.get("auth") == "basic":
            credentials = f"{auth.get('username', '')}:{auth.get('password', '')}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode()
        ip = next(iter(app.get("ips", [])), "127.0.0.1" if app.get("network_mode") == "host" else None)
        result = {"public_ip": None, "api_status": None, "api_message": "API non accessible", "api_key_configured": bool(key or headers)}
        try:
            ip = str(ipaddress.ip_address(ip))
            port = int(app.get("vpn_control_port") or 8000)
            if not 1 <= port <= 65535:
                return result
            address = f"[{ip}]" if ":" in ip else ip
            for path, field, response_key in (("/v1/vpn/status", "api_status", "status"), ("/v1/publicip/ip", "public_ip", "public_ip")):
                request = Request(f"http://{address}:{port}{path}", headers=headers)
                with self.http.open(request, timeout=2) as response:
                    value = json.loads(response.read(16384)).get(response_key)
                    result[field] = str(ipaddress.ip_address(value)) if field == "public_ip" else value
            result["api_message"] = "API de surveillance accessible"
        except HTTPError as exc:
            result["api_message"] = "Clé API requise ou droits GET insuffisants" if exc.code in {401, 403} else f"API : HTTP {exc.code}"
        except (OSError, URLError, ValueError, TypeError):
            pass
        return result

    def collect(self, apps):
        result = []
        now = time.monotonic()
        active = set()
        for app in apps:
            if not app.get("is_vpn"):
                continue
            identity = app["id"]
            active.add(identity)
            counters = network_counters(app.get("pid", 0))
            tunnels = {name: value for name, value in counters.items() if name.startswith(("tun", "wg", "wireguard"))}
            rx = sum(item["rx_bytes"] for item in tunnels.values())
            tx = sum(item["tx_bytes"] for item in tunnels.values())
            previous = self.previous.get(identity)
            measured = bool(tunnels and previous and previous[3] == app.get("pid") and previous[4] == tuple(tunnels) and rx >= previous[1] and tx >= previous[2])
            elapsed = max(now - previous[0], .001) if previous else 1
            metric = {"rx_bytes": rx, "tx_bytes": tx, "rx_bps": (rx - previous[1])*8/elapsed if measured else None,
                      "tx_bps": (tx - previous[2])*8/elapsed if measured else None}
            self.previous[identity] = (now, rx, tx, app.get("pid"), tuple(tunnels))
            cached = self.api_cache.get(identity)
            if not cached or now - cached[0] >= 30:
                api = self.read_api(app) if app.get("status") == "running" else {"public_ip": None, "api_status": None, "api_message": "Conteneur arrêté"}
                self.api_cache[identity] = (now, api)
            else:
                api = cached[1]
            health = app.get("health", "unknown")
            status = "unknown"
            if app.get("status") not in {"running", "restarting"} or api.get("api_status") == "stopped":
                status = "stopped"
            elif app.get("status") == "restarting" or health == "unhealthy":
                status = "reconnecting"
            elif health == "starting":
                status = "connecting"
            elif health == "healthy" and tunnels:
                status = "connected"
            linked = [{"id": item["id"], "name": item["name"]} for item in apps if item.get("vpn_id") == identity]
            result.append({"id": identity, "name": app["docker_name"], "status": status, "container_status": app["status"],
                           "health": health, "type": app.get("vpn_type"), "provider": app.get("vpn_provider"),
                           "interfaces": list(tunnels), "metric": metric, "apps": linked,
                           "history_key": "vpn:" + app["docker_name"], "observable": bool(tunnels), **api})
        self.previous = {key: value for key, value in self.previous.items() if key in active}
        self.api_cache = {key: value for key, value in self.api_cache.items() if key in active}
        return result
