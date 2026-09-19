import os
import tomllib
import re
from dataclasses import dataclass

from station.errors import ExternalError
from station.prototypes.boundary import ext_bool, ext_dict, ext_list, ext_require, ext_str, parse_env_float, parse_env_int

def load_toml_file(path, label):
    if not os.path.exists(path):
        raise ExternalError("%s not found: %s" % (label, path))
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return ext_dict(label, data)

def load_optional_toml(path, label="config file"):
    if not path:
        return {}
    ext_require(label, path, (str,))
    resolved = path if os.path.isabs(path) else os.path.abspath(path)
    return load_toml_file(resolved, label)

def merge_nested(base, overlay):
    out = dict(base)
    for key, value in overlay.items():
        existing = out.get(key)
        if type(value) is dict and type(existing) is dict:
            out[key] = merge_nested(existing, value)
        else:
            out[key] = value
    return out

@dataclass
class ServerConfig:
    request_dedupe_ttl_seconds: int
    dedupe_cleanup_interval_seconds: int
    inbound_workers: int = 4
    inbound_queue: int = 16384
    inbound_batch: int = 8
    inbound_at_least_once: bool = False
    outbound_concurrency: int = 0
    listen_backlog: int = 4096
    isolate_after: int = 3
    outbound_retry_attempts: int = 3
    outbound_retry_base_seconds: float = 0.5
    outbound_circuit_failures: int = 5
    outbound_circuit_cooldown_seconds: float = 1.0

@dataclass
class DatabaseConfig:
    backend: str
    path: str
    dsn: str | None = None
    wipe_on_restart: bool = False

@dataclass
class PrototypeConfig:
    id: int | None
    token: str
    kind: str = "station"
    config_file: str | None = None
    secret_file: str | None = None
    ava: bool = False
    reply_to: bool = False

@dataclass
class RecorderConfig:
    dir_path: str | None = "logs"
    level: str = "INFO"
    max_bytes: int = 10485760
    backup_count: int = 5
    format: str = "%(asctime)s - [%(request_id)s] - %(name)s - %(levelname)s - %(message)s"
    smoothing_window: int = 100

@dataclass
class LocalConductorConfig:
    private_key_path: str = ""
    public_key_path: str = ""
    address: str = ""

