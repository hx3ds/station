from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

from station.api.http import log_caught
from station.config.config import Config
from station.errors import ExternalError
from station import logger

def _mark(out: list[str], path: str, changed: bool) -> None:
    if changed:
        out.append(path)

def _sighup_diffs(current: Config, loaded: Config) -> list[str]:
    applied: list[str] = []
    _mark(applied, "admin_token", current.admin_token != loaded.admin_token)
    _mark(applied, "telegram_api_url", current.telegram_api_url != loaded.telegram_api_url)
    _mark(
        applied,
        "telegram_webhook_mode",
        current.telegram_webhook_mode != loaded.telegram_webhook_mode,
    )
    _mark(
        applied,
        "telegram_webhook_secret",
        current.telegram_webhook_secret != loaded.telegram_webhook_secret,
    )
    _mark(applied, "discord_api_url", current.discord_api_url != loaded.discord_api_url)
    _mark(
        applied,
        "whatsapp_cloud_api_url",
        current.whatsapp_cloud_api_url != loaded.whatsapp_cloud_api_url,
    )
    _mark(applied, "qq_api_url", current.qq_api_url != loaded.qq_api_url)
    _mark(applied, "qq_token_url", current.qq_token_url != loaded.qq_token_url)
    _mark(applied, "consul_url", current.consul_url != loaded.consul_url)
    _mark(applied, "server.isolate_after", current.server.isolate_after != loaded.server.isolate_after)
    _mark(
        applied,
        "server.outbound_retry_attempts",
        current.server.outbound_retry_attempts != loaded.server.outbound_retry_attempts,
    )
    _mark(
        applied,
        "server.outbound_retry_base_seconds",
        current.server.outbound_retry_base_seconds != loaded.server.outbound_retry_base_seconds,
    )
    _mark(
        applied,
        "server.outbound_circuit_failures",
        current.server.outbound_circuit_failures != loaded.server.outbound_circuit_failures,
    )
    _mark(
        applied,
        "server.outbound_circuit_cooldown_seconds",
        current.server.outbound_circuit_cooldown_seconds != loaded.server.outbound_circuit_cooldown_seconds,
    )
    _mark(applied, "recorder.level", current.recorder.level != loaded.recorder.level)
    _mark(
        applied,
        "recorder.smoothing_window",
        current.recorder.smoothing_window != loaded.recorder.smoothing_window,
    )
    cur_proto = current.prototype
    load_proto = loaded.prototype
    _mark(applied, "prototype.ava", cur_proto.ava != load_proto.ava)
    _mark(applied, "prototype.reply_to", cur_proto.reply_to != load_proto.reply_to)
    return applied

def _postmaster_diffs(current: Config, loaded: Config) -> list[str]:
    pending: list[str] = []
    cs, ls = current.server, loaded.server
    _mark(pending, "host", current.host != loaded.host)
    _mark(pending, "port", current.port != loaded.port)
    _mark(pending, "server.listen_backlog", cs.listen_backlog != ls.listen_backlog)
    _mark(pending, "env", current.env != loaded.env)
    _mark(pending, "server.inbound_workers", cs.inbound_workers != ls.inbound_workers)
    _mark(pending, "server.inbound_queue", cs.inbound_queue != ls.inbound_queue)
    _mark(pending, "server.inbound_batch", cs.inbound_batch != ls.inbound_batch)
    _mark(
        pending,
        "server.inbound_at_least_once",
        cs.inbound_at_least_once != ls.inbound_at_least_once,
    )
    _mark(
        pending,
        "server.outbound_concurrency",
        cs.outbound_concurrency != ls.outbound_concurrency,
    )
    _mark(
        pending,
        "server.request_dedupe_ttl_seconds",
        cs.request_dedupe_ttl_seconds != ls.request_dedupe_ttl_seconds,
    )
    _mark(
        pending,
        "server.dedupe_cleanup_interval_seconds",
        cs.dedupe_cleanup_interval_seconds != ls.dedupe_cleanup_interval_seconds,
    )

    cd, ld = current.database, loaded.database
    _mark(pending, "database.backend", cd.backend != ld.backend)
    _mark(pending, "database.path", cd.path != ld.path)
    _mark(pending, "database.dsn", cd.dsn != ld.dsn)
    _mark(pending, "database.wipe_on_restart", cd.wipe_on_restart != ld.wipe_on_restart)

    _mark(pending, "fs_root", current.fs_root != loaded.fs_root)

    cr, lr = current.recorder, loaded.recorder
    _mark(pending, "recorder.dir_path", cr.dir_path != lr.dir_path)
    _mark(pending, "recorder.format", cr.format != lr.format)
    _mark(pending, "recorder.max_bytes", cr.max_bytes != lr.max_bytes)
    _mark(pending, "recorder.backup_count", cr.backup_count != lr.backup_count)

    cl, ll = current.local_conductor, loaded.local_conductor
    _mark(pending, "local_conductor.private_key_path", cl.private_key_path != ll.private_key_path)
    _mark(pending, "local_conductor.public_key_path", cl.public_key_path != ll.public_key_path)
    _mark(pending, "local_conductor.address", cl.address != ll.address)

    cur_ids = [(p.id, p.token.strip()) for p in current.hosted_prototypes]
    load_ids = [(p.id, p.token.strip()) for p in loaded.hosted_prototypes]
    _mark(pending, "hosted_prototypes", cur_ids != load_ids)
    return pending

