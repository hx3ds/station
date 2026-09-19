from aiohttp import web

from station.api.http import read_json_object
from station.api.token import _require_admin
from station.errors import ExternalError
from station.prototypes.boundary import ext_int, ext_str

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
    data = await read_json_object(request)
    smoothing_window = data.get("smoothing_window")
    if smoothing_window is not None:
        smoothing_window = ext_int("smoothing_window", smoothing_window)
        if smoothing_window <= 0:
            raise ExternalError("smoothing_window must be a positive integer")
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
    data = await read_json_object(request)
    level = ext_str("level", data.get("level"), default="")
    if not level:
        raise ExternalError("Level is required")
    recorder.set_log_level(level)
    return web.json_response({"result": 0, "level": recorder.get_log_level()})

def setup_recorder_routes(app):
    app.router.add_get("/api/recorder/config", get_recorder_config)
    app.router.add_post("/api/recorder/config", set_recorder_config)
    app.router.add_get("/api/recorder/stats", get_recorder_stats)
    app.router.add_get("/api/log_level", get_log_level)
    app.router.add_post("/api/log_level", set_log_level)
