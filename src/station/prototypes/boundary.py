from station.errors import ExternalError


def ext_require(name, value, expected, *, allow_none=False):
    if value is None:
        if allow_none:
            return None
        raise ExternalError("%s is required" % name)
    if isinstance(value, bool) and int in expected and bool not in expected:
        raise ExternalError("%s must be int, got bool" % name)
    if not isinstance(value, expected):
        names = ", ".join(t.__name__ for t in expected)
        raise ExternalError("%s must be %s, got %s" % (name, names, type(value).__name__))
    return value


def ext_str(name, value, *, default="", strip=True):
    if value is None:
        return default
    text = ext_require(name, value, (str,))
    return text.strip() if strip else text


def ext_dict(name, value, *, allow_none=False):
    if value is None:
        if allow_none:
            return None
        raise ExternalError("%s is required" % name)
    return ext_require(name, value, (dict,))


def ext_list(name, value, *, allow_none=False, default=None):
    if value is None:
        if allow_none:
            return default if default is not None else []
        raise ExternalError("%s is required" % name)
    return ext_require(name, value, (list,))


def ext_bool(name, value, *, default=False):
    if value is None:
        return default
    return ext_require(name, value, (bool,))


def ext_int(name, value, *, default=None, allow_none=False):
    if value is None:
        if allow_none:
            return default
        raise ExternalError("%s is required" % name)
    return ext_require(name, value, (int,))


def ext_float(name, value, *, default=None, allow_none=False):
    if value is None:
        if allow_none:
            return default
        raise ExternalError("%s is required" % name)
    if isinstance(value, bool):
        raise ExternalError("%s must be float, got bool" % name)
    return ext_require(name, value, (int, float))


def ext_optional_id(name, value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        raise ExternalError("%s must be str|int, got bool" % name)
    if isinstance(value, (str, int)):
        return value
    raise ExternalError("%s must be str|int, got %s" % (name, type(value).__name__))


def parse_env_int(key, value, *, allow_none=False, default=None):
    if value is None:
        if allow_none:
            return default
        raise ExternalError("%s is required" % key)
    text = value.strip()
    if text == "":
        if allow_none:
            return default
        raise ExternalError("%s is required" % key)
    try:
        return int(text, 10)
    except ValueError:
        raise ExternalError("%s must be int, got str" % key)


def parse_env_float(key, value, *, allow_none=False, default=None):
    if value is None:
        if allow_none:
            return default
        raise ExternalError("%s is required" % key)
    text = value.strip()
    if text == "":
        if allow_none:
            return default
        raise ExternalError("%s is required" % key)
    try:
        return float(text)
    except ValueError:
        raise ExternalError("%s must be float, got str" % key)


def ext_result_dict(name, value):
    if value is None:
        return {}
    return ext_dict(name, value)


def ext_mapping_get(mapping, key, expected, default=None, *, allow_none=False):
    if key not in mapping:
        return default
    value = mapping[key]
    if value is None and allow_none:
        return None
    return ext_require(key, value, expected, allow_none=allow_none)


def validate_inbound_data(data, *, label="inbound data"):
    return ext_dict(label, data)


def validate_inbound_message_fields(data):
    text = ext_str("text", data.get("text"))
    caption = ext_str("caption", data.get("caption"))
    msg_id = ext_optional_id("msg_id", data.get("msg_id"))
    reply_to = ext_optional_id("reply_to", data.get("reply_to"))
    platform = ext_str("platform", data.get("platform"))
    chat_type = ext_str("chat_type", data.get("chat_type"))
    attachments = ext_list("attachments", data.get("attachments"), allow_none=True, default=[])
    for index, att in enumerate(attachments):
        ext_dict("attachments[%d]" % index, att)
    is_expired = data.get("is_expired")
    if is_expired is not None:
        ext_require("is_expired", is_expired, (bool,))
    return {
        "text": text,
        "caption": caption,
        "msg_id": msg_id,
        "reply_to": reply_to,
        "platform": platform,
        "chat_type": chat_type,
        "attachments": attachments,
        "is_expired": False if is_expired is None else is_expired,
        "inbound_text": (text.strip() or caption.strip()),
    }


def validate_attachment(attachment, *, label="attachment"):
    att = ext_dict(label, attachment)
    for key in (
        "file_id",
        "file_name",
        "filename",
        "name",
        "title",
        "type",
        "content_type",
        "mime_type",
        "local_path",
        "workspace_path",
        "url",
    ):
        if key in att and att[key] is not None:
            ext_require("%s.%s" % (label, key), att[key], (str,))
    return att


def validate_attachments(value, *, label="attachments"):
    if value is None:
        return []
    items = ext_list(label, value)
    return [validate_attachment(item, label="%s[%d]" % (label, i)) for i, item in enumerate(items)]
