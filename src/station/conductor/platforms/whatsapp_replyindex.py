from station.conductor.platforms import guidance

_MAX = 2000
_data = {}
_order = []

def _key(chat_id, message_id):
    return f"{(chat_id or '').strip()}:{(message_id or '').strip()}"

def record_reply_text(chat_id, message_id, text):
    global _order
    chat_id = (chat_id or "").strip()
    message_id = (message_id or "").strip()
    text = guidance.reply_snippet(text)
    if not chat_id or not message_id or not text:
        return
    key = _key(chat_id, message_id)
    if key not in _data:
        _order.append(key)
    _data[key] = text
    while len(_order) > _MAX:
        old = _order.pop(0)
        _data.pop(old, None)

def lookup_reply_text(chat_id, message_id):
    return _data.get(_key(chat_id, message_id), "")
