import asyncio
import base64
from pathlib import Path

from station import logger
from station.prototypes.attachments import attachment_str, is_image_attachment
from station.prototypes.workspace_stage import WorkspaceAttachmentStaging

MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}

class PiAttachments(WorkspaceAttachmentStaging):
    WORKSPACE_ATTACHMENT_DIR = ".station_pi_attachments"
    ATTACHMENT_COPY_LABEL = "Pi attachment"

    async def _build_prompt_payload(self, *, text, attachments, acct_id, chat_id):
        images = []
        manifest_lines = []

        for attachment in attachments:
            local_path = attachment_str(attachment, "local_path")
            if not local_path:
                continue
            if is_image_attachment(attachment, local_path=local_path):
                image = await self._image_content(attachment=attachment, local_path=local_path)
                if image:
                    images.append(image)
                    continue
            staged = await self._stage_attachment(
                attachment=attachment,
                acct_id=acct_id,
                chat_id=chat_id,
            )
            if staged:
                manifest_lines.append(self._attachment_manifest_line(attachment))

        message = text.strip()
        if manifest_lines:
            manifest = self._attachment_manifest_text(manifest_lines, product_name="Pi")
            message = ("%s\n\n%s" % (message, manifest)).strip() if message else manifest
        if not message and images:
            message = "The user sent image attachment(s). Please inspect them."
        return message, images

    async def _image_content(self, *, attachment, local_path):
        path = Path(local_path)
        if not path.is_file():
            return None
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as e:
            logger.error(
                "Pi image read failed model_id=%s path=%s error=%s",
                self.model_id,
                local_path,
                e,
            )
            return None
        mime = attachment_str(attachment, "mime_type", "content_type").lower()
        if not mime.startswith("image/"):
            mime = MIME_BY_SUFFIX.get(path.suffix.lower(), "image/png")
        return {
            "type": "image",
            "data": base64.b64encode(data).decode("ascii"),
            "mimeType": mime,
        }
