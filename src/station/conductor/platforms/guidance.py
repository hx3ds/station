from __future__ import annotations

import html
import os
import re
from typing import Callable, Optional

MAX_TELEGRAM_MESSAGE_LENGTH = 4096
MAX_TELEGRAM_CAPTION_LENGTH = 1024
MAX_DISCORD_MESSAGE_LENGTH = 2000
MAX_DISCORD_SPLIT_MESSAGES = 8
MAX_MATRIX_MESSAGE_LENGTH = 16000
MAX_WHATSAPP_MESSAGE_LENGTH = 4096
MAX_QQ_MESSAGE_LENGTH = 4000

CHUNK_INDICATOR_RESERVE = 10
_FENCE_CLOSE = "\n```"
_FENCE_BODY_RE = re.compile(r"```.*?```", re.DOTALL)
_TRAILING_OPEN_FENCE_RE = re.compile(r"```[^`]*$")

_PLATFORM_MAX = {
    "telegram": MAX_TELEGRAM_MESSAGE_LENGTH,
    "discord": MAX_DISCORD_MESSAGE_LENGTH,
    "matrix": MAX_MATRIX_MESSAGE_LENGTH,
    "whatsapp": MAX_WHATSAPP_MESSAGE_LENGTH,
    "whatsapp_cloud": MAX_WHATSAPP_MESSAGE_LENGTH,
    "qq": MAX_QQ_MESSAGE_LENGTH,
}

_PLATFORM_CAPTION_MAX = {
    "telegram": MAX_TELEGRAM_CAPTION_LENGTH,
    "whatsapp": MAX_WHATSAPP_MESSAGE_LENGTH,
    "whatsapp_cloud": MAX_WHATSAPP_MESSAGE_LENGTH,
    "qq": MAX_QQ_MESSAGE_LENGTH,
    "discord": MAX_DISCORD_MESSAGE_LENGTH,
    "matrix": MAX_MATRIX_MESSAGE_LENGTH,
}

_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")
_CHUNK_INDICATOR_ON_FENCE_RE = re.compile(
    r"(?m)^``` (?P<indicator>(?:\\)?\(\d+/\d+(?:\\)?\))$"
)
_TELEGRAM_FENCE_RE = re.compile(r"(?s)(```(?:[^\n]*\n)?[\s\S]*?```)")
_TELEGRAM_INLINE_CODE_RE = re.compile(r"(`[^`]+`)")
_TELEGRAM_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
_TELEGRAM_HEADER_RE = re.compile(r"(?m)^#{1,6}\s+(.+)$")
_TELEGRAM_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_TELEGRAM_ITALIC_RE = re.compile(r"\*([^*\n]+)\*")
_TELEGRAM_STRIKE_RE = re.compile(r"~~(.+?)~~")
_TELEGRAM_SPOILER_RE = re.compile(r"\|\|(.+?)\|\|")
_TELEGRAM_BLOCKQUOTE_RE = re.compile(r"(?m)^((?:\*\*)?>{1,3}) (.+)$")
_TELEGRAM_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*){1,}\|?\s*$"
)
_STRIP_MDV2_ESCAPE_RE = re.compile(r"\\([_*\[\]()~`>#+\-=|{}.!\\])")

_INVISIBLE_CHARS_RE = re.compile(r"[\u200b\u2060\u2063\ufeff]")
_ODD_SPACE_RE = re.compile(r"[\u00a0\u1680\u180e\u2000-\u200a\u202f\u205f\u3000]")
_WA_FENCE_RE = re.compile(r"(?s)```.*?```")
_WA_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_WA_BOLD_STAR_RE = re.compile(r"\*\*(.+?)\*\*")
_WA_BOLD_UNDER_RE = re.compile(r"__(.+?)__")
_WA_STRIKE_RE = re.compile(r"~~(.+?)~~")
_WA_HEADER_RE = re.compile(r"(?m)^#{1,6}\s+(.+)$")
_WA_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_WA_SINGLE_STAR_RE = re.compile(r"\*([^*\n]+)\*")

_MATRIX_IMAGE_MD_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_TELEGRAM_CHUNK_INDEX_RE = re.compile(r" \((\d+)/(\d+)\)$")
_MATRIX_FENCE_RE = re.compile(r"(?s)```([^\n]*)\n(.*?)```")
_MATRIX_INLINE_RE = re.compile(r"`([^`]+)`")
_MATRIX_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MATRIX_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MATRIX_ITALIC_RE = re.compile(r"\*([^*\n]+)\*")
_MATRIX_STRIKE_RE = re.compile(r"~~(.+?)~~")
_MATRIX_HEADER_RE = re.compile(r"(?m)^(#{1,6})\s+(.+)$")

