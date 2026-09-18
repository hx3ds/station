import json
from aiohttp import web

from station.api.instances import isolate_instance, note_instance_ok, note_instance_unexpected, should_isolate
from station.api.tenant_auth import authorize_request
from station.prototypes.boundary import ext_dict, ext_int, ext_str
from station import logger

async def _apply(*, app, instance, model_id, apply):
    try:
        await apply()
    except ValueError:
        await isolate_instance(app, model_id, instance)
        raise
    except Exception as e:
        logger.error("unexpected where=model apply model_id=%s error=%s", model_id, e, exc_info=e)
        count = note_instance_unexpected(app, model_id)
        if should_isolate(count, app):
            await isolate_instance(app, model_id, instance)
        raise
    note_instance_ok(app, model_id)

def _decrypt_model_settings(app, raw_settings):
    if "__enc__" not in raw_settings:
        return raw_settings
    enc = ext_str("__enc__", raw_settings["__enc__"], default="", strip=False)
    if not enc.strip():
        return raw_settings
    lc = app["local_conductor"]
    if lc is None:
        raise ValueError("encrypted settings require local conductor")
    decoded = lc.decrypt_settings(raw_settings)
    if decoded is None:
        raise ValueError("Model settings decrypt failed")
    return decoded

async def handle_reload_model(request):
    app = request.app
    db = app["db"]
    model_id = request.match_info.get("model_id")
    if not model_id:
        return web.json_response({"result": 1, "msg": "Missing model_id", "data": None}, status=400)

    tenant = authorize_request(request)
    if tenant is None:
        return web.json_response({"result": 1, "msg": "Unauthorized", "data": None}, status=401)

    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"result": 1, "msg": "Invalid JSON body", "data": None}, status=400)
    try:
        data = ext_dict("body", data)
    except TypeError:
        return web.json_response({"result": 1, "msg": "Invalid JSON body", "data": None}, status=400)

    if "settings" not in data:
        return web.json_response({"result": 1, "msg": "Missing settings", "data": None}, status=400)

    request_prototype_id = data.get("prototype_id")
    if request_prototype_id is None:
        data["prototype_id"] = tenant.id
    else:
        try:
            request_prototype_id = ext_int("prototype_id", request_prototype_id)
        except TypeError:
            return web.json_response({"result": 1, "msg": "Invalid prototype_id", "data": None}, status=400)
        if request_prototype_id != tenant.id:
            return web.json_response({"result": 1, "msg": "Unauthorized (Wrong Prototype ID)", "data": None}, status=401)

    try:
        raw_settings = ext_dict("settings", data["settings"])
        raw_settings = _decrypt_model_settings(app, raw_settings)
    except (ValueError, TypeError) as e:
        return web.json_response({"result": 1, "msg": str(e), "data": None}, status=400)
    data["settings"] = raw_settings

    instances = app["instances"]
    instance_lock = app["instance_lock"]
    async with instance_lock:
        instance = instances.get(model_id)

    if instance:
        if not await db.ensure_model_lock(app, model_id):
            return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)
    else:
        if not await db.try_model_lock_once(app, model_id):
            return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)

    await db.put_model(model_id, data)

    if instance:
        try:
            await _apply(
                app=app,
                instance=instance,
                model_id=model_id,
                apply=lambda: instance.reload(model_settings=raw_settings),
            )
        except ValueError as e:
            return web.json_response({"result": 1, "msg": str(e), "data": None}, status=400)

    return web.json_response({"result": 0, "msg": "Model reloaded", "data": None})

async def handle_remove_model(request):
    app = request.app
    db = app["db"]
    model_id = request.match_info.get("model_id")
    if not model_id:
        return web.json_response({"result": 1, "msg": "Missing model_id", "data": None}, status=400)

    tenant = authorize_request(request)
    if tenant is None:
        return web.json_response({"result": 1, "msg": "Unauthorized", "data": None}, status=401)

    if not await db.ensure_model_lock(app, model_id):
        return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)

    await db.delete_model(model_id)
    instances = app["instances"]
    instance_lock = app["instance_lock"]
    async with instance_lock:
        instance = instances.pop(model_id, None)
        app["instance_runtime"].pop(model_id, None)
    if instance:
        try:
            await instance.stop()
        except Exception as e:
            logger.error("unexpected where=remove_model stop model_id=%s error=%s", model_id, e, exc_info=e)

    await db.release_model_lock_from_app(app, model_id)
    return web.json_response({"result": 0, "msg": "command succeeded", "data": None})

