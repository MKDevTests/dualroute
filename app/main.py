from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .db import Store
from .discovery import container_stats, discover_apps, discover_interfaces
from .metrics import MetricsCollector
from .models import ApplyRequest, NetworkConfigureRequest, NetworkConfigInput, Rule, RuleInput, SettingInput
from .network import (
    NetworkError,
    build_apply_plan,
    build_interface_plan,
    execute_plan,
    resolve_rules_for_health,
    route_capable_interfaces,
    test_gateway,
    validate_distinct_networks,
)


DATA_DIR = Path(os.getenv("DUALROUTE_DATA_DIR", "./data"))
STATIC_DIR = Path(__file__).parent / "static"
store = Store(DATA_DIR / "dualroute.db")
metrics = MetricsCollector(store)


async def routing_reconciler() -> None:
    last_signature: str | None = None
    while True:
        try:
            if not store.get_setting("enforcement_enabled", False):
                last_signature = None
                await asyncio.sleep(10)
                continue
            configured = store.network_configs()
            live_by_name = {item["name"]: item for item in discover_interfaces()}
            for config in configured:
                live = live_by_name.get(config["interface"])
                if live and live.get("address") != config["address"]:
                    await asyncio.to_thread(execute_plan, build_interface_plan(config))
                    store.event(
                        "warning", "network",
                        f"Configuration restaurée sur {config['interface']}",
                        {"address": config["address"], "prefix": config["prefix"]},
                    )
            state = await asyncio.to_thread(current_state)
            routed = route_capable_interfaces(state["interfaces"])[:2]
            checks = await asyncio.gather(
                *(asyncio.to_thread(test_gateway, item["name"], item["gateway"]) for item in routed)
            )
            health = {item["name"]: check["ok"] for item, check in zip(routed, checks)}
            effective_rules, changes = resolve_rules_for_health(state["rules"], health)
            signature = json.dumps(
                {
                    "rules": effective_rules,
                    "apps": [(item["id"], item.get("ips", [])) for item in state["apps"]],
                    "interfaces": [(item["name"], item.get("address"), item.get("gateway")) for item in routed],
                    "health": health,
                },
                sort_keys=True,
            )
            if len(routed) == 2 and signature != last_signature:
                plan = build_apply_plan(effective_rules, state["apps"], state["interfaces"])
                await asyncio.to_thread(execute_plan, plan)
                for change in changes:
                    store.event(
                        "warning", "failover",
                        f"Bascule de {change['app']} : {change['from']} → {change['to']}",
                        change,
                    )
                store.event("info", "routing", "Configuration réseau réconciliée", {"health": health})
                last_signature = signature
        except Exception as exc:
            store.event("error", "reconciler", str(exc))
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(metrics.run())
    reconcile_task = asyncio.create_task(routing_reconciler())
    store.event("info", "system", "DualRoute démarré")
    try:
        yield
    finally:
        metrics.running = False
        task.cancel()
        reconcile_task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        try:
            await reconcile_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="DualRoute", version="0.1.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


def current_state(include_stats: bool = False) -> dict[str, Any]:
    interfaces = discover_interfaces()
    network_configs = store.network_configs()
    config_by_name = {item["interface"]: item for item in network_configs}
    for interface in interfaces:
        config = config_by_name.get(interface["name"])
        if not config:
            continue
        interface["configured"] = config
        if interface.get("address") == config["address"]:
            interface["gateway"] = config["gateway"]
            interface["prefix"] = config["prefix"]
            interface["network"] = str(
                ipaddress.ip_network(f"{config['address']}/{config['prefix']}", strict=False)
            )
    apps = discover_apps()
    return {
        "interfaces": interfaces,
        "apps": apps,
        "app_stats": container_stats(apps) if include_stats else {},
        "rules": store.list_rules(),
        "metrics": metrics.latest,
        "settings": store.settings(),
        "network_configs": network_configs,
        "warnings": validate_distinct_networks(interfaces),
    }


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": app.version, "enforcement": store.get_setting("enforcement_enabled", False)}


@app.get("/api/snapshot")
def snapshot(include_app_stats: bool = False) -> dict[str, Any]:
    return current_state(include_app_stats)


@app.get("/api/interfaces")
def interfaces() -> list[dict[str, Any]]:
    return discover_interfaces()


@app.get("/api/apps")
def apps(include_stats: bool = False) -> dict[str, Any]:
    items = discover_apps()
    return {"items": items, "stats": container_stats(items) if include_stats else {}}


