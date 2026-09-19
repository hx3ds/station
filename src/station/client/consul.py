import asyncio

import aiohttp

from station.client.retry import CircuitOpen, RetryableError, is_transient_status, retry_transient
from station.errors import ExternalError
from station.prototypes.boundary import ext_dict
from station import logger

async def _request_json(*, session, method, url, headers, json_payload=None):
    kwargs = {"headers": headers}
    if json_payload is not None:
        kwargs["json"] = json_payload
    async with session.request(method, url, **kwargs) as resp:
        if is_transient_status(resp.status):
            raise RetryableError("status=%s" % resp.status)
        if resp.status != 200:
            raise aiohttp.ClientResponseError(
                resp.request_info,
                resp.history,
                status=resp.status,
                message="http status",
                headers=resp.headers,
            )
        return await resp.json(content_type=None)

def _require_result_data(body, *, where):
    body = ext_dict("%s body" % where, body)
    if body.get("result") != 0:
        raise ExternalError("%s result=%s" % (where, body.get("result")))
    data = body.get("data")
    if data is not None:
        data = ext_dict("%s data" % where, data)
    return data

_EXPECTED_REMOTE = (CircuitOpen, aiohttp.ClientError, asyncio.TimeoutError, RetryableError)

async def fetch_model(*, session, consul_url, token, model_id):
    consul_url = consul_url.rstrip("/")
    url = "%s/api/station/get_model" % consul_url
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Prototype-Token"] = token
    payload = {"model_id": model_id}
    try:
        body = await retry_transient(
            _request_json,
            session=session,
            method="POST",
            url=url,
            headers=headers,
            json_payload=payload,
            circuit_name="consul",
        )
    except _EXPECTED_REMOTE as e:
        logger.error("fetch_model failed error=%s model_id=%s", e, model_id)
        return None
    return _require_result_data(body, where="fetch_model")

async def push_webrtc(*, session, consul_url, token, chat_id, webrtc_type, webrtc_content, session_id=""):
    consul_url = consul_url.rstrip("/")
    url = "%s/api/station/push_webrtc" % consul_url
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Prototype-Token"] = token
    payload = {
        "chat_id": chat_id,
        "session_id": session_id,
        "webrtc_type": webrtc_type,
        "content": webrtc_content if webrtc_content is not None else {},
    }
    try:
        body = await retry_transient(
            _request_json,
            session=session,
            method="POST",
            url=url,
            headers=headers,
            json_payload=payload,
            circuit_name="consul",
        )
    except _EXPECTED_REMOTE as e:
        logger.error("push_webrtc failed error=%s", e)
        return False
    body = ext_dict("push_webrtc body", body)
    return body.get("result") == 0

async def fetch_prototype(*, session, consul_url, token):
    consul_url = consul_url.rstrip("/")
    url = "%s/api/station/get_prototype" % consul_url
    headers = {}
    if token:
        headers["X-Prototype-Token"] = token
    try:
        body = await retry_transient(
            _request_json,
            session=session,
            method="GET",
            url=url,
            headers=headers,
            circuit_name="consul",
        )
    except _EXPECTED_REMOTE as e:
        logger.error("fetch_prototype failed error=%s", e)
        return None
    return _require_result_data(body, where="fetch_prototype")
