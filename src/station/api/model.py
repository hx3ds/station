from aiohttp import web

from station.api.http import log_caught, read_json_object
from station.api.instances import isolate_instance, note_instance_ok, note_instance_unexpected, should_isolate
from station.api.tenant_auth import authorize_request
from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_int, ext_str
from station import logger


async def _apply(*, app, instance, model_id, apply):
    try:
        await apply()
    except ExternalError:
        await isolate_instance(app, model_id, instance)
        raise
    except Exception as e:
        log_caught(logger, e, where="model apply model_id=%s" % model_id)
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
        raise ExternalError("encrypted settings require local conductor")
    decoded = lc.decrypt_settings(raw_settings)
    if decoded is None:
        raise ExternalError("Model settings decrypt failed")
    return decoded


def _require_model_id(request):
    model_id = request.match_info.get("model_id")
    if not model_id:
        raise ExternalError("Missing model_id")
    tenant = authorize_request(request)
    if tenant is None:
        raise ExternalError("Unauthorized", status=401)
    return request.app, model_id, tenant


async def _locked_instance(request, *, require_existing=True):
    app, model_id, tenant = _require_model_id(request)
    db = app["db"]
    instances = app["instances"]
    instance_lock = app["instance_lock"]
    async with instance_lock:
        instance = instances.get(model_id)
    if not instance:
        if not await db.try_model_lock_once(app, model_id):
            raise ExternalError("Model is active on another station", status=503)
        if require_existing:
            raise ExternalError("Model instance not found", status=404)
        return app, db, model_id, tenant, None, instance_lock, instances
    if not await db.ensure_model_lock(app, model_id):
        raise ExternalError("Model is active on another station", status=503)
    return app, db, model_id, tenant, instance, instance_lock, instances


async def handle_reload_model(request):
    app, model_id, tenant = _require_model_id(request)
    db = app["db"]
    data = await read_json_object(request)
    if "settings" not in data:
        raise ExternalError("Missing settings")
    request_prototype_id = data.get("prototype_id")
    if request_prototype_id is None:
        data["prototype_id"] = tenant.id
    else:
        request_prototype_id = ext_int("prototype_id", request_prototype_id)
        if request_prototype_id != tenant.id:
            raise ExternalError("Unauthorized (Wrong Prototype ID)", status=401)
    raw_settings = ext_dict("settings", data["settings"])
    raw_settings = _decrypt_model_settings(app, raw_settings)
    data["settings"] = raw_settings
    instances = app["instances"]
    instance_lock = app["instance_lock"]
    async with instance_lock:
        instance = instances.get(model_id)
    if instance:
        if not await db.ensure_model_lock(app, model_id):
            raise ExternalError("Model is active on another station", status=503)
    else:
        if not await db.try_model_lock_once(app, model_id):
            raise ExternalError("Model is active on another station", status=503)
    await db.put_model(model_id, data)
    if instance:
        await _apply(
            app=app,
            instance=instance,
            model_id=model_id,
            apply=lambda: instance.reload(model_settings=raw_settings),
        )
    return web.json_response({"result": 0, "msg": "Model reloaded", "data": None})


async def handle_remove_model(request):
    app, model_id, _tenant = _require_model_id(request)
    db = app["db"]
    if not await db.ensure_model_lock(app, model_id):
        raise ExternalError("Model is active on another station", status=503)
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
            log_caught(logger, e, where="remove_model stop model_id=%s" % model_id)
    await db.release_model_lock_from_app(app, model_id)
    return web.json_response({"result": 0, "msg": "command succeeded", "data": None})


async def handle_start_model(request):
    app, db, model_id, _tenant, _instance, instance_lock, instances = await _locked_instance(request)
    async with instance_lock:
        instance = instances.get(model_id)
        if not instance:
            raise ExternalError("Model instance not found", status=404)
        try:
            await instance.start()
        except ExternalError:
            await isolate_instance(app, model_id, instance)
            raise
        except Exception as e:
            log_caught(logger, e, where="start_model model_id=%s" % model_id)
            await isolate_instance(app, model_id, instance)
            raise
    return web.json_response({"result": 0, "msg": "Model started", "data": None})


async def handle_stop_model(request):
    app, db, model_id, _tenant, _instance, instance_lock, instances = await _locked_instance(request)
    async with instance_lock:
        instance = instances.get(model_id)
        if not instance:
            raise ExternalError("Model instance not found", status=404)
        try:
            await instance.stop()
        except Exception as e:
            log_caught(logger, e, where="stop_model model_id=%s" % model_id)
            raise
    return web.json_response({"result": 0, "msg": "Model stopped", "data": None})


async def _call_locked(request, call):
    _app, _db, model_id, _tenant, _instance, instance_lock, instances = await _locked_instance(request)
    async with instance_lock:
        instance = instances.get(model_id)
        if not instance:
            raise ExternalError("Model instance not found", status=404)
        return await call(instance, request)


async def handle_pause_model(request):
    return await _call_locked(request, lambda instance, req: instance.handle_pause(req))


async def handle_resume_model(request):
    return await _call_locked(request, lambda instance, req: instance.handle_resume(req))


async def handle_rewind_model(request):
    return await _call_locked(request, lambda instance, req: instance.handle_rewind(req))
