import asyncio
import json
import os

import aiohttp

from station.client.context import update_client_context_prototype
from station.client.retry import CircuitOpen, RetryableError, is_transient_status, retry_transient
from station.conductor.platforms.policy import plan_outbound
from station import logger
from station.prototypes.boundary import ext_dict, ext_int, ext_str

_SUPPORTED_REPLY_METHODS = {
    "send_message",
    "send_photo",
    "send_video",
    "send_audio",
    "send_document",
    "send_voice",
    "send_animation",
    "send_sticker",
}

_SUPPORTED_GATEWAY_METHODS = {
    "send_webrtc",
    "send_discord_voice",
    "send_typing",
    "download_file",
}

_SUPPORTED_METHODS = _SUPPORTED_REPLY_METHODS | _SUPPORTED_GATEWAY_METHODS

_outbound_sem: asyncio.Semaphore | None = None
_outbound_sem_limit = 0

def configure_outbound_concurrency(limit: int) -> None:
    global _outbound_sem, _outbound_sem_limit
    if limit <= 0:
        _outbound_sem = None
        _outbound_sem_limit = 0
        return
    if _outbound_sem is None or limit != _outbound_sem_limit:
        _outbound_sem = asyncio.Semaphore(limit)
        _outbound_sem_limit = limit

def _outbound_semaphore() -> asyncio.Semaphore | None:
    if _outbound_sem_limit <= 0:
        return None
    return _outbound_sem

async def _sync_prototype_from_consul(ctx):
    from station.client.consul import fetch_prototype

    fetched = await fetch_prototype(session=ctx.session, consul_url=ctx.consul_url, token=ctx.token)
    if not fetched:
        return False
    if ctx.db:
        await ctx.db.put_prototype(fetched)
    update_client_context_prototype(ctx, fetched)
    return True

def _ext_addr(value, name):
    if value is None:
        return ""
    return ext_str(name, value, default="").rstrip("/")

def _ext_version(value, name):
    if value is None:
        return 0
    return ext_int(name, value)

def _extract_conductor_address_from_491(body):
    body = ext_dict("conductor 491 body", body)
    data = body.get("data")
    if data is None:
        return ""
    data = ext_dict("conductor 491 data", data)
    return _ext_addr(data.get("conductor_address"), "conductor_address")

async def _update_conductor_addr(ctx, model_id, conductor_address):
    if not conductor_address:
        return
    conductor_address = conductor_address.strip().rstrip("/")
    if ctx.conductor_addresses is not None and model_id:
        ctx.conductor_addresses[model_id] = conductor_address
    if ctx.db:
        await ctx.db.update_model_conductor_addr(model_id, conductor_address)

async def _maybe_add_prototype_version_header(ctx, headers):
    version = ctx.prototype_version
    if version is None and ctx.db and ctx.prototype_id:
        stored = await ctx.db.get_prototype_info(ctx.prototype_id)
        if stored is None:
            version = 0
        else:
            version = _ext_version(stored.get("version"), "version")
        ctx.prototype_version = version
    if version is None:
        version = 0
    headers["X-Prototype-Version"] = str(version)

def _build_headers_fast(ctx, token):
    headers = {}
    if token:
        headers["X-Prototype-Token"] = token
    version = ctx.prototype_version
    headers["X-Prototype-Version"] = str(0 if version is None else version)
    return headers

async def _build_headers(ctx, token):
    headers = {}
    if token:
        headers["X-Prototype-Token"] = token
    await _maybe_add_prototype_version_header(ctx, headers)
    return headers

async def _get_prototype_type(ctx):
    cached = ctx.prototype_type
    if cached is None:
        cached = ""
    cached = cached.strip().lower()
    if cached:
        return cached
    if ctx.db and ctx.prototype_id:
        stored = await ctx.db.get_prototype_info(ctx.prototype_id)
        if stored is None:
            return ""
        prototype_type = stored.get("type")
        if prototype_type is None:
            return ""
        prototype_type = ext_str("type", prototype_type, default="").lower()
        if prototype_type:
            ctx.prototype_type = prototype_type
            return prototype_type
    return ""

def _local_tenant(ctx):
    if ctx.app is None or ctx.prototype_id is None:
        return None
    registry = ctx.app.get("tenants")
    if registry is None:
        return None
    tenant = registry.get(ctx.prototype_id)
    if tenant is None or not tenant.is_local:
        return None
    return tenant