_QQ_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_QQ_ITALIC_STAR_RE = re.compile(r"\*([^*\n]+)\*")
_QQ_BOLD_UNDER_RE = re.compile(r"__(.+?)__")
_QQ_ITALIC_UNDER_RE = re.compile(r"_([^_\n]+)_")
_QQ_CODE_BLOCK_RE = re.compile(r"(?s)```.*?```")
_QQ_INLINE_CODE_RE = re.compile(r"`(.+?)`")
_QQ_HEADING_RE = re.compile(r"(?m)^#{1,6}\s+")
_QQ_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_QQ_MULTI_NL_RE = re.compile(r"\n{3,}")

LenFn = Callable[[str], int]

def utf16_len(s: str) -> int:

    n = 0
    for ch in s:
        cp = ord(ch)
        if cp >= 0x10000 or 0xD800 <= cp <= 0xDFFF:
            n += 2
        else:
            n += 1
    return n

def rune_len(s: str) -> int:
    return len(s)

def max_message_length(platform: str) -> int:
    return _PLATFORM_MAX.get((platform or "").strip().lower(), MAX_TELEGRAM_MESSAGE_LENGTH)

def max_caption_length(platform: str) -> int:
    return _PLATFORM_CAPTION_MAX.get((platform or "").strip().lower(), MAX_TELEGRAM_CAPTION_LENGTH)

def uses_utf16_length(platform: str) -> bool:
    return (platform or "").strip().lower() == "telegram"

