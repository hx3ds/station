import asyncio
import os
import shutil
import uuid

from station import logger
from station.prototypes.attachments import attachment_str
from station.prototypes.fs_paths import sanitize_path_component

MIME_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "application/pdf": ".pdf",
}

class WorkspaceAttachmentStaging:
    WORKSPACE_ATTACHMENT_DIR = ".station_attachments"
    ATTACHMENT_COPY_LABEL = "attachment"

    def _workspace_root(self):
        if self._settings is not None:
            return os.path.expanduser(self._settings.workspace_dir)
        return self.storage_dir

    def _attachment_manifest_line(self, attachment):
        name = attachment_str(attachment, "file_name", "title", "file_id") or "attachment"
        pieces = [name]
        attachment_type = attachment_str(attachment, "type")
        if attachment_type:
            pieces.append("type=%s" % attachment_type)
        mime_type = attachment_str(attachment, "mime_type", "content_type")
        if mime_type:
            pieces.append("mime=%s" % mime_type)
        workspace_path = attachment_str(attachment, "workspace_path")
        if workspace_path:
            pieces.append("path=%s" % workspace_path)
        return "- " + " ".join(pieces)

    def _attachment_manifest_text(self, manifest_lines, *, product_name):
        return (
            "Attachments received.\n"
            "Non-image attachments have been copied into the %s workspace. "
            "Inspect them from these paths if needed:\n" % product_name
            + "\n".join(manifest_lines)
        )

    async def _stage_attachment(self, *, attachment, acct_id, chat_id):
        local_path = attachment_str(attachment, "local_path")
        if not local_path:
            return ""

        workspace_root = self._workspace_root()
        target_dir = os.path.join(
            workspace_root,
            self.WORKSPACE_ATTACHMENT_DIR,
            sanitize_path_component(acct_id),
            sanitize_path_component(chat_id),
        )
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(
            target_dir,
            self._build_workspace_attachment_name(attachment=attachment, source_path=local_path),
        )
        try:
            await asyncio.to_thread(shutil.copy2, local_path, target_path)
        except OSError as e:
            logger.error(
                "%s copy failed model_id=%s source=%s target=%s error=%s",
                self.ATTACHMENT_COPY_LABEL,
                self.model_id,
                local_path,
                target_path,
                e,
            )
            return ""
        if target_path.startswith(workspace_root + os.sep):
            attachment["workspace_path"] = os.path.relpath(target_path, workspace_root)
        else:
            attachment["workspace_path"] = target_path
        return target_path

    async def _stage_attachments(self, attachments, *, acct_id, chat_id):
        staged = []
        for attachment in attachments:
            if not attachment_str(attachment, "local_path"):
                continue
            path = await self._stage_attachment(
                attachment=attachment,
                acct_id=acct_id,
                chat_id=chat_id,
            )
            if path:
                staged.append((attachment, path))
        return staged

    def _build_workspace_attachment_name(self, *, attachment, source_path):
        original_name = attachment_str(attachment, "file_name", "title")
        mime = attachment_str(attachment, "mime_type", "content_type").lower()
        suffix = (
            os.path.splitext(original_name)[1]
            or os.path.splitext(source_path)[1]
            or MIME_SUFFIXES.get(mime, "")
        )
        if not suffix and self._is_image_attachment(attachment, local_path=source_path):
            suffix = ".png"
        stem_source = original_name or os.path.basename(source_path)
        stem = sanitize_path_component(os.path.splitext(stem_source)[0])
        return "%s_%s%s" % (stem, uuid.uuid4().hex[:8], suffix)
