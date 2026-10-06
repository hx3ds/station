BASIC_COMMANDS = {"/help", "/restart", "/reset"}


class PrototypeCommands:
    def _kind_help_commands(self):
        return []

    def _settings_extra_lines(self):
        return []

    def _has_model_config(self):
        return hasattr(self, "_config_status_text")

    def _help_commands(self):
        items = [
            ("/help", "Show commands"),
            ("/start", "Start"),
            ("/restart", "Clear this chat and start fresh"),
        ]
        if self._has_model_config():
            items.extend(
                [
                    ("/settings", "Show model settings"),
                    ("/key", "Set API key"),
                    ("/model", "Set model"),
                    ("/provider", "Set provider"),
                    ("/base_url", "Set base URL"),
                    ("/config reset", "Clear saved overlay"),
                ]
            )
        items.extend(self._kind_help_commands())
        return items

    def _help_text(self):
        seen = set()
        lines = ["Commands:"]
        for cmd, desc in self._help_commands():
            if cmd in seen:
                continue
            seen.add(cmd)
            lines.append("%s - %s" % (cmd, desc))
        return "\n".join(lines)

    async def _handle_basic_command(
        self,
        *,
        cmd,
        args,
        chat_id,
        acct_id,
        reply_to,
        model_settings,
        platform="",
        chat_type="",
    ):
        if cmd not in BASIC_COMMANDS:
            return False
        if cmd in {"/restart", "/reset"}:
            await self._restart_chat(chat_id=chat_id, acct_id=acct_id)
            await self.send_outbound(
                text="Chat restarted." if cmd == "/restart" else "reset completed",
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type)
            return True
        await self.send_outbound(
            text=self._help_text(),
            chat_id=chat_id,
            acct_id=acct_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type)
        return True
