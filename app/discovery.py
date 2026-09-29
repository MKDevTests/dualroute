from __future__ import annotations

import ipaddress
import json
import os
import platform
import socket
import subprocess
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
        if not is_managed_interface(name):
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
    interfaces.sort(key=lambda item: interface_order(item["name"]))
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
    if client is not None:
        try:
            for container in client.containers.list(all=False):
                attrs = container.attrs
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
                apps.append(
                    {
                        "id": container.id[:12],
                        "container_id": container.id,
                        "name": display,
                        "type": "docker",
                        "status": container.status,
                        "image": attrs.get("Config", {}).get("Image", ""),
                        "ips": ips,
                        "ports": ports,
                        "networks": sorted(networks.keys()),
                    }
                )
        except Exception:
            pass

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
    try:
        for app in apps:
            if not app.get("container_id"):
                continue
            stats = client.containers.get(app["container_id"]).stats(stream=False)
            networks = stats.get("networks", {})
            result[app["id"]] = {
                "rx_bytes": sum(item.get("rx_bytes", 0) for item in networks.values()),
                "tx_bytes": sum(item.get("tx_bytes", 0) for item in networks.values()),
            }
    except Exception:
        return result
    return result
