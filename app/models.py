from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


Direction = Literal["in", "out", "both"]
Strategy = Literal["force", "prefer", "balance", "qos"]


class RuleInput(BaseModel):
    app_id: str = Field(min_length=1, max_length=128)
    app_name: str = Field(min_length=1, max_length=128)
    direction: Direction = "both"
    strategy: Strategy = "prefer"
    primary_interface: str = Field(default="eth0", pattern=r"^[a-zA-Z0-9_.:-]+$")
    fallback_interface: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_.:-]+$")
    qos: int = Field(default=3, ge=1, le=5)
    enabled: bool = True
    download_limit_mbps: int | None = Field(default=None, ge=1, le=100000)
    upload_limit_mbps: int | None = Field(default=None, ge=1, le=100000)
    ports: list[int] = Field(default_factory=list, max_length=64)
    weight_primary: int = Field(default=50, ge=1, le=99)

    @field_validator("ports")
    @classmethod
    def valid_ports(cls, value: list[int]) -> list[int]:
        if any(port < 1 or port > 65535 for port in value):
            raise ValueError("Chaque port doit être compris entre 1 et 65535")
        return sorted(set(value))


class Rule(RuleInput):
    id: int
    updated_at: str


class NetworkConfigInput(BaseModel):
    interface: str = Field(pattern=r"^[a-zA-Z0-9_.:-]+$")
    address: str
    prefix: int = Field(default=24, ge=1, le=32)
    gateway: str
    dns: list[str] = Field(default_factory=list, max_length=4)
    mtu: int = Field(default=1500, ge=576, le=9216)


class ApplyRequest(BaseModel):
    confirm: str = ""
    dry_run: bool = True


class NetworkConfigureRequest(BaseModel):
    config: NetworkConfigInput
    confirm: str = ""
    dry_run: bool = True


class SettingInput(BaseModel):
    enforcement_enabled: bool
    sample_interval_seconds: int = Field(default=2, ge=1, le=60)
    retention_days: int = Field(default=30, ge=1, le=365)
    display_rate_unit: Literal["mbps", "MBps"] = "mbps"
    dashboard_managed_only: bool = True
