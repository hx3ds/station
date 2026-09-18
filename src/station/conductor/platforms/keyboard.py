import json

TELEGRAM_CALLBACK_DATA_MAX = 64
DISCORD_CUSTOM_ID_MAX = 100
DISCORD_LABEL_MAX = 80
DISCORD_BUTTONS_PER_ROW = 5
DISCORD_MAX_ROWS = 5
WHATSAPP_BUTTON_TITLE_MAX = 20
WHATSAPP_LIST_TITLE_MAX = 24
WHATSAPP_LIST_DESC_MAX = 72
WHATSAPP_BUTTON_MAX = 3
WHATSAPP_LIST_MAX = 10
WHATSAPP_BODY_MAX = 1024
WHATSAPP_BUTTON_ID_MAX = 256
QQ_LABEL_MAX = 40


def parse_keyboard(value):
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            value = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return []
    if isinstance(value, dict):
        if "rows" in value:
            return parse_keyboard(value.get("rows"))
        if "content" in value:
            return parse_keyboard(value.get("content"))
        if "inline_keyboard" in value:
            return parse_keyboard(value.get("inline_keyboard"))
        btn = _parse_button(value)
        return _normalize([[btn]] if btn else [])
    if isinstance(value, list):
        rows = []
        for row in value:
            items = []
            if isinstance(row, dict) and "buttons" in row:
                row = row.get("buttons")
            if isinstance(row, list):
                for item in row:
                    btn = _parse_button(item)
                    if btn:
                        items.append(btn)
            else:
                btn = _parse_button(row)
                if btn:
                    items.append(btn)
            if items:
                rows.append(items)
        return _normalize(rows)
    return []


def is_qq_native_keyboard(value):
    return isinstance(value, dict) and "content" in value


def format_keyboard_text(keyboard):
    rows = parse_keyboard(keyboard)
    if not rows:
        return ""
    lines = []
    n = 0
    for row in rows:
        for btn in row:
            n += 1
            label = (btn.get("text") or btn.get("id") or "").strip()
            url = (btn.get("url") or "").strip()
            if url:
                if label:
                    label = "%s (%s)" % (label, url)
                else:
                    label = url
            if not label:
                label = (btn.get("id") or "").strip()
            lines.append("%s. %s" % (n, label))
    if not lines:
        return ""
    lines.append("")
    lines.append("Reply with the number, the option text, or your own answer.")
    return "\n".join(lines)


def join_text_and_keyboard(text, keyboard):
    extra = format_keyboard_text(keyboard)
    text = (text or "").strip()
    if not extra:
        return text
    if not text:
        return extra
    return text + "\n\n" + extra


def telegram_reply_markup(keyboard):
    rows = parse_keyboard(keyboard)
    if not rows:
        return None
    out = []
    for row in rows:
        items = []
        for btn in row:
            text = (btn.get("text") or "").strip()
            if not text:
                continue
            item = {"text": text}
            url = (btn.get("url") or "").strip()
            if url:
                item["url"] = url
            else:
                data = _callback_id(btn)
                if not data:
                    continue
                item["callback_data"] = _truncate_bytes(data, TELEGRAM_CALLBACK_DATA_MAX)
            items.append(item)
        if items:
            out.append(items)
    if not out:
        return None
    return {"inline_keyboard": out}


def discord_components(keyboard):
    rows = parse_keyboard(keyboard)
    if not rows:
        return None
    out = []
    for row in rows:
        if len(out) >= DISCORD_MAX_ROWS:
            break
        comps = []
        for btn in row:
            if len(comps) >= DISCORD_BUTTONS_PER_ROW:
                break
            label = _truncate_runes(btn.get("text") or "", DISCORD_LABEL_MAX)
            if not label:
                continue
            item = {"type": 2, "label": label}
            url = (btn.get("url") or "").strip()
            if url:
                item["style"] = 5
                item["url"] = url
            else:
                custom_id = _callback_id(btn)
                if not custom_id:
                    continue
                item["style"] = 1
                item["custom_id"] = _truncate_runes(custom_id, DISCORD_CUSTOM_ID_MAX)
            comps.append(item)
        if comps:
            out.append({"type": 1, "components": comps})
    return out or None


