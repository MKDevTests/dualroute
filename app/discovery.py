from __future__ import annotations

import ipaddress
import json
import os
import platform
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psutil


MANAGED_INTERFACES = {"eth0", "eth1", "tailscale0", "tailscale"}


def is_managed_interface(name: str) -> bool:
    return name.lower() in MANAGED_INTERFACES


def interface_order(name: str) -> int:
    lowered = name.lower()
    if lowered == "eth0":
        return 0
    if lowered == "eth1":
        return 1
    return 2


def _run_json(command: list[str]) -> list[dict[str, Any]]:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=4, check=True)
        return json.loads(result.stdout or "[]")
    except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError):
        return []


def _default_routes() -> dict[str, dict[str, Any]]:
    routes: dict[str, dict[str, Any]] = {}
    for route in _run_json(["ip", "-j", "route", "show", "default"]):
        device = route.get("dev")
        if device and device not in routes:
            routes[device] = {
                "gateway": route.get("gateway"),
                "metric": route.get("metric", 0),
                "protocol": route.get("protocol", ""),
            }
    return routes


def discover_interfaces() -> list[dict[str, Any]]:
    defaults = _default_routes()
    stats = psutil.net_if_stats()
    counters = psutil.net_io_counters(pernic=True)
    linux_links = {item.get("ifname"): item for item in _run_json(["ip", "-j", "link", "show"])}
    linux_addresses = {item.get("ifname"): item for item in _run_json(["ip", "-j", "addr", "show"])}
    interfaces: list[dict[str, Any]] = []

    for name, addresses in psutil.net_if_addrs().items():
        if name.lower() == "lo":
            continue
        ipv4 = next((addr for addr in addresses if addr.family == socket.AF_INET), None)
        link = linux_links.get(name, {})
        addr_info = linux_addresses.get(name, {})
        mac = link.get("address") or next(
            (addr.address for addr in addresses if getattr(psutil, "AF_LINK", object()) == addr.family), ""
        )
        prefix = None
        if ipv4 and ipv4.netmask:
            try:
                prefix = ipaddress.IPv4Network(f"0.0.0.0/{ipv4.netmask}").prefixlen
            except ValueError:
                prefix = None
        oper = stats.get(name)
        io = counters.get(name)
        route = defaults.get(name, {})
        interfaces.append(
            {
                "name": name,
                "managed": is_managed_interface(name),
                "kind": (link.get("linkinfo") or {}).get("info_kind") or "physical",
                "address": ipv4.address if ipv4 else None,
                "prefix": prefix,
                "network": str(ipaddress.ip_interface(f"{ipv4.address}/{prefix}").network) if ipv4 and prefix else None,
                "gateway": route.get("gateway"),
                "default": name in defaults,
                "metric": route.get("metric"),
                "up": bool(oper and oper.isup),
                "speed_mbps": oper.speed if oper else 0,
                "mtu": oper.mtu if oper else addr_info.get("mtu", 1500),
                "mac": mac,
                "rx_bytes": io.bytes_recv if io else 0,
                "tx_bytes": io.bytes_sent if io else 0,
            }
        )
    interfaces.sort(key=lambda item: (not item["managed"], interface_order(item["name"]), item["name"].lower()))
    return interfaces


def _docker_client():
    try:
        import docker

        socket_path = os.getenv("DUALROUTE_DOCKER_SOCKET", "/var/run/docker.sock")
        if platform.system() == "Linux" and os.path.exists(socket_path):
            return docker.DockerClient(base_url=f"unix://{socket_path}", timeout=4)
        return docker.from_env(timeout=4)
    except Exception:
        return None


