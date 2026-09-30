"""Configuration: loading, parsing and validation.

Everything is validated up front. A config that fails here must never reach a running
process, because a crash on restart takes the whole cluster offline (this happened to
Olla on 2026-09-30 when check_timeout >= check_interval).
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)\s*$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(value: Any) -> float:
    """Parse "500ms", "3s", "15m", "1h" (or a bare number of seconds) into seconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        m = _DURATION_RE.match(value)
        if m:
            return float(m.group(1)) * _UNIT_SECONDS[m.group(2)]
    raise ValueError(f"invalid duration {value!r}; use e.g. 500ms, 3s, 15m, 1h")


Duration = Annotated[float, BeforeValidator(parse_duration)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CorsConfig(_Strict):
    enabled: bool = True
    allowed_origins: list[str] = ["*"]
    allowed_methods: list[str] = ["GET", "POST", "OPTIONS"]
    allowed_headers: list[str] = ["*"]
    allow_credentials: bool = False
    max_age: int = 3600


class RequestLimits(_Strict):
    max_body_size: int = 100 * 1024 * 1024
    max_header_size: int = 1024 * 1024


class PerEndpointLimits(_Strict):
    default_requests_per_minute: int = 200


class RateLimits(_Strict):
    global_requests_per_minute: int = 1000
    per_ip_requests_per_minute: int = 100
    burst_size: int = 50
    # Accepted so an Olla server block can be copied as is. Olla v0.0.28 ignores both:
    # /internal/health is never rate limited and per_endpoint has no effect.
    health_requests_per_minute: int = 1000
    per_endpoint: PerEndpointLimits = PerEndpointLimits()
    cleanup_interval: Duration = 300.0
    trust_proxy_headers: bool = False
    trusted_proxy_cidrs: list[str] = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]

    @model_validator(mode="after")
    def _check_cidrs(self) -> RateLimits:
        for c in self.trusted_proxy_cidrs:
            ipaddress.ip_network(c, strict=False)
        return self


class ServerConfig(_Strict):
    host: str = "0.0.0.0"
    port: int = Field(40114, ge=1, le=65535)
    read_header_timeout: Duration = 10.0
    shutdown_timeout: Duration = 10.0
    request_logging: bool = True
    cors: CorsConfig = CorsConfig()
    request_limits: RequestLimits = RequestLimits()
    rate_limits: RateLimits = RateLimits()


class ProxyConfig(_Strict):
    connect_timeout: Duration = 10.0
    response_header_timeout: Duration = 120.0
    response_timeout: Duration = 900.0
    read_timeout: Duration = 600.0
    # Streaming responses that produce no bytes for this long are aborted.
    stall_timeout: Duration = 60.0
    # Attempts for a request whose host can't be reached (connection error, or 502/503/504
    # before any response body). Default: one per host, as in Olla.
    max_attempts: int | None = Field(None, ge=1, le=10)


class HealthConfig(_Strict):
    interval: Duration = 5.0
    timeout: Duration = 3.0
    path: str = "/"
    failure_threshold: int = Field(3, ge=1)

    @model_validator(mode="after")
    def _timeout_below_interval(self) -> HealthConfig:
        if self.timeout >= self.interval:
            raise ValueError(
                f"health.timeout ({self.timeout}s) must be less than health.interval ({self.interval}s)"
            )
        return self


class DiscoveryConfig(_Strict):
    ps_interval: Duration = 1.0
    tags_interval: Duration = 300.0
    timeout: Duration = 5.0


class HostConfig(_Strict):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    url: str = Field(pattern=r"^https?://")
    vram_mb: int = Field(gt=0)
    # Headroom kept free for CUDA context and fragmentation.
    reserve_mb: int = Field(512, ge=0)
    # Must match OLLAMA_NUM_PARALLEL on the host.
    slots_per_model: int = Field(2, ge=1)
    # Must match OLLAMA_MAX_LOADED_MODELS on the host.
    max_loaded_models: int = Field(2, ge=1)

    @model_validator(mode="after")
    def _reserve_below_vram(self) -> HostConfig:
        if self.reserve_mb >= self.vram_mb:
            raise ValueError(f"host {self.name}: reserve_mb must be less than vram_mb")
        self.url = self.url.rstrip("/")
        return self

    @property
    def usable_vram_bytes(self) -> int:
        return (self.vram_mb - self.reserve_mb) * 1024 * 1024