@app.get("/api/rules", response_model=list[Rule])
def list_rules() -> list[dict[str, Any]]:
    return store.list_rules()


@app.post("/api/rules", response_model=Rule, status_code=201)
def create_rule(payload: RuleInput) -> dict[str, Any]:
    rule = store.save_rule(payload.model_dump())
    store.event("info", "rules", f"Règle enregistrée pour {rule['app_name']}", {"rule_id": rule["id"]})
    return rule


@app.put("/api/rules/{rule_id}", response_model=Rule)
def update_rule(rule_id: int, payload: RuleInput) -> dict[str, Any]:
    try:
        rule = store.save_rule(payload.model_dump(), rule_id)
    except KeyError as exc:
        raise HTTPException(404, "Règle introuvable") from exc
    store.event("info", "rules", f"Règle modifiée pour {rule['app_name']}", {"rule_id": rule_id})
    return rule


@app.delete("/api/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int) -> None:
    if not store.delete_rule(rule_id):
        raise HTTPException(404, "Règle introuvable")
    store.event("info", "rules", "Règle supprimée", {"rule_id": rule_id})


@app.get("/api/traffic/history")
def traffic_history(hours: int = Query(default=1, ge=1, le=24 * 31)) -> list[dict[str, Any]]:
    return metrics.history(hours)


@app.get("/api/events")
def events(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
    return store.events(limit)


@app.post("/api/network/test")
def network_test(payload: NetworkConfigInput) -> dict[str, Any]:
    return test_gateway(payload.interface, payload.gateway)


@app.post("/api/network/configure")
def network_configure(request: NetworkConfigureRequest) -> dict[str, Any]:
    payload = request.config
    data = payload.model_dump()
    try:
        primary = next((item for item in discover_interfaces() if item.get("default")), None)
        if primary and payload.interface == primary["name"] and os.getenv("DUALROUTE_ALLOW_PRIMARY_RECONFIGURE") != "1":
            raise HTTPException(
                409,
                "La modification de l'interface principale est bloquée pour préserver l'accès au NAS",
            )
        plan = build_interface_plan(data)
        preview = [command.display() for command in plan]
        if request.dry_run:
            return {"applied": False, "plan": preview}
        expected = f"APPLY {payload.interface}"
        if request.confirm != expected:
            raise HTTPException(409, f"Confirmation requise : {expected}")
        results = execute_plan(plan)
        store.save_network_config(data)
        validation = test_gateway(payload.interface, payload.gateway)
        level = "warning" if validation["ok"] else "error"
        store.event(level, "network", f"Configuration appliquée à {payload.interface}", {**data, "validation": validation})
        return {"applied": True, "plan": preview, "results": results, "validation": validation}
    except NetworkError as exc:
        store.event("error", "network", str(exc), data)
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/routing/apply")
def apply_routing(request: ApplyRequest) -> dict[str, Any]:
    state = current_state()
    try:
        routed = route_capable_interfaces(state["interfaces"])[:2]
        health = {
            item["name"]: test_gateway(item["name"], item["gateway"])["ok"]
            for item in routed
        }
        effective_rules, failovers = resolve_rules_for_health(state["rules"], health)
        plan = build_apply_plan(effective_rules, state["apps"], state["interfaces"])
        preview = [command.display() for command in plan]
        if request.dry_run:
            nft_script = next((command.stdin for command in plan if command.stdin), "")
            return {"applied": False, "plan": preview, "nft_script": nft_script, "warnings": state["warnings"], "health": health, "failovers": failovers}
        if not store.get_setting("enforcement_enabled", False):
            raise HTTPException(409, "Activez d'abord le mode d'application dans les paramètres")
        if request.confirm != "APPLIQUER":
            raise HTTPException(409, "Confirmation requise : APPLIQUER")
        results = execute_plan(plan)
        store.event("warning", "routing", "Règles de routage appliquées", {"count": len(state["rules"])})
        return {"applied": True, "plan": preview, "results": results, "warnings": state["warnings"], "health": health, "failovers": failovers}
    except NetworkError as exc:
        store.event("error", "routing", str(exc))
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/settings")
def update_settings(payload: SettingInput) -> dict[str, Any]:
    for key, value in payload.model_dump().items():
        store.set_setting(key, value)
    store.event(
        "warning" if payload.enforcement_enabled else "info",
        "settings",
        "Mode actif autorisé" if payload.enforcement_enabled else "Mode prévisualisation activé",
    )
    return store.settings()


@app.get("/")
def root() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/{path:path}")
def spa(path: str) -> FileResponse:
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(STATIC_DIR / "index.html")
