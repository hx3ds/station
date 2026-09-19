import json

from aiohttp import web

from station.errors import ExternalError, InternalError
from station.prototypes.boundary import ext_dict, ext_str


async def read_json_object(request, name="body"):
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise ExternalError("Invalid JSON body")
    return ext_dict(name, data)


def ext_path_int(name, raw):
    text = ext_str(name, raw if raw is not None else "")
    if not text:
        raise ExternalError("Invalid %s" % name)
    try:
        return int(text)
    except ValueError:
        raise ExternalError("Invalid %s" % name)


def external_response(error):
    payload = {"result": 1, "msg": str(error), "data": None}
    if error.reason:
        payload["reason"] = error.reason
    if error.retryable is not None:
        payload["retryable"] = error.retryable
    return web.json_response(payload, status=error.status)


def log_caught(logger, error, *, where):
    if type(error) is ExternalError:
        logger.error("external where=%s error=%s", where, error)
        return
    if type(error) is InternalError:
        logger.error("internal where=%s error=%s", where, error, exc_info=error)
        return
    logger.error("unexpected where=%s error=%s", where, error, exc_info=error)
