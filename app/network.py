from __future__ import annotations

import ipaddress
import platform
import re
import shutil
import subprocess
import threading
from copy import deepcopy
from dataclasses import dataclass
from typing import Any


MARK_ETH0 = 0x65
MARK_ETH1 = 0x66
TABLE_ETH0 = 101
TABLE_ETH1 = 102
PRIVATE_NETWORKS = "{ 10.0.0.0/8, 100.64.0.0/10, 172.16.0.0/12, 192.168.0.0/16 }"
EXECUTION_LOCK = threading.Lock()


class NetworkError(RuntimeError):
    pass


@dataclass
class Command:
    argv: list[str]
    stdin: str | None = None
    ignore_failure: bool = False

    def display(self) -> str:
        if self.stdin:
            return f"{' '.join(self.argv)} << nft-script"
        return " ".join(self.argv)


def route_capable_interfaces(interfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {item["name"].lower(): item for item in interfaces}
    return [
        by_name[name]
        for name in ("eth0", "eth1")
        if name in by_name
        and by_name[name].get("up")
        and by_name[name].get("address")
        and by_name[name].get("gateway")
    ]


def validate_distinct_networks(interfaces: list[dict[str, Any]]) -> list[str]:
    status = network_status(interfaces)
    return [status["message"]] if status["warning"] else []


def network_status(interfaces: list[dict[str, Any]]) -> dict[str, Any]:
    physical = {item["name"].lower(): item for item in interfaces if item["name"].lower() in {"eth0", "eth1"}}
    unavailable = [name.upper() for name in ("eth0", "eth1") if name not in physical or not physical[name].get("up") or not physical[name].get("address")]
    if unavailable:
        return {"mode": "incomplete", "warning": True, "title": "Interface absente ou non configurée",
                "message": f"{', '.join(unavailable)} : interface absente, inactive ou sans adresse IPv4. Aucune adresse ni passerelle n’est proposée automatiquement."}
    networks = [item.get("network") for item in physical.values() if item.get("network")]
    if len(networks) == 2 and len(set(networks)) == 1:
        return {"mode": "overlap", "warning": True, "title": "Même sous-réseau sur les deux cartes",
                "message": f"ETH0 et ETH1 utilisent le même sous-réseau {networks[0]}. Des routes et réponses ambiguës sont possibles ; vérifiez la topologie avant de modifier une adresse."}
    local = [name.upper() for name, item in physical.items() if not item.get("gateway")]
    if local:
        return {"mode": "local", "warning": False, "title": "Réseau local uniquement · " + ", ".join(local),
                "message": f"{', '.join(local)} sans passerelle : accès au sous-réseau local et à SMB, configuration valide. Le double routage Internet nécessite une passerelle réelle sur chaque carte ; DualRoute peut l’utiliser dans ses tables dédiées sans ajouter une deuxième route par défaut au NAS."}
    return {"mode": "dual", "warning": False, "title": "Deux sorties Internet configurées",
            "message": "ETH0 et ETH1 ont une adresse et une passerelle sur des sous-réseaux distincts. Les passerelles doivent être testées avant l’application des règles."}


def _app_ips(apps: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {app["id"]: app.get("ips", []) for app in apps}


def _mark(rule_id: int, route_mark: int) -> int:
    return ((rule_id & 0xFFFF) << 8) | route_mark


def _target_marks(rule: dict[str, Any], interface_names: list[str]) -> tuple[int, int | None]:
    primary_index = interface_names.index(rule["primary_interface"])
    primary = MARK_ETH0 if primary_index == 0 else MARK_ETH1
    fallback = None
    if rule.get("fallback_interface") in interface_names:
        fallback_index = interface_names.index(rule["fallback_interface"])
        fallback = MARK_ETH0 if fallback_index == 0 else MARK_ETH1
    return _mark(rule["id"], primary), _mark(rule["id"], fallback) if fallback else None


def resolve_rules_for_health(
    rules: list[dict[str, Any]], health: dict[str, bool]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    resolved = deepcopy(rules)
    changes: list[dict[str, str]] = []
    for rule in resolved:
        if not rule.get("enabled"):
            continue
        primary = rule.get("primary_interface")
        fallback = rule.get("fallback_interface")
        if rule.get("strategy") in ("prefer", "qos") and fallback:
            if not health.get(primary, False) and health.get(fallback, False):
                rule["primary_interface"], rule["fallback_interface"] = fallback, primary
                changes.append({"app": rule["app_name"], "from": primary, "to": fallback})
        elif rule.get("strategy") == "balance" and fallback:
            if not health.get(primary, False) and health.get(fallback, False):
                rule["strategy"] = "force"
                rule["primary_interface"] = fallback
                changes.append({"app": rule["app_name"], "from": primary, "to": fallback})
            elif health.get(primary, False) and not health.get(fallback, False):
                rule["strategy"] = "force"
                changes.append({"app": rule["app_name"], "from": fallback, "to": primary})
    return resolved, changes


def build_nft_script(
    rules: list[dict[str, Any]], apps: list[dict[str, Any]], interfaces: list[dict[str, Any]]
) -> str:
    routed = route_capable_interfaces(interfaces)[:2]
    if len(routed) < 2:
        raise NetworkError("Deux interfaces routables sont nécessaires")
    names = [item["name"] for item in routed]
    ips = _app_ips(apps)
    lines = [
        "table inet dualroute {",
        "  chain prerouting {",
        "    type filter hook prerouting priority mangle; policy accept;",
        "    ct mark != 0 meta mark set ct mark return",
        f'    iifname "{names[0]}" ct state new meta mark set {MARK_ETH0} ct mark set meta mark return',
        f'    iifname "{names[1]}" ct state new meta mark set {MARK_ETH1} ct mark set meta mark return',
        f"    ip daddr {PRIVATE_NETWORKS} return",
    ]
    for rule in rules:
        if not rule.get("enabled") or rule.get("direction") not in ("out", "both"):
            continue
        app_ips = ips.get(rule["app_id"], [])
        if not app_ips:
            continue
        primary, fallback = _target_marks(rule, names)
        selector = "{ " + ", ".join(app_ips) + " }"
        dscp = f"ip dscp set cs{int(rule.get('qos', 3))}"
        if rule["strategy"] == "balance" and fallback:
            threshold = int(rule.get("weight_primary", 50))
            lines.append(
                f"    ip saddr {selector} ct state new numgen random mod 100 < {threshold} "
                f"{dscp} meta mark set {primary} ct mark set meta mark return"
            )
            lines.append(
                f"    ip saddr {selector} ct state new {dscp} meta mark set {fallback} ct mark set meta mark return"
            )
        else:
            lines.append(
                f"    ip saddr {selector} ct state new {dscp} meta mark set {primary} ct mark set meta mark return"
            )
    lines.extend(["  }", "  chain output {", "    type route hook output priority mangle; policy accept;", "    ct mark != 0 meta mark set ct mark return"])
    for rule in rules:
        if not rule.get("enabled") or rule.get("app_id") != "service:smb":
            continue
        primary, _ = _target_marks(rule, names)
        dscp = f"ip dscp set cs{int(rule.get('qos', 3))}"
        lines.append(f"    tcp dport 445 ip daddr != {PRIVATE_NETWORKS} {dscp} meta mark set {primary} ct mark set meta mark")
    lines.extend(["  }", "}"])
    return "\n".join(lines) + "\n"


def build_apply_plan(
    rules: list[dict[str, Any]], apps: list[dict[str, Any]], interfaces: list[dict[str, Any]]
) -> list[Command]:
    routed = route_capable_interfaces(interfaces)[:2]
    if len(routed) < 2:
        missing = [name.upper() for name in ("eth0", "eth1") if name not in {item["name"].lower() for item in routed}]
        raise NetworkError(f"Double routage Internet indisponible : {', '.join(missing)} sans adresse, passerelle ou lien actif. Une carte sans passerelle reste utilisable sur le réseau local.")
    commands: list[Command] = [
        Command(["nft", "delete", "table", "inet", "dualroute"], ignore_failure=True),
        Command(["nft", "-f", "-"], stdin=build_nft_script(rules, apps, interfaces)),
    ]
    for index, (item, table, mark) in enumerate(
        ((routed[0], TABLE_ETH0, MARK_ETH0), (routed[1], TABLE_ETH1, MARK_ETH1))
    ):
        source = item["address"]
        gateway = item["gateway"]
        device = item["name"]
        network = item["network"]
        commands.extend(
            [
                Command(["ip", "route", "replace", "table", str(table), network, "dev", device, "src", source]),
                Command(["ip", "route", "replace", "table", str(table), "default", "via", gateway, "dev", device, "src", source]),
                Command(["ip", "rule", "del", "priority", str(110 + index)], ignore_failure=True),
                Command(
                    [
                        "ip", "rule", "add", "priority", str(110 + index), "fwmark", f"{mark}/0xff",
                        "lookup", str(table),
                    ]
                ),
            ]
        )
    commands.extend(
        [
            Command(["sysctl", "-w", "net.ipv4.conf.all.rp_filter=2"]),
            Command(["sysctl", "-w", "net.ipv4.conf.default.rp_filter=2"]),
        ]
    )
    return commands


def execute_plan(commands: list[Command]) -> list[dict[str, Any]]:
    if platform.system() != "Linux":
        raise NetworkError("L'application des règles réseau est disponible uniquement sous Linux")
    required = {command.argv[0] for command in commands}
    missing = sorted(binary for binary in required if shutil.which(binary) is None)
    if missing:
        raise NetworkError("Commandes système absentes : " + ", ".join(missing))
    results = []
    with EXECUTION_LOCK:
        for command in commands:
            result = subprocess.run(
                command.argv,
                input=command.stdin,
                text=True,
                capture_output=True,
                timeout=15,
            )
            item = {
                "command": command.display(),
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
            results.append(item)
            if result.returncode and not command.ignore_failure:
                raise NetworkError(f"Échec de {command.display()} : {result.stderr.strip()}")
    return results


def test_gateway(interface: str, gateway: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", interface):
        raise NetworkError("Nom d'interface invalide")
    if not gateway:
        return {"ok": True, "latency_ms": None, "message": "Réseau local uniquement : aucune passerelle à tester. La connexion réseau n’est pas testée."}
    try:
        ipaddress.ip_address(gateway)
    except ValueError as exc:
        raise NetworkError("Passerelle invalide") from exc
    if platform.system() != "Linux" or shutil.which("ping") is None:
        return {"ok": False, "latency_ms": None, "message": "Test disponible dans le conteneur Linux"}
    result = subprocess.run(
        ["ping", "-I", interface, "-c", "2", "-W", "2", gateway],
        capture_output=True,
        text=True,
        timeout=6,
    )
    match = re.search(r"= [\d.]+/([\d.]+)/", result.stdout)
    return {
        "ok": result.returncode == 0,
        "latency_ms": float(match.group(1)) if match else None,
        "message": "Passerelle joignable" if result.returncode == 0 else (result.stderr.strip() or "Aucune réponse"),
    }


def build_interface_plan(config: dict[str, Any]) -> list[Command]:
    interface = config["interface"]
    if interface not in {"eth0", "eth1"}:
        raise NetworkError("Seules ETH0 et ETH1 peuvent être reconfigurées")
    address = ipaddress.ip_address(config["address"])
    gateway = ipaddress.ip_address(config["gateway"]) if config.get("gateway") else None
    network = ipaddress.ip_network(f"{address}/{config['prefix']}", strict=False)
    if address.version != 4 or (gateway and gateway.version != 4):
        raise NetworkError("La configuration des cartes utilise IPv4")
    if gateway and gateway not in network:
        raise NetworkError("La passerelle doit appartenir au même sous-réseau que l'adresse")
    return [
        Command(["ip", "link", "set", "dev", interface, "up"]),
        Command(["ip", "link", "set", "dev", interface, "mtu", str(config.get("mtu", 1500))]),
        Command(["ip", "-4", "address", "flush", "dev", interface, "scope", "global"]),
        Command(["ip", "address", "add", f"{address}/{config['prefix']}", "dev", interface]),
        Command(["ip", "route", "del", "default", "dev", interface], ignore_failure=True),
    ]
