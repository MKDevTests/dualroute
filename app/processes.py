"""Read-only socket/process attribution across host and container namespaces."""
from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

HOST_PROC = Path(os.getenv("DUALROUTE_HOST_PROC", "/host/proc"))


def proc_root() -> Path:
    return HOST_PROC if (HOST_PROC / "1/stat").exists() else Path("/proc")


def decode_address(value: str) -> tuple[str, int]:
    address, port = value.split(":")
    raw = bytes.fromhex(address)
    raw = b"".join(raw[index:index + 4][::-1] for index in range(0, len(raw), 4))
    family = socket.AF_INET if len(raw) == 4 else socket.AF_INET6
    return socket.inet_ntop(family, raw), int(port, 16)


class SocketIndex:
    def __init__(self, apps: list[dict[str, Any]]):
        self.exact: dict[tuple, dict] = {}
        self.listeners: dict[tuple, dict] = {}
        self.wildcards: dict[tuple, list] = {}
        self.host_visible = (HOST_PROC / "1/stat").exists()
        self.permission_errors = 0
        root = proc_root()
        try:
            self.host_namespace = str((root / "self/ns/net").stat().st_ino)
        except OSError:
            self.host_namespace = "host"
        self.namespace_ids = {}
        by_pid = {str(app["pid"]): app for app in apps if app.get("pid")}
        identities = {app["container_id"]: app for app in apps if app.get("container_id")}
        pending = {}
        namespaces = {self.host_namespace: root / "net"}
        for app in apps:
            if app.get("pid"):
                net = root / str(app["pid"]) / "net"
                try:
                    identity = str((root / str(app["pid"]) / "ns/net").stat().st_ino)
                    namespaces[identity] = net
                    self.namespace_ids[app["pid"]] = identity
                except OSError:
                    self.permission_errors += 1
        for namespace, net in namespaces.items():
            for filename, protocol in (("tcp", "TCP"), ("udp", "UDP"), ("tcp6", "TCP"), ("udp6", "UDP")):
                try:
                    lines = (net / filename).read_text().splitlines()[1:]
                except OSError:
                    continue
                for line in lines:
                    fields = line.split()
                    try:
                        local, local_port = decode_address(fields[1])
                        remote, remote_port = decode_address(fields[2])
                        inode = fields[9]
                    except (ValueError, IndexError, OSError):
                        continue
                    if inode != "0":
                        pending.setdefault(inode, []).append((namespace, protocol, local, local_port, remote, remote_port))
        try:
            entries = root.iterdir()
            for entry in entries:
                if not entry.name.isdecimal():
                    continue
                try:
                    owner = by_pid.get(entry.name)
                    if owner is None:
                        cgroup = (entry / "cgroup").read_text()
                        owner = next((app for identity, app in identities.items() if identity in cgroup), None)
                    process = (entry / "comm").read_text().strip()
                    for fd in (entry / "fd").iterdir():
                        try:
                            link = os.readlink(fd)
                        except OSError:
                            continue
                        if not link.startswith("socket:["):
                            continue
                        inode = link[8:-1]
                        for key in pending.get(inode, []):
                            metadata = {"process": process, "pid": int(entry.name), "app": owner}
                            self.exact[key] = metadata
                            if key[5] == 0:
                                self.listeners[(key[0], key[1], key[2], key[3])] = metadata
                                if key[2] in {"0.0.0.0", "::"}:
                                    self.wildcards.setdefault((key[0], key[1], key[3]), []).append(metadata)
                except PermissionError:
                    self.permission_errors += 1
                except (OSError, ValueError):
                    continue
        except OSError:
            pass

    def match(self, protocol: str, values: dict[str, str], local_addresses: set[str] | None = None, namespace_pid: int | None = None) -> tuple[dict, str] | None:
        namespace = self.namespace_ids.get(namespace_pid) if namespace_pid else self.host_namespace
        try:
            src, dst = values["src"], values["dst"]
            sport, dport = int(values.get("sport", 0)), int(values.get("dport", 0))
        except (KeyError, ValueError):
            return None
        for local, local_port, remote, remote_port, side in ((src, sport, dst, dport, "src"), (dst, dport, src, sport, "dst")):
            exact = self.exact.get((namespace, protocol, local, local_port, remote, remote_port))
            if exact:
                return exact, side
        for local, local_port, side in ((dst, dport, "dst"), (src, sport, "src")):
            listener = self.listeners.get((namespace, protocol, local, local_port))
            if listener:
                return listener, side
            candidates = []
            for metadata in self.wildcards.get((namespace, protocol, local_port), []):
                owner = metadata.get("app")
                addresses = set(owner.get("ips", []) + owner.get("shared_ips", [])) if owner else set()
                if not owner or owner.get("network_mode") == "host":
                    addresses.update(local_addresses or set())
                    addresses.update({"127.0.0.1", "::1"})
                if local in addresses:
                    candidates.append(metadata)
            identities = {(item["pid"], (item.get("app") or {}).get("id")) for item in candidates}
            if len(identities) == 1:
                return candidates[0], side
        return None


def network_counters(pid: int) -> dict[str, dict[str, int]]:
    if not pid:
        return {}
    try:
        text = (proc_root() / str(int(pid)) / "net/dev").read_text()
    except OSError:
        return {}
    result = {}
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, data = line.split(":", 1)
        fields = data.split()
        if len(fields) >= 9:
            result[name.strip()] = {"rx_bytes": int(fields[0]), "tx_bytes": int(fields[8])}
    return result
