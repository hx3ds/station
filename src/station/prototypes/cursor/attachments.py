from station.prototypes.workspace_stage import WorkspaceAttachmentStaging


class CursorAttachments(WorkspaceAttachmentStaging):
    WORKSPACE_ATTACHMENT_DIR = ".station_cursor_attachments"
    ATTACHMENT_COPY_LABEL = "Cursor attachment"

    async def _build_prompt_payload(self, *, text, attachments, acct_id, chat_id):
        image_paths = []
        manifest_lines = []

        for attachment, staged_path in await self._stage_attachments(
            attachments,
            acct_id=acct_id,
            chat_id=chat_id,
        ):
            if self._is_image_attachment(attachment, local_path=staged_path):
                image_paths.append(staged_path)
                continue
            manifest_lines.append(self._attachment_manifest_line(attachment))

        message = (text or "").strip()
        if manifest_lines:
            manifest = self._attachment_manifest_text(manifest_lines, product_name="Cursor")
            message = ("%s\n\n%s" % (message, manifest)).strip() if message else manifest
        return message, image_paths
