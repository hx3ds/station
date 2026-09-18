from aiohttp import web
from .reception import handle_reception, handle_event
from .webrtc import handle_webrtc_offer
from .token import handle_admin_update_prototype_token, handle_admin_add_prototype
from .reload import handle_admin_reload_config
from .memory_admin import handle_admin_memory
from station import hopmetrics
from .model import (
    handle_reload_model,
    handle_remove_model,
    handle_start_model,
    handle_stop_model,
    handle_pause_model,
    handle_resume_model,
    handle_rewind_model,
)

async def handle_metrics(request):
    body = hopmetrics.render_prometheus("station")
    return web.Response(text=body, content_type="text/plain; version=0.0.4")

async def handle_hopmetrics(request):
    return web.json_response({"hops": hopmetrics.snapshot()})

class Routes:
    def __init__(self, app):
        app.router.add_get("/metrics", handle_metrics)
        app.router.add_get("/hopmetrics", handle_hopmetrics)
        app.router.add_post("/reception/{model_id}", handle_reception)
        app.router.add_post("/event/{model_id}", handle_event)
        app.router.add_post("/webrtc/offer/{model_id}", handle_webrtc_offer)
        app.router.add_post("/station/reload_model/{model_id}", handle_reload_model)
        app.router.add_post("/station/remove_model/{model_id}", handle_remove_model)
        app.router.add_post("/station/start_model/{model_id}", handle_start_model)
        app.router.add_post("/station/stop_model/{model_id}", handle_stop_model)
        app.router.add_post("/station/pause_model/{model_id}", handle_pause_model)
        app.router.add_post("/station/resume_model/{model_id}", handle_resume_model)
        app.router.add_post("/station/rewind_model/{model_id}", handle_rewind_model)
        app.router.add_post("/admin/prototype_token", handle_admin_update_prototype_token)
        app.router.add_post("/admin/prototype", handle_admin_add_prototype)
        app.router.add_post("/admin/reload_config", handle_admin_reload_config)
        app.router.add_post("/admin/memory/{model_id}", handle_admin_memory)
