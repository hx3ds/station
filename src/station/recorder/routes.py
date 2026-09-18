from aiohttp import web

from station.api.token import _require_admin
from station.prototypes.boundary import ext_dict, ext_int, ext_str

async def get_recorder_config(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied
    recorder = request.app["recorder"]
    return web.json_response(
        {
            "result": 0,
            "config": {
                "smoothing_window": recorder.smoothing_window,
                "log_level": recorder.get_log_level(),
            },
        }
    )

async def set_recorder_config(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied
    recorder = request.app["recorder"]
    try:
        data = ext_dict("body", await request.json())
    except TypeError:
        return web.json_response({"result": 1, "msg": "body must be object"}, status=400)
    smoothing_window = data.get("smoothing_window")
    if smoothing_window is not None:
        try:
            smoothing_window = ext_int("smoothing_window", smoothing_window)
        except TypeError:
            return web.json_response(
                {"result": 1, "msg": "smoothing_window must be a positive integer"},
                status=400,
            )
        if smoothing_window <= 0:
            return web.json_response(
                {"result": 1, "msg": "smoothing_window must be a positive integer"},
                status=400,
            )
        recorder.smoothing_window = smoothing_window
    return web.json_response({"result": 0, "msg": "Recorder configuration updated"})

async def get_recorder_stats(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied
    recorder = request.app["recorder"]
    return web.json_response({"result": 0, "stats": recorder.path_stats})

async def get_log_level(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied
    recorder = request.app["recorder"]
    return web.json_response({"result": 0, "level": recorder.get_log_level()})

async def set_log_level(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied
    recorder = request.app["recorder"]
    try:
        data = ext_dict("body", await request.json())
    except TypeError:
        return web.json_response({"result": 1, "msg": "body must be object"}, status=400)
    try:
        level = ext_str("level", data.get("level"), default="")
    except TypeError:
        return web.json_response({"result": 1, "msg": "Level is required"}, status=400)
    if not level:
        return web.json_response({"result": 1, "msg": "Level is required"}, status=400)
    try:
        recorder.set_log_level(level)
    except ValueError as e:
        return web.json_response({"result": 1, "msg": str(e)}, status=400)
    return web.json_response({"result": 0, "level": recorder.get_log_level()})

def setup_recorder_routes(app):
    app.router.add_get("/api/recorder/config", get_recorder_config)
    app.router.add_post("/api/recorder/config", set_recorder_config)
    app.router.add_get("/api/recorder/stats", get_recorder_stats)
    app.router.add_get("/api/log_level", get_log_level)
    app.router.add_post("/api/log_level", set_log_level)