class Config:
    def __init__(self, config_file=None, secret_file=None):
        self.data = {}
        self.config_file_path: str | None = None
        self.secret_file_path: str | None = None

        explicit_config_path = None
        if config_file:
            explicit_config_path = config_file
        else:
            env_config_path = os.environ.get("STATION_CONFIG_FILE")
            if env_config_path:
                explicit_config_path = env_config_path

        if explicit_config_path:
            self.data.update(load_toml_file(explicit_config_path, "Station config file"))
            self.config_file_path = explicit_config_path

        secret_from_arg = (secret_file or "").strip() if secret_file else ""
        secret_from_env = (os.environ.get("STATION_SECRET_FILE") or "").strip()
        secret_from_data = self.data.get("STATION_SECRET_FILE")
        if secret_from_data is not None:
            secret_from_data = ext_str("STATION_SECRET_FILE", secret_from_data)
        else:
            secret_from_data = ""
        explicit_secret_path = secret_from_arg or secret_from_env or secret_from_data or None
        if explicit_secret_path:
            self.data.update(load_toml_file(explicit_secret_path, "Station secret file"))
            self.secret_file_path = explicit_secret_path

        self.env = self._get_str("ENV", "development")
        self.consul_url = self._get_str("CONSUL_URL", "")
        self.fs_root = self._get_str("FS_ROOT", "").strip()
        self.host = self._get_str("SERVER_HOST", None, allow_none=True)
        self.port = self._get_int("SERVER_PORT", None, allow_none=True)
        self.server = self._load_server_config()
        self.admin_token = self._get_str("STATION_ADMIN_TOKEN", "").strip()
        self.telegram_api_url = self._get_str("TELEGRAM_API_URL", "").strip().rstrip("/")
        self.telegram_webhook_mode = self._get_bool("TELEGRAM_WEBHOOK_MODE", False)
        self.telegram_webhook_secret = self._get_str("TELEGRAM_WEBHOOK_SECRET", "").strip()
        self.discord_api_url = self._get_str("DISCORD_API_URL", "").strip().rstrip("/")
        self.whatsapp_cloud_api_url = self._get_str("WHATSAPP_CLOUD_API_URL", "").strip().rstrip("/")
        self.qq_api_url = self._get_str("QQ_API_URL", "").strip().rstrip("/")
        self.qq_token_url = self._get_str("QQ_TOKEN_URL", "").strip()
        self.database = self._load_database_config()
        self.hosted_prototypes = self._load_prototype_configs()
        self.recorder = self._load_recorder_config()
        self.local_conductor = self._load_local_conductor_config()

    @property
    def prototype(self) -> PrototypeConfig:
        return self.hosted_prototypes[0]

    def _resolve_repo_path(self, path: str):
        path = path.strip()
        if not path:
            return ""
        if os.path.isabs(path):
            return path
        if self.config_file_path:
            parts = os.path.abspath(self.config_file_path).split(os.sep)
            if "config" in parts:
                idx = parts.index("config")
                root = os.sep.join(parts[:idx]) or os.sep
                return os.path.abspath(os.path.join(root, path))
        return os.path.abspath(path)

    def _lookup(self, key, default_value=None):
        if key in self.data:
            return self.data[key], "config"
        env_value = os.getenv(key)
        if env_value is not None and env_value != "":
            return env_value, "env"
        return default_value, "default"

    def _parse_env_int(self, key, value, *, allow_none=False):
        return parse_env_int(key, value, allow_none=allow_none)

    def _parse_env_float(self, key, value, *, allow_none=False):
        return parse_env_float(key, value, allow_none=allow_none)

    def _parse_env_bool(self, key, value):
        stripped = value.strip().lower()
        if stripped in ("true", "1", "yes", "y", "on"):
            return True
        if stripped in ("false", "0", "no", "n", "off"):
            return False
        raise ExternalError("%s must be bool, got str" % key)

    def _get_str(self, key, default_value=None, *, allow_none=False):
        value, _source = self._lookup(key, default_value)
        return ext_require(key, value, (str,), allow_none=allow_none)

    def _get_int(self, key, default_value=None, *, allow_none=False):
        value, source = self._lookup(key, default_value)
        if source == "env":
            return self._parse_env_int(key, value, allow_none=allow_none)
        return ext_require(key, value, (int,), allow_none=allow_none)

    def _get_float(self, key, default_value=None, *, allow_none=False):
        value, source = self._lookup(key, default_value)
        if source == "env":
            return self._parse_env_float(key, value, allow_none=allow_none)
        return ext_require(key, value, (float,), allow_none=allow_none)

    def _get_bool(self, key, default_value=False):
        value, source = self._lookup(key, default_value)
        if source == "env":
            return self._parse_env_bool(key, value)
        return ext_require(key, value, (bool,))

    def _load_server_config(self):
        request_dedupe_ttl_seconds = self._get_int("REQUEST_DEDUPE_TTL_SECONDS", 3600)
        if request_dedupe_ttl_seconds <= 0:
            raise ExternalError("REQUEST_DEDUPE_TTL_SECONDS must be > 0")

        dedupe_cleanup_interval_seconds = self._get_int("DEDUPE_CLEANUP_INTERVAL_SECONDS", 600)
        if dedupe_cleanup_interval_seconds <= 0:
            raise ExternalError("DEDUPE_CLEANUP_INTERVAL_SECONDS must be > 0")

        inbound_workers = self._get_int("STATION_INBOUND_WORKERS", 4)
        if inbound_workers <= 0:
            raise ExternalError("STATION_INBOUND_WORKERS must be > 0")

        inbound_queue = self._get_int("STATION_INBOUND_QUEUE", 16384)
        if inbound_queue <= 0:
            raise ExternalError("STATION_INBOUND_QUEUE must be > 0")

        inbound_batch = self._get_int("STATION_INBOUND_BATCH", 8)
        if inbound_batch <= 0:
            raise ExternalError("STATION_INBOUND_BATCH must be > 0")

        outbound_concurrency = self._get_int("STATION_OUTBOUND_CONCURRENCY", 0)
        if outbound_concurrency < 0:
            raise ExternalError("STATION_OUTBOUND_CONCURRENCY must be >= 0")

        listen_backlog = self._get_int("STATION_LISTEN_BACKLOG", 4096)
        if listen_backlog <= 0:
            raise ExternalError("STATION_LISTEN_BACKLOG must be > 0")

        inbound_at_least_once = self._get_bool("STATION_INBOUND_AT_LEAST_ONCE", False)

        isolate_after = self._get_int("STATION_INSTANCE_ISOLATE_AFTER", 3)
        if isolate_after <= 0:
            raise ExternalError("STATION_INSTANCE_ISOLATE_AFTER must be > 0")

        outbound_retry_attempts = self._get_int("STATION_OUTBOUND_RETRY_ATTEMPTS", 3)
        if outbound_retry_attempts < 0:
            raise ExternalError("STATION_OUTBOUND_RETRY_ATTEMPTS must be >= 0")

        outbound_retry_base_seconds = self._get_float("STATION_OUTBOUND_RETRY_BASE_SECONDS", 0.5)
        if outbound_retry_base_seconds <= 0:
            raise ExternalError("STATION_OUTBOUND_RETRY_BASE_SECONDS must be > 0")

        outbound_circuit_failures = self._get_int("STATION_OUTBOUND_CIRCUIT_FAILURES", 5)
        if outbound_circuit_failures <= 0:
            raise ExternalError("STATION_OUTBOUND_CIRCUIT_FAILURES must be > 0")

        outbound_circuit_cooldown_seconds = self._get_float(
            "STATION_OUTBOUND_CIRCUIT_COOLDOWN_SECONDS",
            1.0,
        )
        if outbound_circuit_cooldown_seconds <= 0:
            raise ExternalError("STATION_OUTBOUND_CIRCUIT_COOLDOWN_SECONDS must be > 0")

        return ServerConfig(
            request_dedupe_ttl_seconds=request_dedupe_ttl_seconds,
            dedupe_cleanup_interval_seconds=dedupe_cleanup_interval_seconds,
            inbound_workers=inbound_workers,
            inbound_queue=inbound_queue,
            inbound_batch=inbound_batch,
            inbound_at_least_once=inbound_at_least_once,
            outbound_concurrency=outbound_concurrency,
            listen_backlog=listen_backlog,
            isolate_after=isolate_after,
            outbound_retry_attempts=outbound_retry_attempts,
            outbound_retry_base_seconds=outbound_retry_base_seconds,
            outbound_circuit_failures=outbound_circuit_failures,
            outbound_circuit_cooldown_seconds=outbound_circuit_cooldown_seconds,
        )

    def _load_database_config(self):
        backend = self._get_str("DB_BACKEND", "sqlite").strip().lower()
        if backend not in ("sqlite", "postgres"):
            raise ExternalError("DB_BACKEND must be either 'sqlite' or 'postgres'")
        return DatabaseConfig(
            backend=backend,
            path=self._get_str("DB_PATH", "prototype.db"),
            dsn=self._get_str("DB_DSN", None, allow_none=True),
            wipe_on_restart=self._get_bool("WIPE_ON_RESTART", False),
        )

    def _field_or_default(self, raw, key, default):
        if key in raw:
            return raw[key]
        return default

    def _parse_prototype_entry(self, raw, *, label: str) -> PrototypeConfig:
        raw = ext_dict(label, raw)
        p_id = ext_require(
            f"{label}.id",
            self._field_or_default(raw, "id", None),
            (int,),
            allow_none=True,
        )
        token = ext_str(
            f"{label}.token",
            self._field_or_default(raw, "token", ""),
        )
        kind = ext_str(
            f"{label}.kind",
            self._field_or_default(raw, "kind", "station"),
        ) or "station"
        config_file = ext_require(
            f"{label}.config_file",
            self._field_or_default(raw, "config_file", None),
            (str,),
            allow_none=True,
        )
        if config_file is not None:
            config_file = config_file.strip() or None
        secret_file = ext_require(
            f"{label}.secret_file",
            self._field_or_default(raw, "secret_file", None),
            (str,),
            allow_none=True,
        )
        if secret_file is not None:
            secret_file = secret_file.strip() or None
        ava = ext_bool(
            f"{label}.ava",
            self._field_or_default(raw, "ava", False),
        )
        reply_to = ext_bool(
            f"{label}.reply_to",
            self._field_or_default(raw, "reply_to", False),
        )
        return PrototypeConfig(
            id=p_id,
            token=token,
            kind=kind,
            config_file=config_file,
            secret_file=secret_file,
            ava=ava,
            reply_to=reply_to,
        )

    def _load_prototype_configs(self) -> list[PrototypeConfig]:
        raw_list = self.data.get("prototypes")
        if raw_list is None:
            raise ExternalError("At least one [[prototypes]] entry is required")
        raw_list = ext_list("prototypes", raw_list)
        if not raw_list:
            raise ExternalError("At least one [[prototypes]] entry is required")

        hosted: list[PrototypeConfig] = []
        for i, entry in enumerate(raw_list):
            hosted.append(self._parse_prototype_entry(entry, label=f"prototypes[{i}]"))

        seen_ids: set[int] = set()
        seen_tokens: set[str] = set()
        for cfg in hosted:
            if cfg.id is None:
                raise ExternalError("Each prototype tenant requires an id")
            if not cfg.token.strip():
                raise ExternalError("prototype_id=%s requires a non-empty token" % cfg.id)
            pid = cfg.id
            token = cfg.token.strip()
            if pid in seen_ids:
                raise ExternalError("Duplicate prototype id: %s" % pid)
            if token in seen_tokens:
                raise ExternalError("Duplicate prototype token for prototype_id=%s" % pid)
            seen_ids.add(pid)
            seen_tokens.add(token)
            cfg.kind = cfg.kind.strip()
            if not cfg.kind:
                raise ExternalError("prototype_id=%s requires a non-empty kind" % cfg.id)

        return hosted

    def _load_recorder_config(self) -> RecorderConfig:
        dir_path = self._get_str("LOG_DIR_PATH", "logs", allow_none=True)
        if dir_path is not None:
            dir_path = dir_path.strip() or None

        level = self._get_str("LOG_LEVEL", "INFO").strip()
        log_format = self._get_str(
            "LOG_FORMAT",
            "%(asctime)s - [%(request_id)s] - %(name)s - %(levelname)s - %(message)s",
        )

        max_bytes = self._get_int("LOG_MAX_BYTES", 10485760)
        if max_bytes <= 0:
            raise ExternalError("LOG_MAX_BYTES must be > 0")

        backup_count = self._get_int("LOG_BACKUP_COUNT", 5)
        if backup_count < 0:
            raise ExternalError("LOG_BACKUP_COUNT must be >= 0")

        smoothing_window = self._get_int("SMOOTHING_WINDOW", 100)
        if smoothing_window <= 0:
            raise ExternalError("SMOOTHING_WINDOW must be > 0")

        return RecorderConfig(
            dir_path=dir_path,
            level=level,
            format=log_format,
            max_bytes=max_bytes,
            backup_count=backup_count,
            smoothing_window=smoothing_window,
        )

    def _load_local_conductor_config(self):
        private = ""
        if "LOCAL_CONDUCTOR_PRIVATE_KEY_PATH" in self.data:
            private = ext_str(
                "LOCAL_CONDUCTOR_PRIVATE_KEY_PATH",
                self.data["LOCAL_CONDUCTOR_PRIVATE_KEY_PATH"],
            )
        public = ""
        if "LOCAL_CONDUCTOR_PUBLIC_KEY_PATH" in self.data:
            public = ext_str(
                "LOCAL_CONDUCTOR_PUBLIC_KEY_PATH",
                self.data["LOCAL_CONDUCTOR_PUBLIC_KEY_PATH"],
            )
        address = ""
        if "LOCAL_CONDUCTOR_ADDRESS" in self.data:
            address = ext_str(
                "LOCAL_CONDUCTOR_ADDRESS",
                self.data["LOCAL_CONDUCTOR_ADDRESS"],
            )
        private = self._resolve_repo_path(private)
        public = self._resolve_repo_path(public)
        if private and not public:
            public = private + ".pub"
        return LocalConductorConfig(
            private_key_path=private,
            public_key_path=public,
            address=address,
        )

    def _parse_access_point_host_port(self, access_point):
        access_point = ext_str("access_point", access_point, strip=False)
        if access_point.startswith("http://"):
            access_point = access_point[7:]
        elif access_point.startswith("https://"):
            access_point = access_point[8:]

        match = re.match(r"^(?:\[(?P<ipv6>[^\]]+)\]|(?P<host>.*)):(?P<port>\d+)$", access_point)
        if match:
            host = match.group("ipv6") or match.group("host")
            port = int(match.group("port"))
            return host, port
        return None, None
