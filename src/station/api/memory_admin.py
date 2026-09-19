from station.api.http import read_json_object
from station.api.token import _require_admin
from station.errors import ExternalError
from aiohttp import web


async def handle_admin_memory(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied

    model_id = request.match_info.get("model_id")
    if not model_id:
        raise ExternalError("Missing model_id")

    body = await read_json_object(request)
    instances = request.app["instances"]
    instance_lock = request.app["instance_lock"]
    async with instance_lock:
        instance = instances.get(model_id)
    if instance is None:
        raise ExternalError("Model instance not found", status=404)
    data = await instance.handle_admin_memory(body)
    return web.json_response({"result": 0, "msg": "ok", "data": data})
