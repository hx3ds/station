from station import logger
from station.prototypes.boundary import ext_mapping_get

CURATED_DISPATCH_SLASH_COMMANDS = frozenset({"goal", "learn", "queue", "retry", "steer", "undo"})
CURATED_EXEC_SLASH_COMMANDS = frozenset(
    {"agents", "commands", "config", "help", "plugins", "skills", "status", "tools"}
)
CURATED_SLASH_COMMANDS = CURATED_DISPATCH_SLASH_COMMANDS | CURATED_EXEC_SLASH_COMMANDS

class HermesSlash:
    def _parse_slash_command(self, text):
        stripped = text.strip()
        if not stripped.startswith("/"):
            return None
        body = stripped[1:].strip()
        if not body:
            return None
        name, _, arg = body.partition(" ")
        return name.strip().lower(), arg.strip()

    async def _resolve_curated_slash_name(self, *, gateway, name):
        try:
            result = await gateway.resolve_command(name=name)
        except Exception:
            logger.info("Hermes slash resolve failed model_id=%s name=%s", self.model_id, name, exc_info=True)
            return name
        canonical = ext_mapping_get(result, "canonical", (str,), "").strip().lower()
        return canonical or name

    async def _outbound(self, *, state, chat_id, acct_id, text):
        await self.send_outbound(
            text=text,
            chat_id=chat_id,
            acct_id=acct_id,
            platform=state.platform,
            chat_type=state.chat_type,
        )

    async def _handle_curated_slash_command(self, *, gateway, state, chat_id, acct_id, text, attachments):
        parsed = self._parse_slash_command(text)
        if parsed is None:
            return
        raw_name, arg = parsed
        canonical_name = await self._resolve_curated_slash_name(gateway=gateway, name=raw_name)

        if canonical_name not in CURATED_SLASH_COMMANDS:
            allowed = ", ".join("/%s" % name for name in sorted(CURATED_SLASH_COMMANDS))
            await self._outbound(
                state=state,
                chat_id=chat_id,
                acct_id=acct_id,
                text="Hermes bridge only allows curated slash commands here.\nAllowed: %s" % allowed,
            )
            return

        if attachments:
            await self._outbound(
                state=state,
                chat_id=chat_id,
                acct_id=acct_id,
                text="Slash commands do not accept attachments in Hermes bridge. Send the command by itself.",
            )
            return

        if canonical_name in CURATED_DISPATCH_SLASH_COMMANDS:
            result = await gateway.dispatch_command(
                session_id=state.session_id,
                name=canonical_name,
                arg=arg,
            )
            await self._handle_dispatch_slash_result(
                gateway=gateway,
                state=state,
                chat_id=chat_id,
                acct_id=acct_id,
                command_name=canonical_name,
                result=result,
                alias_depth=0,
            )
            return

        slash_text = "/%s" % canonical_name
        if arg:
            slash_text = "%s %s" % (slash_text, arg)
        result = await gateway.exec_slash(session_id=state.session_id, command=slash_text)
        warning = ext_mapping_get(result, "warning", (str,), "").strip()
        output = ext_mapping_get(result, "output", (str,), "").strip()
        if warning:
            await self._outbound(state=state, chat_id=chat_id, acct_id=acct_id, text=warning)
        if output:
            await self._outbound(state=state, chat_id=chat_id, acct_id=acct_id, text=output)

    async def _handle_dispatch_slash_result(
        self,
        *,
        gateway,
        state,
        chat_id,
        acct_id,
        command_name,
        result,
        alias_depth,
    ):

        result_type = ext_mapping_get(result, "type", (str,), "").strip().lower()
        notice = ext_mapping_get(result, "notice", (str,), "").strip()

        if result_type in {"send", "skill"}:
            if notice:
                await self._outbound(state=state, chat_id=chat_id, acct_id=acct_id, text=notice)
            message = ext_mapping_get(result, "message", (str,), "").strip()
            if not message:
                raise RuntimeError("Hermes slash command %s returned no message" % command_name)
            state.busy = True
            state.reply_with_voice = False
            await gateway.submit_prompt(session_id=state.session_id, text=message)
            await self._drain_session_events(gateway=gateway, state=state, chat_id=chat_id, acct_id=acct_id)
            return

        if result_type in {"exec", "plugin"}:
            if notice:
                await self._outbound(state=state, chat_id=chat_id, acct_id=acct_id, text=notice)
            output = ext_mapping_get(result, "output", (str,), "").strip() or ("/%s completed." % command_name)
            await self._outbound(state=state, chat_id=chat_id, acct_id=acct_id, text=output)
            return

        if result_type == "prefill":
            draft = ext_mapping_get(result, "message", (str,), "").strip()
            text_parts = []
            if notice:
                text_parts.append(notice)
            if draft:
                text_parts.append("Draft:\n%s" % draft)
            await self._outbound(
                state=state,
                chat_id=chat_id,
                acct_id=acct_id,
                text="\n\n".join(text_parts) if text_parts else ("/%s completed." % command_name),
            )
            return

        if result_type == "alias":
            if notice:
                await self._outbound(state=state, chat_id=chat_id, acct_id=acct_id, text=notice)
            if alias_depth >= 3:
                raise RuntimeError("Hermes slash alias loop for /%s" % command_name)
            target = ext_mapping_get(result, "target", (str,), "").strip()
            parsed = self._parse_slash_command("/%s" % target)
            if parsed is None:
                raise RuntimeError("Hermes slash alias target is invalid: %s" % target)
            target_name, target_arg = parsed
            canonical_target = await self._resolve_curated_slash_name(gateway=gateway, name=target_name)
            if canonical_target not in CURATED_DISPATCH_SLASH_COMMANDS:
                raise RuntimeError("Hermes slash alias target is not allowed: /%s" % canonical_target)
            alias_result = await gateway.dispatch_command(
                session_id=state.session_id,
                name=canonical_target,
                arg=target_arg,
            )
            await self._handle_dispatch_slash_result(
                gateway=gateway,
                state=state,
                chat_id=chat_id,
                acct_id=acct_id,
                command_name=canonical_target,
                result=alias_result,
                alias_depth=alias_depth + 1,
            )
            return

        if notice:
            await self._outbound(state=state, chat_id=chat_id, acct_id=acct_id, text=notice)
        raise RuntimeError(
            "Hermes slash command /%s returned unsupported result type: %s"
            % (command_name, result_type or "unknown")
        )