def discover_apps() -> list[dict[str, Any]]:
    apps: list[dict[str, Any]] = []
    client = _docker_client()
    if client is None and platform.system() == "Linux":
        raise RuntimeError("Docker inaccessible ; vérifiez le montage de /var/run/docker.sock")
    if client is not None:
        try:
            for container in client.containers.list(all=True):
                attrs = container.attrs
                image = attrs.get("Config", {}).get("Image", "")
                is_vpn = image.split("/")[-1].split(":")[0].split("@")[0] == "gluetun"
                if container.status not in {"running", "restarting"} and not is_vpn:
                    continue
                networks = attrs.get("NetworkSettings", {}).get("Networks", {})
                ips = sorted({data.get("IPAddress") for data in networks.values() if data.get("IPAddress")})
                ports = []
                for private, bindings in (attrs.get("NetworkSettings", {}).get("Ports") or {}).items():
                    ports.append({"private": private, "public": bindings or []})
                labels = attrs.get("Config", {}).get("Labels") or {}
                display = (
                    labels.get("com.docker.compose.service")
                    or labels.get("org.opencontainers.image.title")
                    or container.name
                )
                if display in {"app", "web", "server"}:
                    display = container.name
                env = dict(item.split("=", 1) for item in attrs.get("Config", {}).get("Env", []) if "=" in item) if is_vpn else {}
                try:
                    auth = json.loads(env.get("HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE", "{}"))
                except (ValueError, TypeError):
                    auth = {}
                apps.append(
                    {
                        "id": container.id[:12],
                        "container_id": container.id,
                        "name": display,
                        "type": "docker",
                        "status": container.status,
                        "image": image,
                        "ips": ips,
                        "ports": ports,
                        "networks": sorted(networks.keys()),
                        "docker_name": container.name,
                        "pid": attrs.get("State", {}).get("Pid", 0),
                        "health": attrs.get("State", {}).get("Health", {}).get("Status", "unknown"),
                        "network_mode": attrs.get("HostConfig", {}).get("NetworkMode", ""),
                        "is_vpn": is_vpn,
                        "vpn_type": env.get("VPN_TYPE", "openvpn") if is_vpn else None,
                        "vpn_provider": env.get("VPN_SERVICE_PROVIDER", "custom") if is_vpn else None,
                        "vpn_control_port": env.get("HTTP_CONTROL_SERVER_ADDRESS", ":8000").rsplit(":", 1)[-1] if is_vpn else None,
                        "_vpn_auth": auth,
                    }
                )
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Découverte Docker indisponible")
            raise RuntimeError("Docker ne répond pas ; dernières applications conservées") from None
        finally:
            client.close()

    by_identity = {key: app for app in apps for key in (app["id"], app["container_id"], app["docker_name"])}
    for app in apps:
        mode = app.get("network_mode", "")
        if mode.startswith("container:"):
            owner = by_identity.get(mode.split(":", 1)[1])
            if owner:
                app["network_owner_id"] = owner["id"]
                app["vpn_id"] = owner["id"] if owner["is_vpn"] else None
                app["shared_ips"] = owner["ips"]

    apps.append(
        {
            "id": "service:smb",
            "container_id": None,
            "name": "SMB",
            "type": "system",
            "status": "detected" if _port_listening(445) else "available",
            "image": "Service système",
            "ips": [],
            "ports": [{"private": "445/tcp", "public": []}],
            "networks": ["host"],
        }
    )
    return sorted(apps, key=lambda item: (item["type"] == "system", item["name"].lower()))


def _port_listening(port: int) -> bool:
    try:
        return any(connection.laddr.port == port for connection in psutil.net_connections(kind="inet"))
    except (psutil.AccessDenied, OSError):
        return False


def container_stats(apps: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    client = _docker_client()
    if client is None:
        return {}
    result: dict[str, dict[str, int]] = {}
    def read(app):
        try:
            # one_shot avoids waiting for a second Docker sample for each container.
            stats = client.api.stats(app["container_id"], stream=False, one_shot=True)
            networks = stats.get("networks", {})
            return app["id"], {
                "rx_bytes": sum(item.get("rx_bytes", 0) for item in networks.values()),
                "tx_bytes": sum(item.get("tx_bytes", 0) for item in networks.values()),
            }
        except Exception:
            return app["id"], None
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for identity, stats in pool.map(read, [item for item in apps if item.get("container_id") and item.get("status") == "running"]):
                if stats is not None:
                    result[identity] = stats
    finally:
        client.close()
    return result


def public_apps(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if not key.startswith("_")} for item in items]
