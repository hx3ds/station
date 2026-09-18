from station import logger
from station.prototypes.boundary import validate_inbound_message_fields

class PrototypeEcho:
    async def echo_inbound(self, data, echo_file=False, chat_id=None, acct_id=None, request_id=None):
        if not data:
            return False

        fields = validate_inbound_message_fields(data)
        text = fields["inbound_text"]
        attachments = self._message_attachments(data)

        if not chat_id:
            return False

        if not text and attachments:
            text = "[File Received]"
        if not text and not attachments and data.get("raw") is not None:
            text = "[Unsupported Message]"
        if not text and not attachments:
            return False

        if not self.client_context:
            logger.error("unexpected where=echo_inbound error=missing_client_context")
            return False

        reply_to = ""
        if self.prototype_config.reply_to and fields["msg_id"] is not None:
            reply_to = fields["msg_id"]

        keyboard = None
        if (text or "").strip() == "show keyboard":
            keyboard = [[{"text": "Yes", "id": "kb_yes"}, {"text": "No", "id": "kb_no"}]]

        return await self.send_outbound(
            text="Echo: " + text,
            attachments=attachments if echo_file else None,
            reply_to=reply_to,
            chat_id=chat_id,
            request_id=request_id if request_id else None,
            acct_id=acct_id,
            platform=fields["platform"],
            chat_type=fields["chat_type"],
            keyboard=keyboard,
        )
