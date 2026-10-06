import json
import os

from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_list, ext_str
from station.prototypes.fs_paths import sanitize_path_component, write_atomic

SESSION_NAME = "session.json"
MESSAGES_NAME = "messages.json"
DEFAULT_CONTEXT_TOKENS = 128000
DEFAULT_THRESHOLD = 0.50
DEFAULT_PROTECT_LAST_N = 20
COMPACT_SUMMARY_PREFACE = "Earlier conversation summary:\n"
COMPACT_ACK = "Understood. I will continue with that context."


def chat_dir(storage_dir, acct_id, chat_id):
    return os.path.join(
        storage_dir,
        "chats",
        sanitize_path_component(acct_id, empty="unknown"),
        sanitize_path_component(chat_id, empty="unknown"),
    )


def session_path(storage_dir, acct_id, chat_id):
    return os.path.join(chat_dir(storage_dir, acct_id, chat_id), SESSION_NAME)


def messages_path(storage_dir, acct_id, chat_id):
    return os.path.join(chat_dir(storage_dir, acct_id, chat_id), MESSAGES_NAME)


def load_session_record(storage_dir, acct_id, chat_id):
    path = session_path(storage_dir, acct_id, chat_id)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ExternalError("chat session is not valid JSON")
    return ext_dict("chat session", data)


def save_session_record(storage_dir, acct_id, chat_id, record):
    ext_dict("chat session", record)
    write_atomic(
        session_path(storage_dir, acct_id, chat_id),
        json.dumps(record, indent=2, ensure_ascii=False) + "\n",
        mode=0o600,
    )


def load_session_id(storage_dir, acct_id, chat_id):
    return ext_str("chat session_id", load_session_record(storage_dir, acct_id, chat_id).get("session_id"))


def save_session_id(storage_dir, acct_id, chat_id, session_id, **extra):
    record = dict(extra)
    record["session_id"] = ext_str("chat session_id", session_id)
    if not record["session_id"]:
        raise ExternalError("chat session_id must be non-empty str")
    save_session_record(storage_dir, acct_id, chat_id, record)


def clear_session_record(storage_dir, acct_id, chat_id):
    path = session_path(storage_dir, acct_id, chat_id)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def clear_chat_history(storage_dir, acct_id, chat_id):
    clear_session_record(storage_dir, acct_id, chat_id)
    path = messages_path(storage_dir, acct_id, chat_id)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _content_text(content):
    if content is None:
        return ""
    if type(content) is str:
        return content
    if type(content) is list:
        parts = []
        for item in content:
            if type(item) is str:
                if item.strip():
                    parts.append(item.strip())
                continue
            item = ext_dict("chat message content item", item)
            text = item.get("text")
            if text is None:
                continue
            text = ext_str("chat message content text", text, strip=False)
            if text.strip():
                parts.append(text.strip())
        return "\n".join(parts)
    raise ExternalError("chat message content must be str or list")


def _message_sender(data, *, label, role):
    raw = data.get("sender")
    if raw is None:
        return None
    if role != "user":
        raise ExternalError("%s.sender is only valid on user messages" % label)
    raw = ext_dict("%s.sender" % label, raw)
    allowed = {"user_id", "name", "username"}
    extra = set(raw) - allowed
    if extra:
        raise ExternalError("%s.sender has unexpected keys" % label)
    sender = {}
    for key in ("user_id", "name", "username"):
        if key not in raw or raw[key] is None:
            continue
        text = ext_str("%s.sender.%s" % (label, key), raw[key])
        if text:
            sender[key] = text
    if not sender:
        return None
    return sender


def sender_label(sender):
    if not sender:
        return ""
    name = sender.get("name") or ""
    username = sender.get("username") or ""
    user_id = sender.get("user_id") or ""
    if name and username and name != username:
        return "%s (@%s)" % (name, username)
    if name:
        return name
    if username:
        return username
    return user_id


def history_sender(user_id, sender):
    out = {}
    if user_id:
        out["user_id"] = user_id
    if sender:
        name = sender.get("name") or ""
        username = sender.get("username") or ""
        if name:
            out["name"] = name
        if username:
            out["username"] = username
    if not out:
        return None
    return out


def prefix_content(content, sender):
    label = sender_label(sender)
    if not label:
        return content
    if type(content) is str:
        return "%s: %s" % (label, content)
    prefixed = False
    out = []
    for item in content:
        if (not prefixed) and type(item) is dict and item.get("type") == "text" and type(item.get("text")) is str:
            copied = dict(item)
            copied["text"] = "%s: %s" % (label, item["text"])
            out.append(copied)
            prefixed = True
            continue
        out.append(item)
    if not prefixed:
        out.insert(0, {"type": "text", "text": label})
    return out


def _message_from_dict(data, *, label):
    data = ext_dict(label, data)
    role = ext_str("%s.role" % label, data.get("role"))
    if role not in {"user", "assistant"}:
        raise ExternalError("%s.role must be user or assistant" % label)
    text = _content_text(data.get("content")).strip()
    if not text:
        raise ExternalError("%s.content must be non-empty" % label)
    message = {"role": role, "content": text}
    sender = _message_sender(data, label=label, role=role)
    if sender:
        message["sender"] = sender
    return message


def remember_chat_route(storage_dir, acct_id, chat_id, *, platform="", chat_type=""):
    record = load_session_record(storage_dir, acct_id, chat_id)
    changed = False
    if platform and record.get("platform") != platform:
        record["platform"] = platform
        changed = True
    if chat_type and record.get("chat_type") != chat_type:
        record["chat_type"] = chat_type
        changed = True
    if changed:
        save_session_record(storage_dir, acct_id, chat_id, record)
    return record


