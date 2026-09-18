from station.conductor.platforms.guidance import filter_oversized_attachments
from station.prototypes.attachments import ATTACHMENT_TYPE_METHOD_FIELD

NATIVE_KINDS = ("photo", "video", "audio", "document", "voice", "animation")
STICKER_NATIVE_KINDS = NATIVE_KINDS + ("sticker",)

_DEFAULT = {
    "reuse_file_id": False,
    "reuse_http_url": False,
    "reuse_mxc": False,
    "download_http_url": False,
    "native_kinds": NATIVE_KINDS,
    "degrade": {"sticker": "photo"},
    "caption_with_media": True,
    "media_chat_types": None,
    "reply_to": True,
}

PLATFORMS = {
    "telegram": {
        "reuse_file_id": True,
        "reuse_http_url": False,
        "reuse_mxc": False,
        "download_http_url": False,
        "native_kinds": STICKER_NATIVE_KINDS,
        "degrade": {},
        "caption_with_media": True,
        "media_chat_types": None,
        "reply_to": True,
    },
    "discord": {
        "reuse_file_id": False,
        "reuse_http_url": False,
        "reuse_mxc": False,
        "download_http_url": True,
        "native_kinds": NATIVE_KINDS,
        "degrade": {"sticker": "photo"},
        "caption_with_media": True,
        "media_chat_types": None,
        "reply_to": True,
    },
    "matrix": {
        "reuse_file_id": False,
        "reuse_http_url": False,
        "reuse_mxc": True,
        "download_http_url": False,
        "native_kinds": NATIVE_KINDS,
        "degrade": {"sticker": "photo"},
        "caption_with_media": True,
        "media_chat_types": None,
        "reply_to": True,
    },
    "qq": {
        "reuse_file_id": False,
        "reuse_http_url": False,
        "reuse_mxc": False,
        "download_http_url": True,
        "native_kinds": NATIVE_KINDS,
        "degrade": {"sticker": "photo"},
        "caption_with_media": True,
        "media_chat_types": ("c2c", "group"),
        "reply_to": True,
    },
    "whatsapp": {
        "reuse_file_id": False,
        "reuse_http_url": False,
        "reuse_mxc": False,
        "download_http_url": False,
        "native_kinds": NATIVE_KINDS,
        "degrade": {"sticker": "photo"},
        "caption_with_media": True,
        "media_chat_types": None,
        "reply_to": True,
    },
    "whatsapp_cloud": {
        "reuse_file_id": True,
        "reuse_http_url": True,
        "reuse_mxc": False,
        "download_http_url": False,
        "native_kinds": STICKER_NATIVE_KINDS,
        "degrade": {"animation": "video"},
        "caption_with_media": True,
        "media_chat_types": None,
        "reply_to": True,
    },
}

def normalize_platform(value):
    p = (value or "").strip().lower()
    if p.startswith("qr:"):
        p = p[3:]
    return p

def get(platform):
    row = PLATFORMS.get(normalize_platform(platform))
    if row:
        return row
    return _DEFAULT

def allows_media(platform, chat_type=""):
    allowed = get(platform).get("media_chat_types")
    if allowed is None:
        return True
    ct = (chat_type or "").strip().lower()
    if not ct:
        return True
    return ct in allowed

def media_ref_action(platform, ref):
    ref = (ref or "").strip()
    if not ref:
        return ""
    row = get(platform)
    if ref.startswith("mxc://"):
        return "reuse" if row.get("reuse_mxc") else "reject"
    if ref.startswith("http://") or ref.startswith("https://"):
        if row.get("reuse_http_url"):
            return "reuse"
        if row.get("download_http_url"):
            return "download"
        return "reject"
    if ref.isdigit() and normalize_platform(platform) == "telegram":
        return "reject"
    return "reuse" if row.get("reuse_file_id") else "reject"

def can_reuse_ref(platform, ref):
    return media_ref_action(platform, ref) == "reuse"

def native_kind(platform, att_type):
    kind = (att_type or "").strip().lower() or "document"
    row = get(platform)
    kinds = row.get("native_kinds") or ()
    if kind in kinds:
        return kind
    degrade = row.get("degrade") or {}
    mapped = degrade.get(kind)
    if mapped in kinds:
        return mapped
    return ""

def method_field(kind):
    spec = ATTACHMENT_TYPE_METHOD_FIELD.get(kind)
    if not spec:
        return "", ""
    return spec

def fallback_text(att, caption=""):
    kind = (att.get("type") or "file").strip() or "file"
    name = (att.get("file_name") or "").strip()
    if name:
        note = "[%s: %s]" % (kind, name)
    else:
        note = "[%s]" % kind
    cap = (caption or "").strip()
    if cap:
        return cap + "\n" + note
    return note

def _text_step(text, reply_to="", keyboard=None):
    params = {"text": text}
    if reply_to:
        params["reply_to"] = reply_to
    if keyboard is not None:
        params["keyboard"] = keyboard
    return {"method": "send_message", "params": params, "files": None, "download": None, "reused": False}

def _media_step(platform, att, kind, caption, reply_to):
    method, field = method_field(kind)
    if not method:
        return _text_step(fallback_text(att, caption), reply_to)
    params = {}
    if kind != "sticker" and get(platform).get("caption_with_media"):
        params["caption"] = caption or ""
    elif kind != "sticker" and caption:
        params["caption"] = caption
    if reply_to:
        params["reply_to"] = reply_to
    remote = (att.get("remote_id") or "").strip()
    fid = (att.get("file_id") or "").strip() or None
    path = (att.get("local_path") or "").strip()
    if remote and can_reuse_ref(platform, remote):
        params[field] = remote
        return {"method": method, "params": params, "files": None, "download": fid, "reused": True, "field": field}
    if path:
        return {"method": method, "params": params, "files": {field: path}, "download": None, "reused": False, "field": field}
    if fid:
        return {"method": method, "params": params, "files": None, "download": fid, "reused": False, "field": field}
    return _text_step(fallback_text(att, caption), reply_to)

def plan_outbound(platform, text="", attachments=None, reply_to="", chat_type="", keyboard=None):
    row = get(platform)
    text = text or ""
    reply = (reply_to or "").strip() if row.get("reply_to") else ""
    kept, size_notes = filter_oversized_attachments(normalize_platform(platform), attachments)
    notes = list(size_notes or [])
    media_atts = []
    if allows_media(platform, chat_type):
        for att in kept:
            kind = native_kind(platform, att.get("type"))
            if not kind:
                notes.append(fallback_text(att, ""))
                continue
            media_atts.append((att, kind))
    else:
        for att in kept:
            notes.append(fallback_text(att, ""))

    steps = []
    caption_used = False
    for att, kind in media_atts:
        cap = ""
        use_reply = reply if not steps else ""
        if kind != "sticker" and row.get("caption_with_media") and not caption_used and text:
            cap = text
            caption_used = True
        steps.append(_media_step(platform, att, kind, cap, use_reply))

    extra = ""
    if not caption_used:
        extra = text
    if notes:
        joined = "\n".join(notes)
        extra = (extra + "\n" + joined).strip() if extra else joined
    if extra or keyboard is not None:
        use_reply = reply if not steps else ""
        steps.append(_text_step(extra, use_reply, keyboard=keyboard))
    return steps
