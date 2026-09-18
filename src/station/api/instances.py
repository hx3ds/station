from station import logger

def create_tenant_instance(app, tenant, *, prototype_id, model_id, model_settings=None):
    cls = tenant.prototype_class
    instance = cls(
        app,
        prototype_id,
        model_id,
        model_settings=model_settings,
        config_file=tenant.config_file,
        secret_file=tenant.secret_file,
    )
    if tenant.client_context is not None:
        instance.client_context = tenant.client_context
    return instance

def mark_needs_manual(app, where):
    app["needs_manual"] = True
    logger.critical("needs_manual handle=administrator where=%s", where)

def note_instance_ok(app, model_id):
    state = app["instance_runtime"].get(model_id)
    if state is not None:
        state["unexpected"] = 0

def note_instance_unexpected(app, model_id):
    runtime = app["instance_runtime"]
    state = runtime.get(model_id)
    if state is None:
        state = {"unexpected": 0}
        runtime[model_id] = state
    state["unexpected"] = state["unexpected"] + 1
    return state["unexpected"]

def should_isolate(count, app):
    return count >= app["config"].server.isolate_after

async def isolate_instance(app, model_id, instance=None):
    instances = app["instances"]
    instance_lock = app["instance_lock"]
    db = app["db"]
    async with instance_lock:
        current = instances.pop(model_id, None)
        app["instance_runtime"].pop(model_id, None)
    target = current or instance
    if target is None:
        return True
    try:
        await target.stop()
    except Exception as e:
        logger.error("unexpected where=instance isolate stop model_id=%s error=%s", model_id, e, exc_info=e)
        mark_needs_manual(app, "instance model_id=%s" % model_id)
        try:
            await db.release_model_lock_from_app(app, model_id)
        except Exception as inner:
            logger.error(
                "unexpected where=instance isolate unlock model_id=%s error=%s",
                model_id,
                inner,
                exc_info=inner,
            )
        return False
    try:
        await db.release_model_lock_from_app(app, model_id)
    except Exception as e:
        logger.error(
            "unexpected where=instance isolate unlock model_id=%s error=%s",
            model_id,
            e,
            exc_info=e,
        )
    logger.error("instance isolated model_id=%s", model_id)
    return True
