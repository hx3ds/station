import re

_MATRIX_REPLY_PILL_RE = re.compile(r"^>\s*<(@[^>]+)>\s*(.*)$")

def extract_reply_fallback(body: str):
    body = (body or "").replace("\r\n", "\n")
    if not body.startswith("> "):
        return "", ""
    quoted = []
    author_id = ""
    for line in body.split("\n"):
        if not line.startswith("> ") and line != ">":
            break
        content = line[2:] if line.startswith("> ") else ""
        if not author_id:
            m = _MATRIX_REPLY_PILL_RE.match(line)
            if m:
                author_id = m.group(1)
                content = m.group(2)
        quoted.append(content)
    return "\n".join(quoted).strip(), author_id

def strip_reply_fallback(body: str) -> str:
    body = (body or "").replace("\r\n", "\n")
    if not body.startswith("> "):
        return body
    stripped = []
    past = False
    for line in body.split("\n"):
        if not past:
            if line.startswith("> ") or line == ">":
                continue
            if line == "":
                past = True
                continue
            past = True
        stripped.append(line)
    return "\n".join(stripped) if stripped else body

def apply_reply_quote(text: str, reply_to: str):
    from station.conductor.platforms import guidance

    out = text or ""
    if not (reply_to or "").strip():
        return out, ""
    if out.startswith("> "):
        snippet, author_id = extract_reply_fallback(out)
        out = strip_reply_fallback(out)
        if author_id and snippet:
            snippet = f"{author_id}: {snippet}"
        elif author_id:
            snippet = author_id
        reply_to_text = guidance.reply_snippet(snippet)
        out = guidance.inject_reply_context(out, reply_to_text)
        return out, reply_to_text
    return out, ""

def looks_like_matrix_image_filename(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate or "\n" in candidate or candidate.endswith("/"):
        return False
    if "/" in candidate or "\\" in candidate:
        return False
    name = candidate.rsplit("/", 1)[-1]
    if name != candidate:
        return False
    dot = name.rfind(".")
    if dot <= 0:
        return False
    return name[dot:].lower() in {
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif",
    }

def looks_like_matrix_media_filename(text: str) -> bool:
    candidate = (text or "").strip()
    if not candidate or "\n" in candidate or candidate.endswith("/"):
        return False
    if any(ch.isspace() for ch in candidate):
        return False
    if "/" in candidate or "\\" in candidate:
        return False
    dot = candidate.rfind(".")
    if dot <= 0:
        return False
    return candidate[dot:].lower() in {
        ".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".flac",
        ".mp4", ".webm", ".mkv", ".mov", ".avi",
        ".pdf", ".txt", ".doc", ".docx", ".zip", ".gz", ".tar", ".7z", ".rar",
        ".json", ".xml", ".csv", ".bin", ".apk", ".dmg",
    }

def clear_matrix_filename_body(msgtype: str, text: str) -> str:
    msgtype = (msgtype or "").strip()
    if msgtype == "m.image" and looks_like_matrix_image_filename(text):
        return ""
    if msgtype in ("m.audio", "m.file", "m.video") and looks_like_matrix_media_filename(text):
        return ""
    return text
