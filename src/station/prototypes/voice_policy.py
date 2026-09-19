from station.errors import ExternalError

VOICE_REPLY_MODES = frozenset({"voice_only", "all", "off"})
VOICE_DELIVERY_METHODS = frozenset({"voice", "audio"})

def parse_voice_reply_mode(value, *, default="voice_only"):
    mode = (value or "").strip().lower() or default
    if mode not in VOICE_REPLY_MODES:
        raise ExternalError("voice.reply_mode must be voice_only, all, or off")
    return mode

def parse_voice_delivery_method(value, *, default="voice"):
    method = (value or "").strip().lower() or default
    if method not in VOICE_DELIVERY_METHODS:
        raise ExternalError("voice.delivery_method must be voice or audio")
    return method

def should_send_voice_reply(*, reply_mode, incoming_had_audio):
    mode = (reply_mode or "voice_only").strip().lower()
    if mode in {"all", "always"}:
        return True
    if mode in {"off", "none"}:
        return False
    return bool(incoming_had_audio)

def voice_delivery_attachment_type(delivery_method):
    return "voice" if delivery_method == "voice" else "audio"

def voice_delivery_suffix(delivery_method):
    return ".ogg" if delivery_method == "voice" else ".mp3"

def voice_delivery_mime(delivery_method, content_type=""):
    if content_type:
        return content_type
    return "audio/ogg" if delivery_method == "voice" else "audio/mpeg"