def _local_conductor(ctx):
    if _local_tenant(ctx) is None:
        return None
    lc = ctx.app.get("local_conductor")
    if lc is None or not lc.enabled:
        return None
    return lc

async def _resolve_conductor_address(ctx, *, model_id, token):
    if ctx.conductor_addresses and model_id:
        cached = ctx.conductor_addresses.get(model_id)
        if cached is None:
            cached = ""
        cached = cached.strip()
        if cached:
            return cached

    conductor_address = ""
    if ctx.db and model_id:
        stored_model = await ctx.db.get_model(model_id)
        if stored_model is not None:
            conductor_address = _ext_addr(stored_model.get("conductor_address"), "conductor_address")

    if (
        not conductor_address
        and ctx.session
        and ctx.consul_url.strip()
        and token.strip()
        and model_id
    ):
        from station.client.consul import fetch_model

        remote = await fetch_model(session=ctx.session, consul_url=ctx.consul_url, token=token, model_id=model_id)
        if remote is None:
            logger.error("fetch_model failed error=none model_id=%s", model_id)
        else:
            conductor_address = _ext_addr(remote.get("conductor_address"), "conductor_address")
            if conductor_address and ctx.db:
                await ctx.db.put_model(model_id, remote)

    if not conductor_address:
        lc = _local_conductor(ctx)
        if lc is not None:
            conductor_address = _ext_addr(lc.conductor_address(), "conductor_address")

    if not conductor_address:
        return conductor_address

    if ctx.conductor_addresses is not None and model_id:
        ctx.conductor_addresses[model_id] = conductor_address
    return conductor_address

async def _invoke_local(
    ctx,
    *,
    model_id,
    method,
    params,
    files,
    request_id,
    chat_id,
    acct_id,
    use_gateway,
):
    lc = _local_conductor(ctx)
    if lc is None:
        return None
    return await lc.invoke(
        method=method,
        prototype_id=ctx.prototype_id,
        model_id=model_id,
        chat_id=chat_id,
        request_id=request_id,
        acct_id=acct_id,
        params=params,
        files=files,
        use_gateway=use_gateway,
    )

def _prepare_request_data(payload, files, method=None):
    if not files:
        return {"json": payload}, []

    data = aiohttp.FormData()
    for k, v in payload.items():
        if isinstance(v, (dict, list)):
            data.add_field(k, json.dumps(v))
        else:
            data.add_field(k, v)

    opened_files = []
    try:
        for field_name, file_path in files.items():
            f = open(file_path, "rb")
            opened_files.append(f)
            data.add_field(field_name, f)
        return {"data": data}, opened_files
    except Exception:
        for f in opened_files:
            f.close()
        raise

def _close_opened_files(opened_files):
    for file_handle in opened_files:
        file_handle.close()

def _build_conductor_url(
    conductor_url,
    *,
    method,
    prototype_id,
    model_id,
    chat_id=None,
    request_id=None,
    acct_id=None,
    use_gateway=False,
):
    if method in _SUPPORTED_GATEWAY_METHODS or use_gateway:
        if not acct_id:
            return None
        chat_id_str = chat_id if chat_id else "0"
        return "%s/gateway/%s/%s/%s/%s/%s" % (conductor_url, method, chat_id_str, prototype_id, model_id, acct_id)

    if chat_id is None or not request_id:
        return None
    return "%s/reply/%s/%s/%s/%s/%s" % (conductor_url, method, chat_id, request_id, prototype_id, model_id)