class ModelPolicy(_Strict):
    home: list[str] = []
    keep_warm: int = Field(0, ge=0)
    allow_cpu_offload: bool = False


class PriorityClass(_Strict):
    # Client IPs or CIDRs that belong to this class.
    match: list[str] = []
    # Waiters in this class are ordered as if they arrived this much earlier.
    boost: Duration = 0.0
    default: bool = False

    @model_validator(mode="after")
    def _check_match(self) -> PriorityClass:
        for m in self.match:
            ipaddress.ip_network(m, strict=False)
        return self


class QueueConfig(_Strict):
    max_wait: Duration = 120.0
    classes: dict[str, PriorityClass] = {
        "interactive": PriorityClass(match=["127.0.0.1/32", "::1/128"], boost=30.0),
        "batch": PriorityClass(default=True),
    }

    @model_validator(mode="after")
    def _one_default(self) -> QueueConfig:
        defaults = [n for n, c in self.classes.items() if c.default]
        if len(defaults) != 1:
            raise ValueError(f"queue.classes needs exactly one default class, found {defaults or 'none'}")
        return self

    @property
    def default_class(self) -> str:
        return next(n for n, c in self.classes.items() if c.default)


class LoggingConfig(_Strict):
    file: str | None = "/opt/olla/logs/olla.log"
    level: str = Field("info", pattern=r"^(debug|info|warn|error)$")
    max_size_mb: int = Field(1, ge=1)
    max_backups: int = Field(7, ge=0)


class SchedulingConfig(_Strict):
    # Unload the models placement chose before a cold load. When off, Ollama evicts
    # by its own LRU rule instead.
    evict: bool = True
    # Pre-load keep_warm models onto idle hosts.
    keep_warm: bool = True


class Config(_Strict):
    server: ServerConfig = ServerConfig()
    proxy: ProxyConfig = ProxyConfig()
    health: HealthConfig = HealthConfig()
    discovery: DiscoveryConfig = DiscoveryConfig()
    hosts: list[HostConfig] = Field(min_length=1)
    models: dict[str, ModelPolicy] = {}
    queue: QueueConfig = QueueConfig()
    # Set both to false while another balancer shares the hosts (shadow testing), so
    # llm-head never unloads or loads a model the other balancer is using.
    scheduling: SchedulingConfig = SchedulingConfig()
    logging: LoggingConfig = LoggingConfig()
    stats_file: str | None = "/opt/olla/data/llm-head-stats.json"

    @model_validator(mode="after")
    def _cross_checks(self) -> Config:
        names = [h.name for h in self.hosts]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate host names: {sorted(dupes)}")
        for pattern, policy in self.models.items():
            unknown = [h for h in policy.home if h not in names]
            if unknown:
                raise ValueError(f"models[{pattern!r}].home names unknown hosts: {unknown}")
            if policy.keep_warm > len(self.hosts):
                raise ValueError(
                    f"models[{pattern!r}].keep_warm={policy.keep_warm} exceeds host count {len(self.hosts)}"
                )
        return self

    def policy_for(self, model: str) -> ModelPolicy:
        """Return the policy for a normalized model name. Exact keys win over globs;
        among globs, the longest pattern wins."""
        from .names import normalize

        exact = {normalize(k): v for k, v in self.models.items() if not _is_glob(k)}
        if model in exact:
            return exact[model]
        globs = sorted((k for k in self.models if _is_glob(k)), key=len, reverse=True)
        for k in globs:
            if fnmatch.fnmatchcase(model, k.lower()):
                return self.models[k]
        return ModelPolicy()


def _is_glob(s: str) -> bool:
    return any(ch in s for ch in "*?[")


class ConfigError(Exception):
    pass


def load_config(path: str | Path) -> Config:
    try:
        raw = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    try:
        return Config.model_validate(raw)
    except ValueError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
