import json
import os
import tempfile
import time
import uuid
from aiohttp import web
from aiohttp.web_request import FileField
from station.client.context import update_client_context_prototype
from station.api.instances import (
    create_tenant_instance,
    isolate_instance,
    note_instance_ok,
    note_instance_unexpected,
    should_isolate,
)
from station.api.tenant_auth import authorize_request
from station.hopmetrics import observe as observe_hop
from station.prototypes.attachments import (
    attachment_from_params,
    attachment_type_for_method,
    merge_attachments,
)
from station import logger
from station.api.http import ext_path_int, log_caught, read_json_object
from station.errors import ExternalError
from station.prototypes.boundary import ext_bool, ext_dict, ext_list, ext_require, ext_str

def _same_prototype(instance, tenant_id) -> bool:
    return instance.prototype_id == tenant_id

def relay_error(*, reason, msg, status, retryable=None):
    raise ExternalError(msg, status=status, reason=reason, retryable=retryable)

def is_slash_command(text):
    t = text.strip()
    if not t.startswith("/"):
        return False
    first = t.split(None, 1)[0]
    cmd = first.split("@", 1)[0].lower()
    return cmd.startswith("/") and len(cmd) > 1

def _normalize_conductor_message_body(body):
    data = dict(body)
    if "params" in data and data["params"] is not None:
        params = ext_dict("params", data["params"])
    else:
        params = {}
    if "method" in data and data["method"] is not None:
        method = ext_str("method", data["method"], default="", strip=False)
    else:
        method = ""
    if "text" in data and data["text"] is not None:
        ext_require("text", data["text"], (str,))
    if "caption" in data and data["caption"] is not None:
        ext_require("caption", data["caption"], (str,))
    if "text" in params and params["text"] is not None:
        ext_require("text", params["text"], (str,))
    if "caption" in params and params["caption"] is not None:
        ext_require("caption", params["caption"], (str,))
    if "text" in data and data["text"] is not None:
        text = data["text"]
    elif "text" in params and params["text"] is not None:
        text = params["text"]
    else:
        text = ""
    if "caption" in data and data["caption"] is not None:
        caption = data["caption"]
    elif "caption" in params and params["caption"] is not None:
        caption = params["caption"]
    else:
        caption = ""
    if "attachments" in data and data["attachments"] is not None:
        attachments = ext_list("attachments", data["attachments"])
    else:
        attachments = []
    if not attachments:
        param_attachment = attachment_from_params(method, params)
        if param_attachment is not None:
            attachments = [param_attachment]

    if "webrtc" in data and data["webrtc"] is not None:
        ext_dict("webrtc", data["webrtc"])
    if "discord_voice" in data and data["discord_voice"] is not None:
        ext_dict("discord_voice", data["discord_voice"])
    if "is_expired" in data:
        is_expired = ext_bool("is_expired", data["is_expired"])
    else:
        is_expired = False
    if "platform" in data and data["platform"] is not None:
        platform = ext_str("platform", data["platform"], default="", strip=False)
    else:
        platform = ""
    if "chat_type" in data and data["chat_type"] is not None:
        chat_type = ext_str("chat_type", data["chat_type"], default="", strip=False)
    else:
        chat_type = ""

    data["method"] = method
    data["params"] = params
    data["text"] = text
    data["caption"] = caption
    data["attachments"] = attachments
    data["is_expired"] = is_expired
    data["platform"] = platform
    data["chat_type"] = chat_type
    data["webrtc"] = data["webrtc"] if "webrtc" in data else None
    data["discord_voice"] = data["discord_voice"] if "discord_voice" in data else None
    return data

def _instance_delivery_rejection_response(result):
    if result is False:
        return relay_error(reason="", msg="message queue is full", status=503, retryable=True)
    return None

async def _start_instance(*, app, instance, model_id):
    try:
        await instance.start()
    except ExternalError as e:
        await isolate_instance(app, model_id, instance)
        raise ExternalError(str(e), status=400, reason="invalid_request")
    except Exception as e:
        log_caught(logger, e, where="reception start_instance model_id=%s" % model_id)
        await isolate_instance(app, model_id, instance)
        raise
    return None

async def _deliver(*, app, instance, model_id, deliver):
    try:
        result = await deliver()
    except Exception as e:
        log_caught(logger, e, where="reception deliver model_id=%s" % model_id)
        count = note_instance_unexpected(app, model_id)
        if should_isolate(count, app):
            await isolate_instance(app, model_id, instance)
        raise
    note_instance_ok(app, model_id)
    return result

