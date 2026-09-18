from dataclasses import dataclass, field
from pathlib import Path

from station import logger
from station.prototypes.boundary import ext_mapping_get

@dataclass(slots=True)
class NativeAttachmentContext:
    summary_lines: list = field(default_factory=list)
    file_refs: list = field(default_factory=list)
    fallback_attachments: list = field(default_factory=list)

class HermesAttachments:
    async def _attach_attachments_to_hermes(self, *, gateway, session_id, attachments):
        context = NativeAttachmentContext()
        for attachment in attachments:
            local_path = self._attachment_str(attachment, "local_path")
            display_name = self._attachment_display_name(attachment, local_path=local_path)
            if not local_path or not Path(local_path).exists():
                context.fallback_attachments.append(attachment)
                continue
            try:
                if self._is_pdf_attachment(attachment, local_path=local_path):
                    result = await gateway.attach_pdf(session_id=session_id, path=local_path)
                    pages = ext_mapping_get(result, "pages_attached", (int,), 0)
                    suffix = " (%d page(s))" % pages if pages else ""
                    context.summary_lines.append("- pdf: %s%s" % (display_name, suffix))
                    continue
                if self._is_image_attachment(attachment, local_path=local_path):
                    if self._launch_settings().uses_local_llm():
                        context.summary_lines.append("- image: %s" % display_name)
                        continue
                    await gateway.attach_image(session_id=session_id, path=local_path)
                    context.summary_lines.append("- image: %s" % display_name)
                    continue
                result = await gateway.attach_file(session_id=session_id, path=local_path, name=display_name)
                ref_text = ext_mapping_get(result, "ref_text", (str,), "").strip()
                if ref_text:
                    context.file_refs.append(ref_text)
                    context.summary_lines.append("- file: %s" % display_name)
                else:
                    context.fallback_attachments.append(attachment)
            except Exception:
                logger.exception(
                    "Hermes native attachment failed model_id=%s attachment=%s",
                    self.model_id,
                    display_name,
                )
                context.fallback_attachments.append(attachment)
        return context

    def _build_prompt_text(self, *, text, native_attachments):
        parts = []
        if text:
            parts.append(text)
        elif native_attachments.summary_lines or native_attachments.fallback_attachments:
            parts.append("The user sent attachments without additional text. Please inspect the attached content.")

        if native_attachments.summary_lines:
            parts.append("[attached]\n" + "\n".join(native_attachments.summary_lines))
        if native_attachments.file_refs:
            parts.append("[file_refs]\n" + "\n".join(native_attachments.file_refs))

        fallback_lines = []
        for attachment in native_attachments.fallback_attachments:
            name = self._attachment_display_name(
                attachment,
                local_path=self._attachment_str(attachment, "local_path"),
            )
            path = self._attachment_str(attachment, "local_path")
            if path:
                fallback_lines.append("- %s path=%s" % (name, path))
            else:
                fallback_lines.append("- %s" % name)
        if fallback_lines:
            parts.append("[attachments]\n" + "\n".join(fallback_lines))

        return "\n\n".join(parts)

    def _build_batch_prompt_text(self, *, batch, native_attachments):
        if len(batch) == 1:
            return self._build_prompt_text(
                text=batch[0].combined_text,
                native_attachments=native_attachments,
            )

        sections = []
        for index, message in enumerate(batch, start=1):
            sections.append(
                self._queued_followup_label(
                    index,
                    message.combined_text,
                    empty="The user sent attachments without additional text.",
                )
            )
        return self._build_prompt_text(
            text="\n\n".join(sections),
            native_attachments=native_attachments,
        )