def note_chat_opened(storage_dir, acct_id, chat_id, *, platform="", chat_type="", user_id="", sender=None):
    record = remember_chat_route(storage_dir, acct_id, chat_id, platform=platform, chat_type=chat_type)
    record["opened"] = True
    arrived = history_sender(user_id, sender)
    senders = record.get("senders")
    if senders is None:
        senders = []
    else:
        senders = ext_list("senders", senders)
    if arrived and arrived not in senders:
        senders.append(arrived)
        record["senders"] = senders
    save_session_record(storage_dir, acct_id, chat_id, record)
    return record


def _senders(messages, extra=None):
    found = []
    seen = set()
    items = list(messages)
    for sender in extra or []:
        items.append({"sender": sender})
    for item in items:
        sender = item.get("sender")
        if not sender:
            continue
        user_id = sender.get("user_id") or ""
        name = sender.get("name") or ""
        username = sender.get("username") or ""
        key = (user_id, name, username)
        if key in seen or key == ("", "", ""):
            continue
        seen.add(key)
        row = {}
        if user_id:
            row["user_id"] = user_id
        if name:
            row["name"] = name
        if username:
            row["username"] = username
        found.append(row)
    return found


def list_started_chats(storage_dir):
    root = os.path.join(storage_dir, "chats")
    if not os.path.isdir(root):
        return []
    found = []
    for acct_name in sorted(os.listdir(root)):
        acct_path = os.path.join(root, acct_name)
        if not os.path.isdir(acct_path):
            continue
        for chat_name in sorted(os.listdir(acct_path)):
            messages_file = os.path.isfile(os.path.join(acct_path, chat_name, MESSAGES_NAME))
            record = load_session_record(storage_dir, acct_name, chat_name)
            if not messages_file and not record.get("opened"):
                continue
            messages = load_messages(storage_dir, acct_name, chat_name) if messages_file else []
            found.append({
                "acct_id": acct_name,
                "chat_id": chat_name,
                "platform": ext_str("platform", record.get("platform")),
                "chat_type": ext_str("chat_type", record.get("chat_type")),
                "senders": _senders(messages, record.get("senders")),
                "message_count": len(messages),
            })
    return found


def chat_history_view(storage_dir, acct_id, chat_id, *, limit=40):
    record = load_session_record(storage_dir, acct_id, chat_id)
    has_messages = os.path.isfile(messages_path(storage_dir, acct_id, chat_id))
    if not has_messages and not record.get("opened"):
        return None
    messages = load_messages(storage_dir, acct_id, chat_id) if has_messages else []
    tail = messages[-limit:] if len(messages) > limit else messages
    rows = []
    for item in tail:
        row = {"role": item["role"], "content": item["content"]}
        if item.get("sender"):
            row["sender"] = item["sender"]
        rows.append(row)
    return {
        "acct_id": acct_id,
        "chat_id": chat_id,
        "platform": ext_str("platform", record.get("platform")),
        "chat_type": ext_str("chat_type", record.get("chat_type")),
        "senders": _senders(messages, record.get("senders")),
        "messages": rows,
    }


def load_messages(storage_dir, acct_id, chat_id):
    path = messages_path(storage_dir, acct_id, chat_id)
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ExternalError("chat messages are not valid JSON")
    rows = ext_list("chat messages", data)
    return [_message_from_dict(item, label="chat messages[%d]" % index) for index, item in enumerate(rows)]


def save_messages(storage_dir, acct_id, chat_id, messages):
    ext_list("chat messages", messages)
    payload = [_message_from_dict(item, label="chat messages[%d]" % index) for index, item in enumerate(messages)]
    write_atomic(
        messages_path(storage_dir, acct_id, chat_id),
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        mode=0o600,
    )


def estimate_tokens(messages):
    total = 0
    for item in messages:
        total += max(1, len(_content_text(item.get("content"))) // 4)
    return total


def needs_compact(
    messages,
    *,
    context_tokens=DEFAULT_CONTEXT_TOKENS,
    threshold=DEFAULT_THRESHOLD,
    protect_last_n=DEFAULT_PROTECT_LAST_N,
):
    if len(messages) <= protect_last_n:
        return False
    limit = max(1, int(context_tokens * threshold))
    return estimate_tokens(messages) > limit


def split_for_compact(messages, *, protect_last_n=DEFAULT_PROTECT_LAST_N):
    if len(messages) <= protect_last_n:
        return [], list(messages)
    return list(messages[:-protect_last_n]), list(messages[-protect_last_n:])


def format_transcript(messages):
    lines = []
    for item in messages:
        role = ext_str("chat message role", item.get("role"))
        text = _content_text(item.get("content")).strip()
        if not text:
            continue
        if role == "user":
            label = sender_label(item.get("sender"))
            if label:
                lines.append("%s: %s" % (label, text))
                continue
        lines.append("%s: %s" % (role, text))
    return "\n\n".join(lines)


def apply_compact_summary(summary, tail):
    text = ext_str("compact summary", summary).strip()
    if not text:
        return list(tail)
    prefix = [{"role": "user", "content": COMPACT_SUMMARY_PREFACE + text}]
    if tail and tail[0].get("role") == "user":
        prefix.append({"role": "assistant", "content": COMPACT_ACK})
    return prefix + list(tail)
