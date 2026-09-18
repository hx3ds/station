import json
import time
from aiohttp import web

from station.api.tenant_auth import authorize_request
from station.prototypes.boundary import ext_dict, ext_int, ext_str

async def handle_webrtc_offer(request):
    app = request.app

    model_id = request.match_info.get("model_id")
    if not model_id:
        return web.json_response({"result": 1, "msg": "Missing model_id", "data": None}, status=400)

    tenant = authorize_request(request)
    if tenant is None:
        return web.json_response({"result": 1, "msg": "Unauthorized", "data": None}, status=401)

    db = app["db"]
    info = await db.get_prototype_info(tenant.id)
    if info is None:
        return web.json_response({"result": 1, "msg": "Prototype does not support calls", "data": None}, status=403)
    call_support = info.get("call_support")
    if call_support is not True:
        return web.json_response({"result": 1, "msg": "Prototype does not support calls", "data": None}, status=403)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"result": 1, "msg": "Invalid JSON body", "data": None}, status=400)
    try:
        body = ext_dict("body", body)
    except TypeError:
        return web.json_response({"result": 1, "msg": "Invalid JSON body", "data": None}, status=400)

    try:
        sdp = ext_str("sdp", body.get("sdp"), default="", strip=False)
    except TypeError:
        return web.json_response({"result": 1, "msg": "Missing sdp", "data": None}, status=400)
    if not sdp.strip():
        return web.json_response({"result": 1, "msg": "Missing sdp", "data": None}, status=400)

    webrtc = app["webrtc"]
    if webrtc is None:
        return web.json_response({"result": 1, "msg": "WebRTC not enabled", "data": None}, status=501)

    chat_id = request.headers.get("X-Chat-Id")
    if chat_id is None:
        chat_id = ""
    acct_id = request.headers.get("X-Acct-Id")
    if acct_id is None:
        acct_id = ""
    request_id = request.headers.get("X-Request-Id")
    if request_id is None:
        request_id = ""
    call_id = request.headers.get("X-Call-Id")
    if call_id is None:
        call_id = body.get("call_id")
    if call_id is None:
        call_id = ""
    else:
        try:
            call_id = ext_str("call_id", call_id, default="", strip=False)
        except TypeError:
            return web.json_response({"result": 1, "msg": "Invalid call_id", "data": None}, status=400)
    call_version = body.get("version")
    if call_version is not None:
        try:
            call_version = ext_int("version", call_version)
        except TypeError:
            return web.json_response({"result": 1, "msg": "Invalid version", "data": None}, status=400)

    answer_sdp = await webrtc.handle_offer(
        offer_sdp=sdp,
        model_id=model_id,
        chat_id=chat_id,
        acct_id=acct_id,
        request_id=request_id,
        call_id=call_id,
        call_version=call_version,
    )

    return web.json_response(
        {
            "result": 0,
            "msg": "ok",
            "data": {"type": "answer", "sdp": answer_sdp, "ts": time.time()},
        },
        status=200,
    )
