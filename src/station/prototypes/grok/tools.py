import json

from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_list, ext_str
from station.prototypes.chat_history import chat_history_view, list_started_chats, load_messages, save_messages

CLIENT_TOOL_NAMES = frozenset({"list_chats", "get_chat_history", "send_message"})

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_chats",
            "description": "List chats already started with this model. Use when the user asks you to contact someone and refers to them by display name or first name. Each row has acct_id, chat_id, and senders.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chat_history",
            "description": "Read one started chat and match a person by display name or first name and by how they described themselves. Senders include the user_id to pass to send_message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "acct_id": {"type": "string"},
                    "chat_id": {"type": "string"},
                },
                "required": ["acct_id", "chat_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "Send text and optional files into a different chat that has already started, after get_chat_history matches the person. user_id must be a sender user_id from that chat. files are Station file_ids or local paths. Station delivers them with send_message methods. This does not reply in the current chat and cannot start a chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "acct_id": {"type": "string"},
                    "chat_id": {"type": "string"},
                    "user_id": {"type": "string"},
                    "text": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["acct_id", "chat_id", "user_id", "text"],
            },
        },
    },
]


def parse_tool_calls(message):
    raw = message.get("tool_calls")
    if raw is None:
        return []
    calls = ext_list("tool_calls", raw)
    parsed = []
    for index, item in enumerate(calls):
        item = ext_dict("tool_calls[%d]" % index, item)
        call_id = ext_str("tool_calls[%d].id" % index, item.get("id"))
        if not call_id:
            raise ExternalError("tool_calls[%d].id is required" % index)
        function = item.get("function")
        function = ext_dict("tool_calls[%d].function" % index, function)
        name = ext_str("tool_calls[%d].function.name" % index, function.get("name"))
        if not name:
            raise ExternalError("tool_calls[%d].function.name is required" % index)
        arguments = function.get("arguments")
        if arguments is None or arguments == "":
            arguments = {}
        if type(arguments) is str:
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                raise ExternalError("tool_calls[%d].function.arguments is not JSON" % index)
        arguments = ext_dict("tool_calls[%d].function.arguments" % index, arguments)
        parsed.append({
            "id": call_id,
            "name": name,
            "arguments": arguments,
            "api": {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            },
        })
    return parsed


def _required(arguments, key):
    if key not in arguments:
        raise ExternalError("%s is required" % key)
    text = ext_str(key, arguments.get(key))
    if not text:
        raise ExternalError("%s is required" % key)
    return text


def _optional_files(arguments):
    raw = arguments.get("files")
    if raw is None:
        return []
    items = ext_list("files", raw)
    files = []
    for index, item in enumerate(items):
        text = ext_str("files[%d]" % index, item)
        if text:
            files.append(text)
    return files


def tool_result(storage_dir, name, arguments):
    if name == "list_chats":
        return {"ok": True, "chats": list_started_chats(storage_dir)}
    if name == "get_chat_history":
        acct_id = _required(arguments, "acct_id")
        chat_id = _required(arguments, "chat_id")
        view = chat_history_view(storage_dir, acct_id, chat_id)
        if view is None:
            return {"ok": False, "error": "chat has not started"}
        return {"ok": True, "chat": view}
    if name == "send_message":
        acct_id = _required(arguments, "acct_id")
        chat_id = _required(arguments, "chat_id")
        user_id = _required(arguments, "user_id")
        text = _required(arguments, "text")
        files = _optional_files(arguments)
        view = chat_history_view(storage_dir, acct_id, chat_id)
        if view is None:
            return {"ok": False, "error": "chat has not started"}
        known = False
        for sender in view["senders"]:
            if sender.get("user_id") == user_id:
                known = True
                break
        if not known:
            return {"ok": False, "error": "user_id is not in this chat history"}
        return {
            "ok": True,
            "send": {
                "acct_id": acct_id,
                "chat_id": chat_id,
                "user_id": user_id,
                "text": text,
                "files": files,
                "platform": view["platform"],
                "chat_type": view["chat_type"],
            },
        }
    raise ExternalError("unknown tool")


def record_outbound(storage_dir, acct_id, chat_id, text):
    history = load_messages(storage_dir, acct_id, chat_id)
    history.append({"role": "assistant", "content": text})
    save_messages(storage_dir, acct_id, chat_id, history)