async def _check_or_sync_prototype_version(request, app, tenant):
    db = app["db"]
    config = app["config"]

    incoming_version_raw = request.headers.get("X-Prototype-Version") or ""
    if incoming_version_raw == "":
        raise ExternalError("Synchronize from consul", status=490)
    try:
        incoming_version = ext_path_int("X-Prototype-Version", incoming_version_raw)
    except ExternalError:
        raise ExternalError("Synchronize from consul", status=490)

    ctx = tenant.client_context
    if ctx is not None and ctx.prototype_version is not None:
        local_version = ctx.prototype_version
    else:
        local_version = 0
    if local_version == incoming_version:
        return None

    stored = await db.get_prototype_info(tenant.id)
    if stored:
        if stored.get("version") is not None:
            local_version = stored["version"]
        if ctx is not None:
            ctx.prototype_version = local_version
    if local_version == incoming_version:
        return None

    session = app["session"]
    consul_url = config.consul_url
    token = tenant.token_state.token
    from station.client.consul import fetch_prototype
    fetched = await fetch_prototype(session=session, consul_url=consul_url, token=token)
    if fetched:
        await db.put_prototype(fetched)
        if ctx is not None:
            update_client_context_prototype(ctx, fetched)
            local_version = 0 if ctx.prototype_version is None else ctx.prototype_version
        else:
            local_version = 0 if fetched.get("version") is None else fetched["version"]
    if local_version == incoming_version:
        return None

    return relay_error(reason="", msg="Synchronize from consul", status=490, retryable=True)

async def handle_reception(request):
    start = time.perf_counter()
    request_id = request.headers.get("X-Request-Id", "")
    model_id = request.match_info.get("model_id") or request.headers.get("X-Model-Id", "")
    outcome = "failed"
    status = 500
    try:
        resp = await _handle_reception(request)
        status = resp.status or 500
        if 200 <= status < 300:
            outcome = "succeeded"
        return resp
    finally:
        duration_ms = (time.perf_counter() - start) * 1000.0
        observe_hop("station_reception_to_reply", outcome, duration_ms)
        logger.debug(
            "flow_event flow=station_reception_to_reply outcome=%s duration_ms=%.3f status=%s request_id=%s model_id=%s",
            outcome,
            duration_ms,
            status,
            request_id,
            model_id,
        )

async def _handle_reception(request):
    app = request.app
    db = app['db']
    config = app['config']

    model_id = request.match_info.get('model_id')
    if not model_id:
        return relay_error(reason="missing_model_id", msg="Missing model_id", status=400)

    tenant = authorize_request(request)
    if tenant is None:
        return relay_error(reason="token_misconfigured", msg="Prototype token misconfigured", status=401)
    token = tenant.token_state.token

    version_resp = await _check_or_sync_prototype_version(request, app, tenant)
    if version_resp is not None:
        return version_resp

    request_id = request.headers.get("X-Request-Id", "")
    chat_id = request.headers.get("X-Chat-Id")
    acct_id = request.headers.get("X-Acct-Id") or ""
    if not acct_id:
        return relay_error(reason="missing_acct_id", msg="Missing acct_id", status=400)
    model_id = request.headers.get("X-Model-Id", model_id)

    dedupe_key = ""
    if request_id:
        dedupe_key = f"{model_id}:{request_id}"
        if await db.is_duplicate_request(dedupe_key):
            return web.json_response({"result": 0, "msg": "Duplicate", "data": None}, status=200)

    if request.content_type and request.content_type.startswith("multipart/"):
        form = await request.post()

        body = {}
        attachments = []
        for k, v in form.items():
            if not isinstance(v, FileField):
                body[k] = v
                continue
            filename = v.filename or ""
            _, ext = os.path.splitext(filename)
            tmp_path = os.path.join(tempfile.gettempdir(), f"station_rx_{uuid.uuid4().hex}{ext}")
            v.file.seek(0)
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = v.file.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
            attachments.append(
                {
                    "type": "document",
                    "file_name": filename,
                    "content_type": v.content_type,
                    "local_path": tmp_path,
                    "field": k,
                }
            )

        params_raw = body.get("params")
        if params_raw:
            try:
                body["params"] = json.loads(params_raw)
            except json.JSONDecodeError:
                raise ExternalError("Invalid params JSON", reason="invalid_request")

        method = body.get("method")
        att_type = attachment_type_for_method(method)
        if att_type:
            for att in attachments:
                att["type"] = att_type

        if attachments:
            body["attachments"] = merge_attachments(body.get("attachments"), attachments)
    else:
        raw = await request.read()
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise ExternalError("Invalid JSON body", reason="invalid_request")
        body = ext_dict("body", body)
    body = _normalize_conductor_message_body(body)

    session = app["session"]
    consul_url = config.consul_url
    instances = app['instances']
    instance_lock = app['instance_lock']

    instance = instances.get(model_id)
    settings = None
    created = False
    if instance is not None and _same_prototype(instance, tenant.id):
        settings = instance.model_settings
        if not instance._started:
            start_resp = await _start_instance(app=app, instance=instance, model_id=model_id)
            if start_resp is not None:
                return start_resp
    else:
        model = await db.get_or_fetch_model(model_id, session, consul_url, token)
        if not model:
            return relay_error(reason="model_not_found", msg="Model not found", status=404)

        prototype_id = model.get("prototype_id")
        if prototype_id != tenant.id:
            return relay_error(reason="prototype_mismatch", msg="Prototype mismatch", status=409)

        settings = model.get("settings")
        if settings is None:
            settings = {}
        else:
            settings = ext_dict("settings", settings)
        if not await db.ensure_model_lock(app, model_id):
            return relay_error(reason="model_locked", msg="Model is active on another station", status=503, retryable=True)
        async with instance_lock:
            instance = instances.get(model_id)
            if not instance:
                instance = create_tenant_instance(
                    app,
                    tenant,
                    prototype_id=prototype_id,
                    model_id=model_id,
                    model_settings=settings,
                )
                instances[model_id] = instance
                created = True

        if created:
            start_resp = await _start_instance(app=app, instance=instance, model_id=model_id)
            if start_resp is not None:
                return start_resp
        elif not instance._started:
            start_resp = await _start_instance(app=app, instance=instance, model_id=model_id)
            if start_resp is not None:
                return start_resp

    body = instance.rewrite_inbound_attachments(body, chat_id=chat_id, acct_id=acct_id)

    if is_slash_command(body["text"]):
        deliver = lambda: instance.handle_command(
            body,
            model_id=model_id,
            model_settings=settings,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        )
    else:
        deliver = lambda: instance.handle_message(
            body,
            model_id=model_id,
            model_settings=settings,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        )

    accepted = await _deliver(app=app, instance=instance, model_id=model_id, deliver=deliver)
    delivery_resp = _instance_delivery_rejection_response(accepted)
    if delivery_resp is not None:
        logger.warning("queue full model_id=%s", model_id)
        return delivery_resp
    if dedupe_key:
        await db.remember_request(dedupe_key)
    return web.json_response({"result": 0, "msg": "ok", "data": None})