def _restore_postmaster(dst: Config, src: Config) -> None:
    loaded_ava = dst.prototype.ava
    loaded_reply_to = dst.prototype.reply_to
    loaded_level = dst.recorder.level
    loaded_smoothing = dst.recorder.smoothing_window
    loaded_consul = dst.consul_url
    loaded_isolate = dst.server.isolate_after
    loaded_retry_attempts = dst.server.outbound_retry_attempts
    loaded_retry_base = dst.server.outbound_retry_base_seconds
    loaded_circuit_failures = dst.server.outbound_circuit_failures
    loaded_circuit_cooldown = dst.server.outbound_circuit_cooldown_seconds

    dst.server = replace(
        src.server,
        isolate_after=loaded_isolate,
        outbound_retry_attempts=loaded_retry_attempts,
        outbound_retry_base_seconds=loaded_retry_base,
        outbound_circuit_failures=loaded_circuit_failures,
        outbound_circuit_cooldown_seconds=loaded_circuit_cooldown,
    )
    dst.env = src.env
    dst.host = src.host
    dst.port = src.port
    dst.consul_url = loaded_consul
    dst.fs_root = src.fs_root
    dst.database = copy.deepcopy(src.database)
    dst.local_conductor = copy.deepcopy(src.local_conductor)
    dst.recorder = replace(
        src.recorder,
        level=loaded_level,
        smoothing_window=loaded_smoothing,
    )

    hosted = copy.deepcopy(src.hosted_prototypes)
    primary = hosted[0]
    primary.ava = loaded_ava
    primary.reply_to = loaded_reply_to
    dst.hosted_prototypes = hosted

def reload_from_disk(current_config: Config) -> dict[str, Any]:
    config_path = current_config.config_file_path
    if not config_path or not config_path.strip():
        raise ExternalError("missing config_file_path")
    secret_path = current_config.secret_file_path

    loaded = Config(
        config_file=config_path,
        secret_file=secret_path,
    )
    applied = _sighup_diffs(current_config, loaded)
    pending_restart = _postmaster_diffs(current_config, loaded)
    _restore_postmaster(loaded, current_config)

    return {
        "config": loaded,
        "applied": applied,
        "pending_restart": pending_restart,
        "unchanged": len(applied) == 0,
    }

def apply_reloaded_config(app, result: dict[str, Any]) -> None:
    new_config: Config = result["config"]
    applied = result["applied"]
    app["config"] = new_config

    station = app["_station"]
    station.config = new_config

    tenants = app["tenants"]
    primary = tenants.primary
    primary.client_context.consul_url = new_config.consul_url
    if "prototype.ava" in applied or "prototype.reply_to" in applied:
        primary.ava = new_config.prototype.ava
        primary.reply_to = new_config.prototype.reply_to

    from station.client.retry import apply_retry_config

    apply_retry_config(new_config.server)

    app["env"] = new_config.env
    app["fs_root"] = new_config.fs_root

    recorder = app["recorder"]
    if "recorder.level" in applied:
        try:
            recorder.set_log_level(new_config.recorder.level)
        except Exception as e:
            log_caught(logger, e, where="reload recorder.level")
    if "recorder.smoothing_window" in applied:
        try:
            recorder.smoothing_window = new_config.recorder.smoothing_window
        except Exception as e:
            log_caught(logger, e, where="reload recorder.smoothing_window")

    recorder.config.level = new_config.recorder.level
    recorder.config.smoothing_window = new_config.recorder.smoothing_window
