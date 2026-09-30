from __future__ import annotations

import hashlib
import ipaddress
import platform
import shutil
import subprocess
from typing import Any
from pathlib import Path


ROUTE_MARKS = {0x65: "eth0", 0x66: "eth1"}
SERVICE_NAMES = {
    22: "SSH",
    53: "DNS",
    80: "HTTP",
    123: "NTP",
    139: "NetBIOS/SMB",
    443: "HTTPS",
    445: "SMB",
    853: "DNS chiffré",
    1900: "SSDP",
    32400: "Plex",
}


def _number(value: str | None) -> int:
    try:
        return int(value or "0", 16 if (value or "").startswith("0x") else 10)
    except ValueError:
        return 0


def parse_conntrack_line(line: str) -> dict[str, Any] | None:
    tokens = line.split()
    if tokens and tokens[0] in {"ipv4", "ipv6"}:
        tokens = tokens[2:]
    if len(tokens) < 4 or tokens[0] not in {"tcp", "udp", "icmp", "icmpv6", "sctp"}:
        return None

    tuples: list[dict[str, str]] = []
    current: dict[str, str] = {}
    metadata: dict[str, str] = {}
    tuple_keys = {"src", "dst", "sport", "dport", "packets", "bytes", "id", "type", "code"}
    for token in tokens[3:]:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key == "src" and "src" in current:
            tuples.append(current)
            current = {}
        if len(tuples) < 2 and key in tuple_keys:
            current[key] = value
        else:
            metadata[key] = value
    if current:
        tuples.append(current)
    if len(tuples) < 2:
        return None

    original, reply = tuples[:2]
    state = next((token for token in tokens[3:] if "=" not in token and not token.startswith("[")), "—")
    mark = _number(metadata.get("mark"))
    route_mark = mark & 0xFF
    identity = "|".join(
        [
            tokens[0],
            original.get("src", ""),
            original.get("sport", ""),
            original.get("dst", ""),
            original.get("dport", ""),
            original.get("id", ""),
            original.get("type", ""),
        ]
    )
    return {
        "id": hashlib.sha1(identity.encode(), usedforsecurity=False).hexdigest()[:16],
        "protocol": tokens[0].upper(),
        "state": state,
        "timeout_seconds": _number(tokens[2]),
        "original": original,
        "reply": reply,
        "mark": mark,
        "interface": ROUTE_MARKS.get(route_mark),
        "rule_id": mark >> 8 or None,
        "accounting": "bytes" in original and "bytes" in reply,
        "original_bytes": _number(original.get("bytes")),
        "reply_bytes": _number(reply.get("bytes")),
    }


def _endpoint(values: dict[str, str], address_key: str, port_key: str) -> str:
    address = values.get(address_key, "—")
    port = values.get(port_key)
    return f"{address}:{port}" if port else address


