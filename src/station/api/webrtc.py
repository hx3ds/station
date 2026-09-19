import time
from aiohttp import web

from station.api.http import read_json_object
from station.api.tenant_auth import authorize_request
from station.errors import ExternalError
from station.prototypes.boundary import ext_int, ext_str

async def handle_webrtc_offer(request):
    app = request.app

    model_id = request.match_info.get("model_id")
    if not model_id:
        raise ExternalError("Missing model_id")

    tenant = authorize_request(request)
    if tenant is None:
        raise ExternalError("Unauthorized", status=401)

    db = app["db"]
    info = await db.get_prototype_info(tenant.id)
    if info is None:
        raise ExternalError("Prototype does not support calls", status=403)
    call_support = info.get("call_support")
    if call_support is not True:
        raise ExternalError("Prototype does not support calls", status=403)

    body = await read_json_object(request)
    sdp = ext_str("sdp", body.get("sdp"), default="", strip=False)
    if not sdp.strip():
        raise ExternalError("Missing sdp")

    webrtc = app["webrtc"]
    if webrtc is None:
        raise ExternalError("WebRTC not enabled", status=501)

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
        call_id = ext_str("call_id", call_id, default="", strip=False)
    call_version = body.get("version")
    if call_version is not None:
        call_version = ext_int("version", call_version)

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
