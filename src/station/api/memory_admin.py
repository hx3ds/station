from aiohttp import web
import json

from station.api.token import _require_admin
from station.prototypes.boundary import ext_dict


async def handle_admin_memory(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied

    model_id = request.match_info.get("model_id")
    if not model_id:
        return web.json_response({"result": 1, "msg": "Missing model_id", "data": None}, status=400)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"result": 1, "msg": "Invalid JSON body", "data": None}, status=400)
    try:
        body = ext_dict("body", body)
    except TypeError:
        return web.json_response({"result": 1, "msg": "Invalid JSON body", "data": None}, status=400)

    instances = request.app["instances"]
    instance_lock = request.app["instance_lock"]
    async with instance_lock:
        instance = instances.get(model_id)
    if instance is None:
        return web.json_response({"result": 1, "msg": "Model instance not found", "data": None}, status=404)
    handler = getattr(instance, "handle_admin_memory", None)
    if not callable(handler) or getattr(instance, "memory", None) is None:
        return web.json_response(
            {"result": 1, "msg": "Model does not support admin memory", "data": None},
            status=400,
        )

    try:
        data = await handler(body)
    except ValueError as exc:
        return web.json_response({"result": 1, "msg": str(exc), "data": None}, status=400)
    except RuntimeError as exc:
        return web.json_response({"result": 1, "msg": str(exc), "data": None}, status=400)
    except Exception:
        return web.json_response({"result": 1, "msg": "Internal error", "data": None}, status=500)
    return web.json_response({"result": 0, "msg": "ok", "data": data})
