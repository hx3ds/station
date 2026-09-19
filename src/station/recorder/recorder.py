import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from aiohttp import web

from station.api.http import external_response, log_caught
from station.errors import ExternalError

from .context import request_id_var

_PROBE_PATHS = frozenset({"/healthz", "/metrics"})
_VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
_VENDOR_LOGGERS = ("aiohttp.access", "aiohttp.server", "httpx", "urllib3", "aioice", "aiortc")
_HOT_PREFIXES = ("/reception/", "/event/")

class RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = request_id_var.get()
        return True

class TZFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone()
        return dt.isoformat(sep=" ", timespec="milliseconds")

def bootstrap_logger(name="station"):
    level_name = (os.getenv("LOG_LEVEL") or "INFO").upper()
    if level_name not in _VALID_LEVELS:
        raise ExternalError("Invalid LOG_LEVEL: %s" % level_name)
    root = logging.getLogger()
    if not root.handlers:
        formatter = TZFormatter(
            "%(asctime)s - [%(request_id)s] - %(name)s - %(levelname)s - %(message)s"
        )
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        handler.addFilter(RequestIdFilter())
        root.addHandler(handler)
    root.setLevel(level_name)
    return logging.getLogger(name)

class Recorder:
    def __init__(self, name, config, port=None):
        self.base_name = name
        self.port = port
        self.name = "%s:%s" % (name, port) if port else name
        self.config = config
        self.path_stats = {}
        self.logger = logging.getLogger(self.name)
        self._setup_logging()

    def _setup_logging(self):
        log_level = self.config.level.upper()
        if log_level not in _VALID_LEVELS:
            raise ExternalError("Invalid log level: %s" % self.config.level)
        formatter = TZFormatter(self.config.format)
        request_id_filter = RequestIdFilter()

        root = logging.getLogger()
        root.setLevel(log_level)
        if root.hasHandlers():
            root.handlers.clear()

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(request_id_filter)
        root.addHandler(console_handler)

        dir_path = self.config.dir_path
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
            file_name = "%s-%s.log" % (self.base_name, self.port) if self.port else "%s.log" % self.base_name
            file_path = os.path.join(dir_path, file_name)
            file_handler = RotatingFileHandler(
                file_path,
                maxBytes=self.config.max_bytes,
                backupCount=self.config.backup_count,
            )
            file_handler.setFormatter(formatter)
            file_handler.addFilter(request_id_filter)
            root.addHandler(file_handler)

        self.logger.setLevel(log_level)
        for name in _VENDOR_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

    def set_log_level(self, level_name):
        level = level_name.upper()
        if level not in _VALID_LEVELS:
            raise ExternalError("Invalid log level: %s" % level_name)
        logging.getLogger().setLevel(level)
        self.logger.setLevel(level)
        self.config.level = level
        self.logger.info("log_level level=%s", level)

    def get_log_level(self):
        return self.config.level

    @property
    def smoothing_window(self):
        return self.config.smoothing_window

    @smoothing_window.setter
    def smoothing_window(self, value):
        self.config.smoothing_window = value

    @web.middleware
    async def tracing_middleware(self, request, handler):
        path = request.path
        if path.startswith(_HOT_PREFIXES):
            request_id = request.headers.get("X-Request-Id") or request.headers.get("X-Request-ID") or ""
            if not request_id:
                return await handler(request)
            token = request_id_var.set(request_id)
            request["request_id"] = request_id
            try:
                response = await handler(request)
                response.headers["X-Request-ID"] = request_id
                return response
            finally:
                request_id_var.reset(token)

        request_id = request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id")
        if not request_id:
            request_id = str(uuid.uuid4())
        token = request_id_var.set(request_id)
        request["request_id"] = request_id
        try:
            response = await handler(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)

    def _log_http(self, request, status, duration_ms, req_bytes, resp_size, remote):
        path = request.path
        if path in _PROBE_PATHS:
            return
        msg = (
            "http method=%s path=%s status=%s duration_ms=%s request_bytes=%s "
            "response_bytes=%s remote=%s"
        )
        args = (request.method, path, status, duration_ms, req_bytes, resp_size, remote)
        if status >= 500:
            if request.get("error_logged"):
                return
            self.logger.error(msg, *args)
            return
        if status == 429:
            self.logger.warning(msg, *args)
            return
        if status >= 400:
            self.logger.info(msg, *args)
            return
        self.logger.debug(msg, *args)

    @web.middleware
    async def middleware(self, request, handler):
        start_time = time.perf_counter()
        path = request.path
        hot = path.startswith(_HOT_PREFIXES)
        n = self.smoothing_window
        if not hot:
            if path not in self.path_stats:
                self.path_stats[path] = {
                    "avg_duration": 0.0,
                    "avg_interval": 0.0,
                    "last_request_time": start_time,
                    "status_codes": {},
                    "total_requests": 0,
                    "current_concurrency": 1,
                    "max_concurrency": 1,
                    "avg_request_size": 0.0,
                    "avg_response_size": 0.0,
                }
            else:
                stats = self.path_stats[path]
                interval = start_time - stats["last_request_time"]
                stats["last_request_time"] = start_time
                if stats["avg_interval"] == 0:
                    stats["avg_interval"] = interval
                else:
                    stats["avg_interval"] = stats["avg_interval"] * (n - 1) / n + interval / n
                stats["current_concurrency"] += 1
                if stats["current_concurrency"] > stats["max_concurrency"]:
                    stats["max_concurrency"] = stats["current_concurrency"]

        response = None
        try:
            response = await handler(request)
            return response
        finally:
            duration = time.perf_counter() - start_time
            status = 500
            resp_size = 0
            if response:
                status = response.status
                if response.content_length is not None:
                    resp_size = response.content_length
            if not hot:
                stats = self.path_stats[path]
                stats["total_requests"] += 1
                stats["current_concurrency"] -= 1
                if stats["avg_duration"] == 0:
                    stats["avg_duration"] = duration
                else:
                    stats["avg_duration"] = stats["avg_duration"] * (n - 1) / n + duration / n
                stats["status_codes"][status] = stats["status_codes"].get(status, 0) + 1
                req_size = request.content_length or 0
                if stats["avg_request_size"] == 0:
                    stats["avg_request_size"] = req_size
                else:
                    stats["avg_request_size"] = stats["avg_request_size"] * (n - 1) / n + req_size / n
                if stats["avg_response_size"] == 0:
                    stats["avg_response_size"] = resp_size
                else:
                    stats["avg_response_size"] = stats["avg_response_size"] * (n - 1) / n + resp_size / n
            req_bytes = request.content_length if request.content_length is not None else 0
            if req_bytes < 0:
                req_bytes = 0
            self._log_http(
                request,
                status,
                int(duration * 1000),
                req_bytes,
                resp_size,
                request.remote or "",
            )

    @web.middleware
    async def error_middleware(self, request, handler):
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except ExternalError as e:
            return external_response(e)
        except Exception as e:
            request["error_logged"] = True
            log_caught(self.logger, e, where="%s %s" % (request.method, request.path))
            return web.json_response({"result": 1, "msg": "Internal error"}, status=500)