async def _conductor_proxy(
    ctx,
    *,
    model_id,
    token,
    method,
    params,
    files,
    request_id,
    chat_id,
    acct_id,
):
    if method not in _SUPPORTED_METHODS:
        return False

    if not ctx.prototype_version:
        await _sync_prototype_from_consul(ctx)
    use_gateway = (await _get_prototype_type(ctx)) == "subscription" or method in _SUPPORTED_GATEWAY_METHODS

    local_result = await _invoke_local(
        ctx,
        model_id=model_id,
        method=method,
        params=params,
        files=files,
        request_id=request_id,
        chat_id=chat_id,
        acct_id=acct_id,
        use_gateway=use_gateway,
    )
    if local_result is not None:
        return bool(local_result)

    if not ctx.session:
        return False

    conductor_url = await _resolve_conductor_address(ctx, model_id=model_id, token=token)
    if not conductor_url:
        return False

    prototype_id = ctx.prototype_id

    for attempt in range(2):
        url = _build_conductor_url(
            conductor_url,
            method=method,
            prototype_id=prototype_id,
            model_id=model_id,
            chat_id=chat_id,
            request_id=request_id,
            acct_id=acct_id,
            use_gateway=use_gateway,
        )
        if not url:
            return False

        body = dict(params or {})
        if not use_gateway and method not in _SUPPORTED_GATEWAY_METHODS and acct_id:
            body["acct_id"] = acct_id
        request_kwargs, opened_files = _prepare_request_data(body, files, method)
        try:
            if ctx.prototype_version:
                headers = _build_headers_fast(ctx, token)
            else:
                headers = await _build_headers(ctx, token)

            async def _post():
                async with ctx.session.post(url, headers=headers, **request_kwargs) as response:
                    status = response.status
                    if is_transient_status(status):
                        raise RetryableError("status=%s" % status)
                    body = None
                    if status in (490, 491):
                        body = await response.json(content_type=None)
                    return status, body

            try:
                status, body = await retry_transient(_post, circuit_name="conductor")
            except CircuitOpen:
                return False
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError, RetryableError) as e:
                logger.error("conductor_proxy failed method=%s error=%s", method, e)
                return False
            if status == 490 and attempt == 0:
                if await _sync_prototype_from_consul(ctx):
                    continue
            if status == 491 and attempt == 0:
                new_url = _extract_conductor_address_from_491(body)
                if new_url:
                    await _update_conductor_addr(ctx, model_id, new_url)
                    conductor_url = new_url
                    continue
            if status >= 500:
                logger.error("conductor_proxy failed method=%s status=%s", method, status)
                return False
            if status >= 400:
                logger.warning("conductor_proxy failed method=%s status=%s", method, status)
                return False
            logger.debug("conductor_proxy ok: method=%s", method)
            return True
        finally:
            _close_opened_files(opened_files)

    return False

async def send_proxy(
    ctx,
    *,
    model_id=None,
    token=None,
    method="send_message",
    params=None,
    files=None,
    request_id=None,
    chat_id=None,
    acct_id=None,
    **kwargs,
):
    if params is None:
        params = {}
    params = dict(params)
    params.update(kwargs)

    if token is None and ctx.token:
        token = ctx.token

    sem = _outbound_semaphore()
    if sem is None:
        return await _conductor_proxy(
            ctx,
            model_id=model_id,
            token=token,
            method=method,
            params=params,
            files=files,
            request_id=request_id,
            chat_id=chat_id,
            acct_id=acct_id,
        )
    async with sem:
        return await _conductor_proxy(
            ctx,
            model_id=model_id,
            token=token,
            method=method,
            params=params,
            files=files,
            request_id=request_id,
            chat_id=chat_id,
            acct_id=acct_id,
        )

async def _gateway_file_transfer(
    ctx,
    *,
    model_id,
    token,
    method,
    params,
    max_attempts,
    acct_id,
    chat_id,
    error_label,
):
    if not ctx.session:
        return None
    if not acct_id:
        return None

    conductor_url = await _resolve_conductor_address(ctx, model_id=model_id, token=token)
    if not conductor_url:
        return None

    prototype_id = ctx.prototype_id
    chat_id_str = chat_id if chat_id else "0"
    request_kwargs, _ = _prepare_request_data(params, None, method=method)

    for attempt in range(max_attempts):
        url = "%s/gateway/%s/%s/%s/%s/%s" % (conductor_url, method, chat_id_str, prototype_id, model_id, acct_id)
        headers = await _build_headers(ctx, token)

        async def _post():
            async with ctx.session.post(url, headers=headers, **request_kwargs) as response:
                status = response.status
                if is_transient_status(status):
                    raise RetryableError("status=%s" % status)
                body = None
                raw = None
                if status in (490, 491):
                    body = await response.json(content_type=None)
                elif status < 400:
                    raw = await response.read()
                return status, body, raw

        try:
            status, body, raw = await retry_transient(_post, circuit_name="conductor")
        except CircuitOpen:
            return None
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError, RetryableError) as e:
            logger.error("%s failed error=%s", error_label, e)
            return None
        if status == 490:
            if await _sync_prototype_from_consul(ctx):
                continue
        if status == 491:
            new_url = _extract_conductor_address_from_491(body)
            if new_url:
                await _update_conductor_addr(ctx, model_id, new_url)
                conductor_url = new_url
                continue
        if status >= 500:
            logger.error("%s failed status=%s", error_label, status)
            continue
        if status >= 400:
            logger.warning("%s failed status=%s", error_label, status)
            continue
        return raw

    return None