async def handle_event(request):
    app = request.app
    db = app['db']
    config = app['config']

    model_id = request.match_info.get('model_id')
    if not model_id:
        return relay_error(reason="missing_model_id", msg="Missing model_id", status=400)

    tenant = authorize_request(request)
    if tenant is None:
        return relay_error(reason="token_misconfigured", msg="Prototype token misconfigured", status=401)
    token = tenant.token_state.token

    version_resp = await _check_or_sync_prototype_version(request, app, tenant)
    if version_resp is not None:
        return version_resp

    request_id = request.headers.get("X-Request-Id", "")
    chat_id = request.headers.get("X-Chat-Id") or ""
    acct_id = request.headers.get("X-Acct-Id") or ""
    event_level = (request.headers.get("X-Event-Level") or "").lower()
    if not acct_id:
        return relay_error(reason="missing_acct_id", msg="Missing acct_id", status=400)
    if event_level not in ("acct", "chat"):
        return relay_error(reason="invalid_request", msg="Missing or invalid event_level", status=400)
    if event_level == "acct" and chat_id:
        return relay_error(reason="invalid_request", msg="chat_id must be empty for acct-level event", status=400)
    if event_level == "chat" and not chat_id:
        return relay_error(reason="invalid_request", msg="Missing chat_id for chat-level event", status=400)
    model_id = request.headers.get("X-Model-Id", model_id)

    dedupe_key = ""
    if request_id:
        dedupe_key = f"{model_id}:{request_id}"
        if await db.is_duplicate_request(dedupe_key):
            return web.json_response({"result": 0, "msg": "Duplicate", "data": None}, status=200)

    body = dict(await read_json_object(request))

    session = app["session"]
    consul_url = config.consul_url
    model = await db.get_or_fetch_model(model_id, session, consul_url, token)

    if not model:
        return relay_error(reason="model_not_found", msg="Model not found", status=404)

    prototype_id = model.get("prototype_id")
    if prototype_id != tenant.id:
        return relay_error(reason="prototype_mismatch", msg="Prototype mismatch", status=409)

    settings = model.get("settings")
    if settings is None:
        settings = {}
    else:
        settings = ext_dict("settings", settings)
    instances = app['instances']
    instance_lock = app['instance_lock']

    created = False
    if not await db.ensure_model_lock(app, model_id):
        return relay_error(reason="model_locked", msg="Model is active on another station", status=503, retryable=True)
    async with instance_lock:
        instance = instances.get(model_id)
        if not instance:
            instance = create_tenant_instance(
                app,
                tenant,
                prototype_id=prototype_id,
                model_id=model_id,
                model_settings=settings,
            )
            instances[model_id] = instance
            created = True

    if created:
        start_resp = await _start_instance(app=app, instance=instance, model_id=model_id)
        if start_resp is not None:
            return start_resp
    elif not instance._started:
        start_resp = await _start_instance(app=app, instance=instance, model_id=model_id)
        if start_resp is not None:
            return start_resp

    accepted = await _deliver(
        app=app,
        instance=instance,
        model_id=model_id,
        deliver=lambda: instance.handle_event(
            body,
            model_id=model_id,
            model_settings=settings,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        ),
    )
    delivery_resp = _instance_delivery_rejection_response(accepted)
    if delivery_resp is not None:
        logger.warning("queue full model_id=%s", model_id)
        return delivery_resp
    if dedupe_key:
        await db.remember_request(dedupe_key)
    return web.json_response({"result": 0, "msg": "ok", "data": None})
