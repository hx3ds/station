import mimetypes
import os
from urllib.parse import quote

from station.prototypes.workspace_stage import WorkspaceAttachmentStaging

class OpenCodeAttachments(WorkspaceAttachmentStaging):
    WORKSPACE_ATTACHMENT_DIR = ".station_opencode_attachments"
    ATTACHMENT_COPY_LABEL = "OpenCode attachment"

    async def _build_prompt_parts(self, *, text, attachments, acct_id, chat_id):
        parts = []
        manifest_lines = []
        settings = self._launch_settings()
        skip_image_parts = settings.uses_local_llm()

        if text:
            parts.append({"type": "text", "text": text})

        for attachment in attachments:
            if not self._attachment_str(attachment, "local_path"):
                continue
            staged_path = await self._stage_attachment(
                attachment=attachment,
                acct_id=acct_id,
                chat_id=chat_id,
            )
            if not staged_path:
                continue
            image = self._is_image_attachment(attachment, local_path=staged_path)
            if image and skip_image_parts:
                manifest_lines.append(self._attachment_manifest_line(attachment))
                continue
            parts.append(self._file_part(attachment=attachment, local_path=staged_path))
            if not image:
                manifest_lines.append(self._attachment_manifest_line(attachment))

        if manifest_lines:
            manifest_text = self._attachment_manifest_text(manifest_lines, product_name="OpenCode")
            if parts and parts[0]["type"] == "text":
                parts[0]["text"] = ("%s\n\n%s" % (parts[0]["text"], manifest_text)).strip()
            else:
                parts.insert(0, {"type": "text", "text": manifest_text})

        return parts

    def _file_part(self, *, attachment, local_path):
        path = os.path.realpath(os.path.expanduser(local_path))
        mime = self._attachment_str(attachment, "mime_type", "content_type")
        if not mime:
            guessed, _ = mimetypes.guess_type(path)
            mime = guessed or "application/octet-stream"
        filename = self._attachment_str(attachment, "file_name", "title") or os.path.basename(path)
        return {
            "type": "file",
            "mime": mime,
            "filename": filename,
            "url": "file://%s" % quote(path),
            "source": {
                "type": "file",
                "path": path,
                "text": {"value": filename, "start": 0, "end": max(len(filename), 1)},
            },
        }
