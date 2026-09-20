"""Configuration loading/persistence with validation.

config/config.yaml holds credentials + settings (gitignored).
config/config.example.yaml is the committed template.
All settings passed via the UI are validated/coerced before being applied,
and writes are atomic (tmp + replace).
"""
import os
import re
import threading
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RUNS_DIR = DATA_DIR / "runs"
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
UI_DIR = BASE_DIR / "ui"

# Container deployments set configuration via env vars (PUS_<KEY>) -
# they take precedence over config.yaml. See docker-compose.yml.
ENV_PREFIX = "PUS_"

DEFAULTS = {
    "portainer_url": "",
    "portainer_api_key": "",
    "portainer_endpoint_id": None,   # None = auto-detect via hostname
    "update_interval_hours": 168,    # weekly - only used in "interval" mode
    "update_schedule_mode": "interval",  # "interval" | "daily" | "weekly"
    "update_schedule_time": "03:30",     # HH:MM, server-local time (TZ env)
    "update_schedule_day": 0,            # 0=Monday .. 6=Sunday (weekly mode)
    "tls_verify": False,             # True = verify Portainer TLS cert
    "max_parallel_deploys": 3,
    "deploy_wait_time": 300,         # seconds to wait for containers to become ready
    "keep_backups": 5,
    "backup_dir": str(DATA_DIR / "backups"),
    "portainer_compose_dir": "",     # host path to Portainer's own docker-compose.yml
    "check_cache_minutes": 30,       # docker hub result cache TTL
    "listen_host": "127.0.0.1",      # safe default: localhost only
    "listen_port": 8090,
    "auth_token": "",                # if set: mutating API calls need Bearer token
    "notify_webhook": "",            # optional POST target for failure notifications
    "self_stack_name": "",           # compose project name of THIS app when deployed
                                     # as a Portainer stack (enables self-update-last)
    "include_portainer": False,      # true = also update Portainer itself
                                     # (redeploy its stack via the API, LAST step)
    "repairs": {
        "enabled": True,
        # optional user-defined repair rules (see config.example.yaml);
        # the built-in stale host-gateway repair needs no config
        "rules": [],
    },
}

# validation table: key -> (type, min, max) - value ranges for ints
INT_RANGES = {
    "update_interval_hours": (1, 8760),
    "update_schedule_day": (0, 6),
    "max_parallel_deploys": (1, 10),
    "deploy_wait_time": (30, 3600),
    "keep_backups": (1, 100),
    "check_cache_minutes": (5, 720),
    "listen_port": (1, 65535),
    "portainer_endpoint_id": (None, None),
}

STR_ENUMS = {
    "update_schedule_mode": ("interval", "daily", "weekly"),
}


class ConfigError(ValueError):
    pass


def validate(key: str, value):
    """Coerce + validate one setting. Raises ConfigError on garbage."""
    if key in STR_ENUMS:
        v = str(value or "").strip().lower()
        if v not in STR_ENUMS[key]:
            raise ConfigError(f"{key} must be one of: {', '.join(STR_ENUMS[key])}")
        return v
    if key == "update_schedule_time":
        v = str(value or "").strip()
        if not re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", v):
            raise ConfigError("update_schedule_time must be HH:MM (24h), e.g. 03:30")
        return v
    if key in INT_RANGES:
        try:
            v = int(value)
        except (TypeError, ValueError):
            raise ConfigError(f"{key} must be an integer (got {value!r})")
        lo, hi = INT_RANGES[key]
        if lo is not None and v < lo:
            raise ConfigError(f"{key} must be >= {lo}")
        if hi is not None and v > hi:
            raise ConfigError(f"{key} must be <= {hi}")
        return v
    if key == "tls_verify":
        if not isinstance(value, bool):
            raise ConfigError("tls_verify must be true/false")
        return value
    if key in ("portainer_url", "notify_webhook"):
        v = str(value or "").strip()
        if v and not re.match(r"^https?://", v):
            raise ConfigError(f"{key} must start with http:// or https://")
        return v
    if key in ("portainer_api_key", "portainer_compose_dir", "auth_token",
               "self_stack_name"):
        return str(value or "").strip()
    if key == "include_portainer":
        if not isinstance(value, bool):
            raise ConfigError("include_portainer must be true/false")
        return value
    raise ConfigError(f"unknown setting: {key}")


_lock = threading.Lock()
_cfg = None


def load(force: bool = False) -> dict:
    global _cfg
    with _lock:
        if _cfg is not None and not force:
            return _cfg
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        cfg = dict(DEFAULTS)
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, encoding="utf-8") as f:
                user = yaml.safe_load(f) or {}
            cfg.update({k: v for k, v in user.items()
                        if k not in ("repairs", "repairs_enabled")})
            cfg["repairs"] = {**DEFAULTS["repairs"], **(user.get("repairs") or {})}
        # env overrides (container deployments): PUS_PORTAINER_URL, PUS_LISTEN_HOST, ...
        for key in list(cfg.keys()):
            if key == "repairs":
                continue
            env_val = os.environ.get(ENV_PREFIX + key.upper())
            if env_val is not None and env_val != "":
                try:
                    cfg[key] = yaml.safe_load(env_val)  # coerces true/false/ints/null
                except yaml.YAMLError:
                    cfg[key] = env_val
        # repairs_enabled: flat UI/API alias for cfg["repairs"]["enabled"]
        # (the nested dict is config.yaml-only; the UI toggles the flat key)
        env_rep = os.environ.get(ENV_PREFIX + "REPAIRS_ENABLED")
        if env_rep not in (None, ""):
            cfg["repairs"]["enabled"] = bool(env_rep)
        elif "repairs_enabled" in user_vars(CONFIG_FILE):
            cfg["repairs"]["enabled"] = bool(user_vars(CONFIG_FILE)["repairs_enabled"])
        # expose the flat alias so /api/settings GET + UI checkbox stay in sync
        cfg["repairs_enabled"] = bool(cfg["repairs"].get("enabled", True))
        _cfg = cfg
        return cfg


def user_vars(path: Path) -> dict:
    """Raw user-supplied keys from config.yaml (no defaults merged)."""
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def save() -> None:
    """Atomic write: tmp file + replace."""
    with _lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(_cfg, f, sort_keys=False, allow_unicode=True)
        tmp.replace(CONFIG_FILE)


def get(key: str, default=None):
    return load().get(key, default)


def set(key: str, value) -> None:
    """Validate then set (does NOT save; call save() explicitly)."""
    load()
    if key == "repairs_enabled":
        if not isinstance(value, bool):
            raise ConfigError("repairs_enabled must be true/false")
        with _lock:
            _cfg["repairs_enabled"] = value
            _cfg["repairs"] = {**_cfg.get("repairs", {}), "enabled": value}
        return
    validated = validate(key, value)
    with _lock:
        _cfg[key] = validated