def _custom_unit_to_cp(s: str, budget: int, len_fn: LenFn) -> int:
    if len_fn(s) <= budget:
        return len(s)
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len_fn(s[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo

def prefix_within_len(s: str, limit: int, len_fn: Optional[LenFn] = None) -> str:
    if len_fn is None:
        len_fn = rune_len
    if len_fn(s) <= limit:
        return s
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if len_fn(s[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    return s[:lo]

def truncate_to_limit(content: str, max_length: int, len_fn: Optional[LenFn] = None) -> str:
    if not content or max_length <= 0:
        return content
    if len_fn is None:
        len_fn = rune_len
    if len_fn(content) <= max_length:
        return content
    return prefix_within_len(content, max_length, len_fn)

def truncate_message(
    content: str,
    max_length: int,
    len_fn: Optional[LenFn] = None,
) -> list[str]:

    if max_length <= 0:
        max_length = MAX_TELEGRAM_MESSAGE_LENGTH
    if not content:
        return []
    if len_fn is None:
        len_fn = rune_len
    if len_fn(content) <= max_length:
        return [content]

    chunks: list[str] = []
    remaining = content
    carry_lang: Optional[str] = None

    while remaining:
        prefix = f"```{carry_lang}\n" if carry_lang is not None else ""
        headroom = max_length - CHUNK_INDICATOR_RESERVE - len_fn(prefix) - len_fn(_FENCE_CLOSE)
        if headroom < 1:
            headroom = max(1, max_length // 2)
        if len_fn(prefix) + len_fn(remaining) <= max_length - CHUNK_INDICATOR_RESERVE:
            chunks.append(prefix + remaining)
            break

        cp_limit = _custom_unit_to_cp(remaining, headroom, len_fn)
        region = remaining[:cp_limit] if cp_limit < len(remaining) else remaining
        split_at = region.rfind("\n")
        if split_at < cp_limit // 2:
            split_at = region.rfind(" ")
        if split_at < 1:
            split_at = max(1, cp_limit)
            if split_at > len(remaining):
                split_at = len(remaining)
            while split_at > 0 and (remaining[:split_at].encode("utf-8", errors="ignore").decode("utf-8") != remaining[:split_at]):

                split_at -= 1
            if split_at < 1:
                split_at = 1

        candidate = remaining[:split_at]
        backtick_count = candidate.count("`") - candidate.count("\\`")
        if backtick_count % 2 == 1:
            last_bt = candidate.rfind("`")
            while last_bt > 0 and candidate[last_bt - 1] == "\\":
                last_bt = candidate.rfind("`", 0, last_bt)
            if last_bt > 0:
                safe_split = candidate.rfind(" ", 0, last_bt)
                nl_split = candidate.rfind("\n", 0, last_bt)
                if nl_split > safe_split:
                    safe_split = nl_split
                if safe_split > cp_limit // 4:
                    split_at = safe_split

        chunk_body = remaining[:split_at]
        remaining = remaining[split_at:].lstrip("\n \t")
        full_chunk = prefix + chunk_body

        in_code = carry_lang is not None
        lang = carry_lang or ""
        for line in chunk_body.split("\n"):
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_code:
                    in_code = False
                    lang = ""
                else:
                    in_code = True
                    tag = stripped[3:].strip()
                    lang = tag.split()[0] if tag else ""
        if in_code:
            full_chunk += _FENCE_CLOSE
            carry_lang = lang
        else:
            carry_lang = None
        chunks.append(full_chunk)

    if len(chunks) <= 1:
        return chunks
    total = len(chunks)
    out: list[str] = []
    for i, chunk in enumerate(chunks):
        indicator = f" ({i + 1}/{total})"
        if len_fn(chunk) + len_fn(indicator) <= max_length:
            out.append(chunk + indicator)
            continue
        keep_budget = max_length - len_fn(indicator)
        if keep_budget < 1:
            keep_budget = 1
        out.append(prefix_within_len(chunk, keep_budget, len_fn) + indicator)
    return out

def _escape_mdv2(text: str) -> str:
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)

def strip_markdown_v2(text: str) -> str:

    cleaned = _STRIP_MDV2_ESCAPE_RE.sub(r"\1", text)
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
    cleaned = re.sub(r"_([^_]+)_", r"\1", cleaned)
    cleaned = re.sub(r"~([^~]+)~", r"\1", cleaned)
    cleaned = re.sub(r"\|\|([^|]+)\|\|", r"\1", cleaned)
    return cleaned

def separate_chunk_indicator_from_fence(text: str) -> str:
    return _CHUNK_INDICATOR_ON_FENCE_RE.sub(r"```\n\g<indicator>", text)

def escape_telegram_chunk_index(text: str) -> str:
    return _TELEGRAM_CHUNK_INDEX_RE.sub(r" \\(\1/\2\\)", text)

def _looks_like_table_row(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and "|" in stripped

def _split_markdown_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [p.strip() for p in stripped.split("|")]

def _render_table_block(table_block: list[str]) -> str:
    if len(table_block) < 3:
        return "\n".join(table_block)
    headers = _split_markdown_table_row(table_block[0])
    if len(headers) < 2:
        return "\n".join(table_block)
    groups: list[str] = []
    for row_line in table_block[2:]:
        cells = _split_markdown_table_row(row_line)
        bullets: list[str] = []
        for i, header in enumerate(headers):
            val = cells[i] if i < len(cells) else ""
            if not val or val == header:
                continue
            bullets.append(f"- **{header}**: {val}")
        if bullets:
            groups.append("\n".join(bullets))
    if not groups:
        return "\n".join(table_block)
    return "\n\n".join(groups)

def convert_tables_to_bullets(content: str) -> str:
    lines = content.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if (
            i + 2 < len(lines)
            and _looks_like_table_row(lines[i])
            and _TELEGRAM_TABLE_SEPARATOR_RE.match(lines[i + 1])
            and _looks_like_table_row(lines[i + 2])
        ):
            block = [lines[i], lines[i + 1]]
            j = i + 2
            while j < len(lines) and _looks_like_table_row(lines[j]):
                block.append(lines[j])
                j += 1
            out.append(_render_table_block(block))
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)

def format_telegram_markdown_v2(content: str) -> str:

    if not content:
        return content
    text = convert_tables_to_bullets(content)
    placeholders: dict[str, str] = {}
    counter = 0

    def ph(value: str) -> str:
        nonlocal counter
        key = f"\x00PH{counter}\x00"
        counter += 1
        placeholders[key] = value
        return key

    def protect_fence(m: re.Match) -> str:
        raw = m.group(0)
        open_end = 3
        nl = raw.find("\n", 3)
        if nl >= 0:
            open_end = nl + 1
        opening = raw[:open_end]
        body_and_close = raw[open_end:]
        if len(body_and_close) < 3:
            return ph(raw)
        body = body_and_close[:-3]
        body = body.replace("\\", "\\\\").replace("`", "\\`")
        return ph(opening + body + "```")

    text = _TELEGRAM_FENCE_RE.sub(protect_fence, text)
    text = _TELEGRAM_INLINE_CODE_RE.sub(lambda m: ph(m.group(0).replace("\\", "\\\\")), text)

    def protect_link(m: re.Match) -> str:
        display = _escape_mdv2(m.group(1))
        url = m.group(2).replace("\\", "\\\\").replace(")", "\\)")
        return ph(f"[{display}]({url})")

    text = _TELEGRAM_LINK_RE.sub(protect_link, text)

    def protect_header(m: re.Match) -> str:
        inner = m.group(1).strip()
        inner = _TELEGRAM_BOLD_RE.sub(r"\1", inner)
        return ph("*" + _escape_mdv2(inner) + "*")

    text = _TELEGRAM_HEADER_RE.sub(protect_header, text)
    text = _TELEGRAM_BOLD_RE.sub(lambda m: ph("*" + _escape_mdv2(m.group(1)) + "*"), text)
    text = _TELEGRAM_ITALIC_RE.sub(lambda m: ph("_" + _escape_mdv2(m.group(1)) + "_"), text)
    text = _TELEGRAM_STRIKE_RE.sub(lambda m: ph("~" + _escape_mdv2(m.group(1)) + "~"), text)
    text = _TELEGRAM_SPOILER_RE.sub(lambda m: ph("||" + _escape_mdv2(m.group(1)) + "||"), text)

    def protect_blockquote(m: re.Match) -> str:
        prefix = m.group(1)
        content_inner = m.group(2)
        if prefix.startswith("**") and content_inner.endswith("||"):
            return ph(prefix + " " + _escape_mdv2(content_inner[:-2]) + "||")
        return ph(prefix + " " + _escape_mdv2(content_inner))

    text = _TELEGRAM_BLOCKQUOTE_RE.sub(protect_blockquote, text)
    text = _escape_mdv2(text)
    for i in range(counter - 1, -1, -1):
        key = f"\x00PH{i}\x00"
        if key in placeholders:
            text = text.replace(key, placeholders[key])
    return text

def sanitize_outbound_text(content: str) -> str:
    if not content:
        return content
    content = _INVISIBLE_CHARS_RE.sub("", content)
    return _ODD_SPACE_RE.sub(" ", content)

def format_whatsapp_message(content: str) -> str:

    if not content:
        return content
    content = sanitize_outbound_text(content)
    fences: list[str] = []
    codes: list[str] = []
    bolds: list[str] = []

    def save_fence(m: re.Match) -> str:
        fences.append(m.group(0))
        return f"\x00FENCE{len(fences) - 1}\x00"

    def save_code(m: re.Match) -> str:
        codes.append(m.group(0))
        return f"\x00CODE{len(codes) - 1}\x00"

    result = _WA_FENCE_RE.sub(save_fence, content)
    result = _WA_INLINE_CODE_RE.sub(save_code, result)

    def save_bold_star(m: re.Match) -> str:
        bolds.append("*" + m.group(1) + "*")
        return f"\x00BOLD{len(bolds) - 1}\x00"

    def save_bold_under(m: re.Match) -> str:
        bolds.append("*" + m.group(1) + "*")
        return f"\x00BOLD{len(bolds) - 1}\x00"

    result = _WA_BOLD_STAR_RE.sub(save_bold_star, result)
    result = _WA_BOLD_UNDER_RE.sub(save_bold_under, result)
    result = _WA_SINGLE_STAR_RE.sub(r"_\1_", result)
    result = _WA_STRIKE_RE.sub(r"~\1~", result)

    def header_to_bold(m: re.Match) -> str:
        inner = m.group(1).strip()
        while len(inner) > 1 and inner.startswith("*") and inner.endswith("*"):
            inner = inner[1:-1].strip()
        return f"*{inner}*"

    result = _WA_HEADER_RE.sub(header_to_bold, result)
    result = _WA_LINK_RE.sub(r"\1 (\2)", result)
    for i, bold in enumerate(bolds):
        result = result.replace(f"\x00BOLD{i}\x00", bold)
    for i, fence in enumerate(fences):
        result = result.replace(f"\x00FENCE{i}\x00", fence)
    for i, code in enumerate(codes):
        result = result.replace(f"\x00CODE{i}\x00", code)
    return result

def format_discord_message(content: str) -> str:
    if not content:
        return content
    return convert_tables_to_bullets(content)

def format_matrix_message(content: str) -> str:
    if not content:
        return content
    return _MATRIX_IMAGE_MD_RE.sub(r"\1", content).strip()

def matrix_html(content: str) -> str:

    if not content:
        return ""
    text = format_matrix_message(content)
    placeholders: list[tuple[str, str]] = []

    def protect(value: str) -> str:
        key = f"\x00MH{len(placeholders)}\x00"
        placeholders.append((key, value))
        return key

    def fence_repl(m: re.Match) -> str:
        lang = (m.group(1) or "").strip()
        body = html.escape(m.group(2) or "")
        class_attr = f' class="language-{html.escape(lang)}"' if lang else ""
        return protect(f"<pre><code{class_attr}>{body}</code></pre>")

    text = _MATRIX_FENCE_RE.sub(fence_repl, text)
    text = _MATRIX_INLINE_RE.sub(lambda m: protect("<code>" + html.escape(m.group(1)) + "</code>"), text)

    def link_repl(m: re.Match) -> str:
        label = html.escape(m.group(1))
        href = html.escape(m.group(2))
        if href.lower().startswith("javascript:"):
            return label
        return protect(f'<a href="{href}">{label}</a>')

    text = _MATRIX_LINK_RE.sub(link_repl, text)

    def header_repl(m: re.Match) -> str:
        level = len(m.group(1))
        level = max(1, min(6, level))
        return protect(f"<h{level}>{html.escape(m.group(2))}</h{level}>")

    text = _MATRIX_HEADER_RE.sub(header_repl, text)
    text = _MATRIX_BOLD_RE.sub(lambda m: protect("<strong>" + html.escape(m.group(1)) + "</strong>"), text)
    text = _MATRIX_ITALIC_RE.sub(lambda m: protect("<em>" + html.escape(m.group(1)) + "</em>"), text)
    text = _MATRIX_STRIKE_RE.sub(lambda m: protect("<del>" + html.escape(m.group(1)) + "</del>"), text)

    escaped = html.escape(text)
    for key, value in reversed(placeholders):
        escaped = escaped.replace(html.escape(key), value)
    return escaped.replace("\n", "<br/>")

def qq_markdown_enabled() -> bool:
    v = (os.environ.get("QQ_MARKDOWN_SUPPORT") or "").strip()
    return v == "" or (v != "0" and v.lower() != "false")

def strip_markdown(text: str) -> str:
    text = _QQ_BOLD_RE.sub(r"\1", text)
    text = _QQ_ITALIC_STAR_RE.sub(r"\1", text)
    text = _QQ_BOLD_UNDER_RE.sub(r"\1", text)
    text = _QQ_ITALIC_UNDER_RE.sub(r"\1", text)
    text = _QQ_CODE_BLOCK_RE.sub("", text)
    text = _QQ_INLINE_CODE_RE.sub(r"\1", text)
    text = _QQ_HEADING_RE.sub("", text)
    text = _QQ_LINK_RE.sub(r"\1", text)
    text = _QQ_MULTI_NL_RE.sub("\n\n", text)
    return text.strip()

def format_qq_message(content: str) -> str:
    if not content:
        return content
    if qq_markdown_enabled():
        return content
    return strip_markdown(content)

def format_outbound_text(platform: str, text: str) -> str:
    p = (platform or "").strip().lower()
    if p == "telegram":
        return format_telegram_markdown_v2(text)
    if p in ("whatsapp", "whatsapp_cloud"):
        return format_whatsapp_message(text)
    if p == "discord":
        return format_discord_message(text)
    if p == "matrix":
        return format_matrix_message(text)
    if p == "qq":
        return format_qq_message(text)
    return text

def ensure_closed_code_fences(text: str) -> str:

    if not text or "`" not in text:
        return text
    if text.count("```") % 2 == 1:
        text = text.rstrip("\n") + "\n```"
    without = _FENCE_BODY_RE.sub("", text)
    without = _TRAILING_OPEN_FENCE_RE.sub("", without)
    if without.count("`") % 2 == 1:
        text = text + "`"
    return text

def prepare_outbound_text(platform: str, text: str) -> list[str]:
    text = ensure_closed_code_fences(text or "")
    formatted = format_outbound_text(platform, text)
    if not formatted:
        return []
    max_len = max_message_length(platform)
    len_fn = utf16_len if uses_utf16_length(platform) else None
    chunks = truncate_message(formatted, max_len, len_fn)
    p = (platform or "").strip().lower()
    if p == "discord" and len(chunks) > MAX_DISCORD_SPLIT_MESSAGES:
        note = "\n…(truncated)"
        kept = list(chunks[: MAX_DISCORD_SPLIT_MESSAGES - 1])
        last = chunks[MAX_DISCORD_SPLIT_MESSAGES - 1]
        if len_fn is None:
            if len(last) + len(note) <= max_len:
                last = last + note
            else:
                last = truncate_to_limit(last, max(1, max_len - len(note)), None) + note
        else:
            if len_fn(last) + len_fn(note) <= max_len:
                last = last + note
            else:
                last = truncate_to_limit(last, max(1, max_len - len_fn(note)), len_fn) + note
        chunks = kept + [last]
    if p == "telegram":
        chunks = [separate_chunk_indicator_from_fence(c) for c in chunks]
        if len(chunks) > 1:
            chunks = [escape_telegram_chunk_index(c) for c in chunks]
            chunks = [
                truncate_to_limit(c, max_len, utf16_len) if utf16_len(c) > max_len else c
                for c in chunks
            ]
    return chunks

def prepare_outbound_caption(platform: str, caption: str) -> str:
    caption = ensure_closed_code_fences(caption or "")
    formatted = format_outbound_text(platform, caption)
    if not formatted:
        return ""
    max_len = max_caption_length(platform)
    len_fn = utf16_len if uses_utf16_length(platform) else None
    return truncate_to_limit(formatted, max_len, len_fn)

def default_discord_allowed_mentions() -> dict:

    return {
        "parse": ["users"],
        "replied_user": True,
    }

def sticker_injection(description: str, emoji: str = "", set_name: str = "") -> str:
    desc = (description or "").strip() or "a sticker"
    emoji = (emoji or "").strip()
    set_name = (set_name or "").strip()
    msg = "[The user sent a sticker"
    if emoji:
        msg += f" {emoji}"
    if set_name:
        msg += f' from set "{set_name}"'
    msg += f'. It shows: "{desc}"]'
    return msg

def animated_sticker_injection(emoji: str = "") -> str:
    emoji = (emoji or "").strip()
    if not emoji:
        return "[The user sent an animated sticker]"
    return f"[The user sent an animated sticker {emoji}. The emoji suggests: {emoji}]"

def format_coord(v):
    return format(float(v), "g").replace("+", "")

def location_injection(lat, lon, venue_title="", address=""):
    parts = ["[The user shared a location pin.]"]
    venue_title = (venue_title or "").strip()
    address = (address or "").strip()
    if venue_title:
        parts.append("Venue: " + venue_title)
    if address:
        parts.append("Address: " + address)
    lat_s = format_coord(lat)
    lon_s = format_coord(lon)
    parts.append("latitude: " + lat_s)
    parts.append("longitude: " + lon_s)
    parts.append(f"Map: https://www.google.com/maps/search/?api=1&query={lat_s},{lon_s}")
    return "\n".join(parts)

def parse_geo_uri(uri):
    uri = (uri or "").strip()
    if not uri:
        return None
    for sep in (";", "?"):
        i = uri.find(sep)
        if i >= 0:
            uri = uri[:i]
    if not uri.lower().startswith("geo:"):
        return None
    coords = uri[4:]
    parts = coords.split(",")
    if len(parts) < 2:
        return None
    try:
        lat = float(parts[0].strip())
        lon = float(parts[1].strip())
    except ValueError:
        return None
    return lat, lon

def contact_injection(name="", phones=None):
    parts = ["[The user shared a contact.]"]
    name = (name or "").strip()
    if name:
        parts.append("Name: " + name)
    if phones:
        for p in phones:
            p = (p or "").strip()
            if p:
                parts.append("Phone: " + p)
    return "\n".join(parts)

def discord_sticker_url(sticker_id, format_type=1, api_base=""):
    sticker_id = (sticker_id or "").strip()
    if not sticker_id:
        return "", "", False
    fmt = int(format_type)
    base = (api_base or "").strip().rstrip("/")
    lower = base.lower()
    if base and ("discord.com" in lower or "discordapp.com" in lower):
        base = ""
    elif base:
        idx = lower.rfind("/api/v")
        if idx >= 0:
            rest = lower[idx + len("/api/v") :]
            n = 0
            while n < len(rest) and rest[n].isdigit():
                n += 1
            if n > 0 and (n == len(rest) or rest[n] in "/?"):
                base = base[:idx].rstrip("/")
    if fmt == 3:
        return "", "", True
    if fmt == 4:
        if base:
            return f"{base}/cdn/stickers/{sticker_id}.gif", "image/gif", True
        return f"https://media.discordapp.net/stickers/{sticker_id}.gif", "image/gif", True
    if fmt == 2:
        if base:
            return f"{base}/cdn/stickers/{sticker_id}.png", "image/png", True
        return f"https://cdn.discordapp.com/stickers/{sticker_id}.png", "image/png", True
    if base:
        return f"{base}/cdn/stickers/{sticker_id}.png", "image/png", False
    return f"https://cdn.discordapp.com/stickers/{sticker_id}.png", "image/png", False

_TELEGRAM_REPLY_SNIPPET_MAX = 500

def telegram_reply_snippet(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) <= _TELEGRAM_REPLY_SNIPPET_MAX:
        return text
    return text[:_TELEGRAM_REPLY_SNIPPET_MAX]

def inject_telegram_reply_context(message_text: str, reply_snippet: str) -> str:
    return inject_reply_context(message_text, reply_snippet)

def format_telegram_reaction_text(emojis=None, custom_emoji_ids=None) -> str:
    return format_reaction_text(emojis, custom_emoji_ids)

def reply_snippet(text: str) -> str:
    return telegram_reply_snippet(text)

def inject_reply_context(message_text: str, reply_snippet: str) -> str:
    snippet = telegram_reply_snippet(reply_snippet)
    if not snippet:
        return (message_text or "").strip()
    prefix = f'[Replying to: "{snippet}"]'
    message_text = (message_text or "").strip()
    if not message_text:
        return prefix
    return prefix + "\n\n" + message_text

def format_reaction_text(emojis=None, custom_emoji_ids=None) -> str:
    parts = []
    for e in emojis or []:
        e = (e or "").strip()
        if e:
            parts.append(e)
    for cid in custom_emoji_ids or []:
        cid = (cid or "").strip()
        if cid:
            parts.append("custom:" + cid)
    if not parts:
        return "[Reaction removed]"
    return "[Reaction]: " + " ".join(parts)

MAX_TELEGRAM_MEDIA_BYTES = 20 * 1024 * 1024
MAX_TELEGRAM_MEDIA_LOCAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_DISCORD_MEDIA_BYTES = 32 * 1024 * 1024
MAX_MATRIX_MEDIA_BYTES = 100 * 1024 * 1024
MAX_WHATSAPP_IMAGE_BYTES = 5 * 1024 * 1024
MAX_WHATSAPP_VIDEO_BYTES = 16 * 1024 * 1024
MAX_WHATSAPP_AUDIO_BYTES = 16 * 1024 * 1024
MAX_WHATSAPP_DOCUMENT_BYTES = 100 * 1024 * 1024
MAX_WHATSAPP_STICKER_BYTES = 100 * 1024
MAX_QQ_MEDIA_BYTES = 100 * 1024 * 1024

_METHOD_MEDIA_KIND = {
    "send_photo": "image",
    "send_video": "video",
    "send_animation": "video",
    "send_audio": "audio",
    "send_voice": "audio",
    "send_document": "document",
    "send_sticker": "sticker",
}

_ATT_MEDIA_KIND = {
    "photo": "image",
    "image": "image",
    "video": "video",
    "animation": "video",
    "video_note": "video",
    "audio": "audio",
    "voice": "audio",
    "sticker": "sticker",
    "document": "document",
    "file": "document",
}

class MediaTooLargeError(Exception):
    def __init__(self, platform: str, kind: str, size: int, limit: int):
        self.platform = platform
        self.kind = kind
        self.size = size
        self.limit = limit
        super().__init__(f"{platform} {kind} exceeds size limit ({self.size} > {self.limit} bytes)")

def _env_int(key: str, fallback: int) -> int:
    raw = (os.environ.get(key) or "").strip()
    if not raw:
        return fallback
    n = int(raw)
    return n if n >= 0 else fallback

def telegram_local_bot_api(base_url: str = "") -> bool:
    base = (base_url or "").strip().lower()
    if not base:
        base = (
            os.environ.get("TELEGRAM_API_URL")
            or os.environ.get("TGB_API_URL")
            or os.environ.get("TELEGRAM_API_BASE")
            or ""
        ).strip().lower()
    if not base:
        return False
    return "api.telegram.org" not in base

def max_telegram_media_bytes(base_url: str = "") -> int:
    override = _env_int("TELEGRAM_MAX_MEDIA_BYTES", 0)
    if override > 0:
        return override
    if telegram_local_bot_api(base_url):
        return MAX_TELEGRAM_MEDIA_LOCAL_BYTES
    return MAX_TELEGRAM_MEDIA_BYTES

def media_kind_from_method(method: str) -> str:
    return _METHOD_MEDIA_KIND.get((method or "").strip(), "document")

_AUDIO_EXTS = frozenset({".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"})
_TELEGRAM_AUDIO_EXTS = frozenset({".mp3", ".m4a"})
_TELEGRAM_VOICE_EXTS = frozenset({".ogg", ".opus"})

def resolve_telegram_media_method(method, filename=""):
    method = (method or "").strip()
    ext = os.path.splitext((filename or "").strip().lower())[1]
    if method not in ("send_audio", "send_voice"):
        return method
    if ext in _TELEGRAM_VOICE_EXTS:
        if method == "send_voice":
            return "send_voice"
        return "send_document"
    if ext in _TELEGRAM_AUDIO_EXTS:
        return "send_audio"
    if ext in _AUDIO_EXTS:
        return "send_document"
    return method

def media_kind_from_attachment_type(att_type: str) -> str:
    return _ATT_MEDIA_KIND.get((att_type or "").strip().lower(), "document")

def max_media_bytes(platform: str, kind: str = "document", *, telegram_base_url: str = "") -> int:
    platform = (platform or "").strip().lower()
    kind = (kind or "document").strip().lower()
    if platform == "telegram":
        return max_telegram_media_bytes(telegram_base_url)
    if platform == "discord":
        return _env_int("DISCORD_MAX_ATTACHMENT_BYTES", MAX_DISCORD_MEDIA_BYTES)
    if platform == "matrix":
        return _env_int("MATRIX_MAX_MEDIA_BYTES", MAX_MATRIX_MEDIA_BYTES)
    if platform in ("whatsapp", "whatsapp_cloud"):
        return {
            "image": MAX_WHATSAPP_IMAGE_BYTES,
            "video": MAX_WHATSAPP_VIDEO_BYTES,
            "audio": MAX_WHATSAPP_AUDIO_BYTES,
            "sticker": MAX_WHATSAPP_STICKER_BYTES,
        }.get(kind, MAX_WHATSAPP_DOCUMENT_BYTES)
    if platform == "qq":
        return _env_int("QQ_MAX_MEDIA_BYTES", MAX_QQ_MEDIA_BYTES)
    return MAX_TELEGRAM_MEDIA_BYTES

def check_media_size(platform: str, size: int, kind: str = "document", *, telegram_base_url: str = "") -> None:
    size = size or 0
    if size <= 0:
        return
    limit = max_media_bytes(platform, kind, telegram_base_url=telegram_base_url)
    if limit > 0 and size > limit:
        raise MediaTooLargeError(platform, kind, size, limit)

def media_too_large_note(platform: str, label: str, size: int, limit: int) -> str:
    limit_mb = max(1, limit // (1024 * 1024))
    size_text = "unknown size"
    if size and size > 0:
        size_text = f"{size / (1024 * 1024):.1f} MB"
    plat = (platform or "platform").strip() or "platform"
    plat = plat[:1].upper() + plat[1:]
    label = (label or "media").strip() or "media"
    return (
        f"[{plat} {label} skipped: file size {size_text} exceeds the "
        f"{limit_mb} MB limit. Ask the user to send a smaller file.]"
    )

def filter_oversized_attachments(platform: str, attachments: list | None, *, telegram_base_url: str = ""):
    kept = []
    notes = []
    for att in attachments or []:
        kind = media_kind_from_attachment_type(att.get("type") or "")
        raw_size = att.get("file_size")
        if raw_size is None or raw_size == "":
            raw_size = att.get("size")
        size = int(raw_size) if raw_size is not None and raw_size != "" else 0
        try:
            check_media_size(platform, size, kind, telegram_base_url=telegram_base_url)
        except MediaTooLargeError as exc:
            notes.append(media_too_large_note(platform, att.get("type") or kind, exc.size, exc.limit))
            continue
        kept.append(att)
    return kept, notes