async def download_file(ctx, *, model_id=None, token=None, chat_id=None, file_id=None, max_attempts=3, acct_id=None):
    if token is None and ctx.token:
        token = ctx.token
    if not file_id:
        return None
    local_result = await _invoke_local(
        ctx,
        model_id=model_id,
        method="download_file",
        params={"file_id": file_id},
        files=None,
        request_id=None,
        chat_id=chat_id,
        acct_id=acct_id,
        use_gateway=True,
    )
    if local_result is not None:
        if local_result is False:
            return None
        return local_result
    return await _gateway_file_transfer(
        ctx,
        model_id=model_id,
        token=token,
        method="download_file",
        params={"file_id": file_id},
        max_attempts=max_attempts,
        acct_id=acct_id,
        chat_id=chat_id,
        error_label="Download",
    )

def resolve_attachment(proto, attachment):
    item = dict(attachment)
    fid = item.get("file_id")
    if not fid:
        return item
    remote = proto.remote_file_id_for(fid)
    if remote:
        item["remote_id"] = remote
    row = proto.file_row(fid)
    if row and row.get("status") == "ready":
        path = proto.resolve_file_id(fid)
        if os.path.isfile(path):
            item["local_path"] = path
    return item

async def _ensure_path(proto, file_id):
    if not file_id:
        return None
    try:
        meta = await proto.download_chat_file(file_id=file_id)
    except Exception as e:
        logger.error("unexpected where=outbound_ensure_path file_id=%s error=%s", file_id, e, exc_info=e)
        return None
    if not meta:
        return None
    return proto.resolve_file_id(file_id)

async def _send_step(proto, step, *, chat_id, request_id, acct_id):
    raw_params = step.get("params")
    params = dict(raw_params) if raw_params is not None else {}
    files = step.get("files")
    download = step.get("download")
    reused = step.get("reused")
    if reused is None:
        reused = False
    if download and not files and not reused:
        path = await _ensure_path(proto, download)
        if not path:
            return False
        field = step.get("field")
        files = {field if field is not None else "document": path}
    method = step.get("method")
    if method is None:
        method = "send_message"
    ok = await proto.send_proxy(
        method=method,
        chat_id=chat_id,
        request_id=request_id,
        params=params,
        files=files,
        acct_id=acct_id,
    )
    if ok or not reused or not download:
        return ok
    path = await _ensure_path(proto, download)
    if not path:
        return False
    retry = dict(params)
    field = step.get("field")
    if field:
        retry.pop(field, None)
    return await proto.send_proxy(
        method=method,
        chat_id=chat_id,
        request_id=request_id,
        params=retry,
        files={field if field is not None else "document": path},
        acct_id=acct_id,
    )

async def send_outbound(
    proto,
    *,
    text="",
    attachments=None,
    reply_to=None,
    chat_id=None,
    acct_id=None,
    request_id=None,
    platform="",
    chat_type="",
    keyboard=None,
):
    if not proto.client_context:
        return False
    if platform is None:
        platform = ""
    if chat_type is None:
        chat_type = ""
    if reply_to is not None and str(reply_to).startswith("callback:"):
        reply_to = ""
    resolved = []
    for att in attachments if attachments is not None else []:
        resolved.append(resolve_attachment(proto, att))
    steps = plan_outbound(
        platform,
        text=text,
        attachments=resolved,
        reply_to="" if reply_to is None else reply_to,
        chat_type=chat_type,
        keyboard=keyboard,
    )
    if not steps:
        return False
    ok = True
    for step in steps:
        if chat_type:
            step["params"]["chat_type"] = chat_type
        sent = await _send_step(
            proto,
            step,
            chat_id=chat_id,
            request_id=request_id,
            acct_id=acct_id,
        )
        if not sent:
            ok = False
    return ok
