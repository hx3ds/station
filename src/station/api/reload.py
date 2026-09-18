from aiohttp import web

from station.api.token import _require_admin
from station.config.reload import apply_reloaded_config, reload_from_disk

async def handle_admin_reload_config(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied

    app = request.app
    current = app["config"]

    result = reload_from_disk(current)
    apply_reloaded_config(app, result)

    return web.json_response(
        {
            "result": 0,
            "msg": "ok",
            "data": {
                "applied": result["applied"],
                "pending_restart": result["pending_restart"],
                "unchanged": result["unchanged"],
            },
        },
        status=200,
    )