async def handle_start_model(request):
    app = request.app
    db = app["db"]

    model_id = request.match_info.get("model_id")
    if not model_id:
        return web.json_response({"result": 1, "msg": "Missing model_id", "data": None}, status=400)

    tenant = authorize_request(request)
    if tenant is None:
        return web.json_response({"result": 1, "msg": "Unauthorized", "data": None}, status=401)

    instances = app["instances"]
    instance_lock = app["instance_lock"]

    async with instance_lock:
        instance = instances.get(model_id)
    if not instance:
        if not await db.try_model_lock_once(app, model_id):
            return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)
        return web.json_response({"result": 1, "msg": "Model instance not found", "data": None}, status=404)
    if not await db.ensure_model_lock(app, model_id):
        return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)

    async with instance_lock:
        instance = instances.get(model_id)
        if not instance:
            return web.json_response({"result": 1, "msg": "Model instance not found", "data": None}, status=404)
        try:
            await instance.start()
        except ValueError as e:
            await isolate_instance(app, model_id, instance)
            return web.json_response({"result": 1, "msg": str(e), "data": None}, status=400)
        except Exception as e:
            logger.error("unexpected where=start_model model_id=%s error=%s", model_id, e, exc_info=e)
            await isolate_instance(app, model_id, instance)
            raise

    return web.json_response({"result": 0, "msg": "Model started", "data": None})

async def handle_stop_model(request):
    app = request.app
    db = app["db"]

    model_id = request.match_info.get("model_id")
    if not model_id:
        return web.json_response({"result": 1, "msg": "Missing model_id", "data": None}, status=400)

    tenant = authorize_request(request)
    if tenant is None:
        return web.json_response({"result": 1, "msg": "Unauthorized", "data": None}, status=401)

    instances = app["instances"]
    instance_lock = app["instance_lock"]

    async with instance_lock:
        instance = instances.get(model_id)
    if not instance:
        if not await db.try_model_lock_once(app, model_id):
            return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)
        return web.json_response({"result": 1, "msg": "Model instance not found", "data": None}, status=404)
    if not await db.ensure_model_lock(app, model_id):
        return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)

    async with instance_lock:
        instance = instances.get(model_id)
        if not instance:
            return web.json_response({"result": 1, "msg": "Model instance not found", "data": None}, status=404)
        try:
            await instance.stop()
        except Exception as e:
            logger.error("unexpected where=stop_model model_id=%s error=%s", model_id, e, exc_info=e)
            raise

    return web.json_response({"result": 0, "msg": "Model stopped", "data": None})

async def handle_pause_model(request):
    app = request.app
    db = app["db"]

    model_id = request.match_info.get("model_id")
    if not model_id:
        return web.json_response({"result": 1, "msg": "Missing model_id", "data": None}, status=400)

    tenant = authorize_request(request)
    if tenant is None:
        return web.json_response({"result": 1, "msg": "Unauthorized", "data": None}, status=401)

    instances = app["instances"]
    instance_lock = app["instance_lock"]

    async with instance_lock:
        instance = instances.get(model_id)
    if not instance:
        if not await db.try_model_lock_once(app, model_id):
            return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)
        return web.json_response({"result": 1, "msg": "Model instance not found", "data": None}, status=404)
    if not await db.ensure_model_lock(app, model_id):
        return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)

    async with instance_lock:
        instance = instances.get(model_id)
        if not instance:
            return web.json_response({"result": 1, "msg": "Model instance not found", "data": None}, status=404)
        return await instance.handle_pause(request)

async def handle_resume_model(request):
    app = request.app
    db = app["db"]

    model_id = request.match_info.get("model_id")
    if not model_id:
        return web.json_response({"result": 1, "msg": "Missing model_id", "data": None}, status=400)

    tenant = authorize_request(request)
    if tenant is None:
        return web.json_response({"result": 1, "msg": "Unauthorized", "data": None}, status=401)

    instances = app["instances"]
    instance_lock = app["instance_lock"]

    async with instance_lock:
        instance = instances.get(model_id)
    if not instance:
        if not await db.try_model_lock_once(app, model_id):
            return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)
        return web.json_response({"result": 1, "msg": "Model instance not found", "data": None}, status=404)
    if not await db.ensure_model_lock(app, model_id):
        return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)

    async with instance_lock:
        instance = instances.get(model_id)
        if not instance:
            return web.json_response({"result": 1, "msg": "Model instance not found", "data": None}, status=404)
        return await instance.handle_resume(request)

async def handle_rewind_model(request):
    app = request.app
    db = app["db"]

    model_id = request.match_info.get("model_id")
    if not model_id:
        return web.json_response({"result": 1, "msg": "Missing model_id", "data": None}, status=400)

    tenant = authorize_request(request)
    if tenant is None:
        return web.json_response({"result": 1, "msg": "Unauthorized", "data": None}, status=401)

    instances = app["instances"]
    instance_lock = app["instance_lock"]

    async with instance_lock:
        instance = instances.get(model_id)
    if not instance:
        if not await db.try_model_lock_once(app, model_id):
            return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)
        return web.json_response({"result": 1, "msg": "Model instance not found", "data": None}, status=404)
    if not await db.ensure_model_lock(app, model_id):
        return web.json_response({"result": 1, "msg": "Model is active on another station", "data": None}, status=503)

    async with instance_lock:
        instance = instances.get(model_id)
        if not instance:
            return web.json_response({"result": 1, "msg": "Model instance not found", "data": None}, status=404)
        return await instance.handle_rewind(request)