def whatsapp_interactive(body, keyboard):
    rows = parse_keyboard(keyboard)
    if not rows:
        return None
    callbacks = []
    url_lines = []
    for row in rows:
        for btn in row:
            url = (btn.get("url") or "").strip()
            if url:
                label = (btn.get("text") or "").strip()
                url_lines.append("%s (%s)" % (label, url) if label else url)
                continue
            callbacks.append(btn)
    if not callbacks or len(callbacks) > WHATSAPP_LIST_MAX:
        return None
    option_lines = []
    for i, btn in enumerate(callbacks):
        label = (btn.get("text") or btn.get("id") or "").strip()
        option_lines.append("%s. %s" % (i + 1, label))
    body_text = (body or "").strip()
    if option_lines:
        joined = "\n".join(option_lines)
        body_text = joined if not body_text else body_text + "\n\n" + joined
    if url_lines:
        joined = "\n".join(url_lines)
        body_text = joined if not body_text else body_text + "\n" + joined
    body_text = _truncate_runes(body_text, WHATSAPP_BODY_MAX) or "Choose"
    if len(callbacks) <= WHATSAPP_BUTTON_MAX:
        buttons = []
        for i, btn in enumerate(callbacks):
            btn_id = _callback_id(btn) or str(i + 1)
            title = _truncate_runes(btn.get("text") or btn_id, WHATSAPP_BUTTON_TITLE_MAX)
            if not title:
                continue
            buttons.append({
                "type": "reply",
                "reply": {
                    "id": _truncate_runes(btn_id, WHATSAPP_BUTTON_ID_MAX),
                    "title": title,
                },
            })
        if not buttons:
            return None
        return {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": buttons},
        }
    list_rows = []
    for i, btn in enumerate(callbacks):
        btn_id = _callback_id(btn) or str(i + 1)
        list_rows.append({
            "id": _truncate_runes(btn_id, WHATSAPP_BUTTON_ID_MAX),
            "title": _truncate_runes(str(i + 1), WHATSAPP_LIST_TITLE_MAX),
            "description": _truncate_runes(btn.get("text") or "", WHATSAPP_LIST_DESC_MAX),
        })
    return {
        "type": "list",
        "body": {"text": body_text},
        "action": {
            "button": "Choose",
            "sections": [{"title": "Options", "rows": list_rows}],
        },
    }


def qq_keyboard(keyboard):
    if is_qq_native_keyboard(keyboard):
        return keyboard
    rows = parse_keyboard(keyboard)
    if not rows:
        return None
    out_rows = []
    for i, row in enumerate(rows):
        buttons = []
        for j, btn in enumerate(row):
            label = _truncate_runes(btn.get("text") or "", QQ_LABEL_MAX)
            if not label:
                continue
            btn_id = (btn.get("id") or "").strip() or ("b%s-%s" % (i, j))
            url = (btn.get("url") or "").strip()
            if url:
                action_type = 2
                data = url
            else:
                action_type = 1
                data = _callback_id(btn) or btn_id
            buttons.append({
                "id": btn_id,
                "render_data": {
                    "label": label,
                    "visited_label": label,
                    "style": 1,
                },
                "action": {
                    "type": action_type,
                    "data": data,
                    "permission": {"type": 2},
                },
                "group_id": "default",
            })
        if buttons:
            out_rows.append({"buttons": buttons})
    if not out_rows:
        return None
    return {"content": {"rows": out_rows}}


def _parse_button(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        text = str(value).strip()
        if not text:
            return None
        return {"text": text, "id": text, "url": ""}
    text = _first_str(value, "text", "label", "title")
    btn_id = _first_str(value, "id", "callback_data", "custom_id", "data")
    url = _first_str(value, "url")
    render = value.get("render_data")
    if isinstance(render, dict) and not text:
        text = _first_str(render, "label", "text")
    action = value.get("action")
    if isinstance(action, dict):
        action_type = str(action.get("type") or "").strip()
        data = _first_str(action, "data", "url")
        if action_type == "2" and not url:
            url = data
        elif data:
            btn_id = data
    if not (text or btn_id or url):
        return None
    if not text:
        text = btn_id or url
    return {"text": text, "id": btn_id, "url": url}


def _normalize(rows):
    out = []
    for row in rows:
        items = []
        for btn in row:
            if not btn:
                continue
            text = (btn.get("text") or "").strip()
            btn_id = (btn.get("id") or "").strip()
            url = (btn.get("url") or "").strip()
            if not (text or btn_id or url):
                continue
            if not text:
                text = btn_id or url
            items.append({"text": text, "id": btn_id, "url": url})
        if items:
            out.append(items)
    return out


def _callback_id(btn):
    return (btn.get("id") or btn.get("text") or "").strip()


def _first_str(obj, *keys):
    for key in keys:
        val = obj.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return ""


def _truncate_runes(text, limit):
    text = str(text or "")
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1] + "…"


def _truncate_bytes(text, limit):
    text = str(text or "")
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    while len(raw) > limit:
        text = text[:-1]
        raw = text.encode("utf-8")
        if not text:
            break
    return text
