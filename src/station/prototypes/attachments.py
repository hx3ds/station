import os
from pathlib import Path

from station import logger
from station.prototypes.boundary import ext_list, ext_str, validate_attachment, validate_attachments

ATTACHMENT_TYPE_METHOD_FIELD = {
    "photo": ("send_photo", "photo"),
    "video": ("send_video", "video"),
    "audio": ("send_audio", "audio"),
    "document": ("send_document", "document"),
    "voice": ("send_voice", "voice"),
    "animation": ("send_animation", "animation"),
    "sticker": ("send_sticker", "sticker"),
}

ATTACHMENT_METHOD_TO_TYPE = {
    method: att_type for att_type, (method, _) in ATTACHMENT_TYPE_METHOD_FIELD.items()
}

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
AUDIO_SUFFIXES = frozenset({".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm", ".ogg", ".aac", ".flac", ".opus"})

def attachment_str(attachment, *keys):
    for key in keys:
        value = attachment.get(key)
        if value is None:
            continue
        text = ext_str(key, value)
        if text:
            return text
    return ""

def attachment_display_name(attachment, *, local_path=""):
    return attachment_str(attachment, "file_name", "filename", "name", "title") or (
        Path(local_path).name if local_path else "attachment"
    )

def attachment_content_type(attachment):
    return attachment_str(attachment, "content_type", "mime_type").lower()

def is_image_attachment(attachment, *, local_path=""):
    if attachment_str(attachment, "type").lower() in {"image", "photo"}:
        return True
    if attachment_content_type(attachment).startswith("image/"):
        return True
    if local_path:
        return Path(local_path).suffix.lower() in IMAGE_SUFFIXES
    return False

def is_audio_attachment(attachment, *, local_path=""):
    if attachment_str(attachment, "type").lower() in {"audio", "voice"}:
        return True
    if attachment_content_type(attachment).startswith("audio/"):
        return True
    if local_path:
        return Path(local_path).suffix.lower() in AUDIO_SUFFIXES
    return False

def is_pdf_attachment(attachment, *, local_path=""):
    if attachment_content_type(attachment) == "application/pdf":
        return True
    if local_path:
        return Path(local_path).suffix.lower() == ".pdf"
    return False

def normalize_attachments(value):
    return validate_attachments(value)

def merge_attachments(existing, new_items):
    merged = list(normalize_attachments(existing))
    merged.extend(normalize_attachments(new_items))
    return merged

def find_attachment(attachments, *, types=None, require_file_id=False):
    allowed_types = set(types) if types else None
    for att in normalize_attachments(attachments):
        if allowed_types and att.get("type") not in allowed_types:
            continue
        if require_file_id and not att.get("file_id"):
            continue
        return att
    return None

def attachment_type_for_method(method):
    return ATTACHMENT_METHOD_TO_TYPE.get(method)

def attachment_from_params(method, params):
    attachment_type = attachment_type_for_method(method)
    if not attachment_type:
        return None
    file_id = params.get(attachment_type)
    if not file_id:
        return None
    attachment = {"type": attachment_type, "file_id": file_id}
    file_name = params.get("file_name")
    if file_name:
        attachment["file_name"] = file_name
    return attachment

def attachment_summary_parts(attachments, limit=5):
    parts = []
    for att in normalize_attachments(attachments)[:limit]:
        attachment_type = att.get("type")
        if attachment_type is None:
            attachment_type = "file"
        else:
            attachment_type = ext_str("attachment type", attachment_type)
        file_name = att.get("file_name")
        if file_name is None:
            file_name = att.get("filename")
        if file_name is None:
            file_name = ""
        else:
            file_name = ext_str("attachment file_name", file_name)
        file_id = att.get("file_id")
        if file_id is None:
            file_id = ""
        else:
            file_id = ext_str("attachment file_id", file_id)
        part = f"{attachment_type}"
        if file_name:
            part += f":{file_name}"
        if file_id:
            part += f"({file_id})"
        parts.append(part)
    return parts

def attachment_summary_text(attachments, limit=5):
    return ", ".join(attachment_summary_parts(attachments, limit=limit))

class PrototypeAttachments:
    def _attachment_str(self, attachment, *keys):
        return attachment_str(attachment, *keys)

    def _attachment_display_name(self, attachment, *, local_path=""):
        return attachment_display_name(attachment, local_path=local_path)

    def _attachment_content_type(self, attachment):
        return attachment_content_type(attachment)

    def _is_image_attachment(self, attachment, *, local_path=""):
        return is_image_attachment(attachment, local_path=local_path)

    def _is_audio_attachment(self, attachment, *, local_path=""):
        return is_audio_attachment(attachment, local_path=local_path)

    def _is_pdf_attachment(self, attachment, *, local_path=""):
        return is_pdf_attachment(attachment, local_path=local_path)

    async def _materialize_attachments(self, attachments):
        items = ext_list("attachments", attachments)
        out = []
        for attachment in items:
            item = dict(validate_attachment(attachment))
            local_path = self._attachment_str(item, "local_path")
            if local_path and os.path.exists(local_path):
                item["local_path"] = local_path
                out.append(item)
                continue
            file_id = item.get("file_id")
            if file_id:
                try:
                    meta = await self.download_chat_file(file_id=file_id)
                    if meta:
                        item["local_path"] = self.resolve_file_id(file_id)
                        size_bytes = meta.get("size_bytes")
                        if (
                            size_bytes is not None
                            and item.get("size_bytes") is None
                            and item.get("file_size") is None
                        ):
                            item["size_bytes"] = size_bytes
                except Exception:
                    logger.exception("failed to download chat attachment file_id=%s", file_id)
            out.append(item)
        return out