def attribute_flow(flow: dict[str, Any], apps: list[dict[str, Any]], local_addresses: set[str] | None = None) -> dict[str, Any]:
    ip_to_app = {
        address: app
        for app in apps
        for address in app.get("ips", [])
        if address
    }
    original = flow["original"]
    reply = flow["reply"]
    app = None
    local_addresses = local_addresses or set()
    direction = "entrant" if original.get("dst") in local_addresses else "sortant"

    if original.get("src") in ip_to_app:
        app = ip_to_app[original["src"]]
        direction = "sortant"
    elif original.get("dst") in ip_to_app:
        app = ip_to_app[original["dst"]]
        direction = "entrant"
    elif reply.get("src") in ip_to_app:
        app = ip_to_app[reply["src"]]
        direction = "entrant"
        original = {**original, "dst": reply["src"], "dport": reply.get("sport", "")}
    elif reply.get("dst") in ip_to_app:
        app = ip_to_app[reply["dst"]]
        direction = "sortant"
    else:
        local_port = _number(original.get("dport" if direction == "entrant" else "sport"))
        # Published Docker ports also identify host-network inbound connections.
        for candidate in apps:
            if direction != "entrant" or candidate.get("type") != "docker":
                continue
            if any(str(binding.get("HostPort")) == str(local_port)
                   and port.get("private", "").endswith("/" + flow["protocol"].lower())
                   for port in candidate.get("ports", []) for binding in port.get("public", [])):
                app = candidate
                break
        if not app and (local_port in {139, 445} or (not local_addresses and _number(original.get("dport")) == 445)):
            app = next((item for item in apps if item.get("id") == "service:smb"), None)
            direction = "entrant" if _number(original.get("dport")) in {139, 445} else "sortant"

    if direction == "sortant":
        local_endpoint = _endpoint(original, "src", "sport")
        remote_endpoint = _endpoint(original, "dst", "dport")
        remote_port = _number(original.get("dport"))
        tx_bytes = flow["original_bytes"]
        rx_bytes = flow["reply_bytes"]
    else:
        local_endpoint = _endpoint(original, "dst", "dport")
        remote_endpoint = _endpoint(original, "src", "sport")
        remote_port = _number(original.get("sport"))
        rx_bytes = flow["original_bytes"]
        tx_bytes = flow["reply_bytes"]

    flow.update(
        {
            "app_id": app.get("id") if app else "host:unattributed",
            "application": app.get("name") if app else "Hôte / non attribué",
            "container_id": (app.get("container_id") or "")[:12] if app else None,
            "app_type": app.get("type") if app else "host",
            "direction": direction,
            "local_endpoint": local_endpoint,
            "remote_endpoint": remote_endpoint,
            "service": (
                app.get("name")
                if app and (direction == "entrant" or app.get("id") == "service:smb")
                else SERVICE_NAMES.get(remote_port, f"Port {remote_port}" if remote_port else "—")
            ),
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
        }
    )
    flow.pop("original", None)
    flow.pop("reply", None)
    return flow


def discover_flows(apps: list[dict[str, Any]], limit: int = 1000, interfaces: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if platform.system() != "Linux" or shutil.which("conntrack") is None:
        return {
            "available": False,
            "message": "Le détail des connexions est disponible sur le NAS Linux avec conntrack.",
            "items": [],
        }
    try:
        result = subprocess.run(
            ["conntrack", "-L", "-f", "ipv4", "-o", "extended"],
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "message": f"Lecture conntrack impossible : {exc}", "items": []}
    if result.returncode:
        message = result.stderr.strip() or "La table conntrack n’est pas accessible."
        return {"available": False, "message": message, "items": []}

    interfaces = interfaces or []
    local_addresses = {item["address"] for item in interfaces if item.get("address")}
    items = []
    for line in result.stdout.splitlines():
        parsed = parse_conntrack_line(line)
        if parsed:
            attributed = attribute_flow(parsed, apps, local_addresses)
            if not attributed["interface"]:
                # A connected subnet is a reliable route hint, not proof of policy routing.
                destination = attributed["remote_endpoint"].split(":")[0]
                matches = []
                for interface in interfaces:
                    try:
                        network = ipaddress.ip_network(interface.get("network") or "", strict=False)
                        if ipaddress.ip_address(destination) in network:
                            matches.append((network.prefixlen, interface["name"]))
                    except ValueError:
                        continue
                if matches:
                    attributed["interface"] = max(matches)[1]
                    attributed["interface_source"] = "Sous-réseau connecté (estimé)"
            else:
                attributed["interface_source"] = "Marque de routage conntrack"
            items.append(attributed)
    items.sort(key=lambda item: item["rx_bytes"] + item["tx_bytes"], reverse=True)
    missing = sum(not item["accounting"] for item in items)
    try:
        accounting_enabled = Path("/proc/sys/net/netfilter/nf_conntrack_acct").read_text().strip() == "1"
    except OSError:
        accounting_enabled = False
    return {
        "available": True,
        "message": f"{len(items)} connexions IPv4 actives détectées.",
        "accounting_enabled": accounting_enabled,
        "accounting_missing": missing,
        "total": len(items),
        "truncated": len(items) > limit,
        "items": items[:limit],
    }
