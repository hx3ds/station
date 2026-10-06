from dataclasses import dataclass, field

from station import logger
from station.prototypes.boundary import ext_str
from station.prototypes.bridge_worker import PrototypeBridgeWorker
from station.prototypes.chat_history import load_session_record, save_session_id

from .client import create_cursor_agent, cursor_user_message, resume_cursor_agent


@dataclass(slots=True)
class PendingTurnMessage:
    message: str
    image_paths: list = field(default_factory=list)
    reply_with_voice: bool = False


class CursorWorker(PrototypeBridgeWorker):
    def _bridge_worker_error_text(self, error):
        return "Cursor bridge error: %s" % error

    async def _enqueue_turn(self, *, message, image_paths, chat_id, acct_id, platform="", chat_type="", reply_with_voice=False):
        state = await self._get_chat_state(acct_id=acct_id, chat_id=chat_id)
        gateway = await self._ensure_gateway()
        await self._enqueue_pending(
            state=state,
            gateway=gateway,
            chat_id=chat_id,
            acct_id=acct_id,
            item=PendingTurnMessage(message=message, image_paths=list(image_paths or []), reply_with_voice=reply_with_voice),
            platform=platform,
            chat_type=chat_type,
        )

    async def _bridge_worker_loop(self, *, gateway, state, chat_id, acct_id):
        while True:
            payload = await self._pop_batched_turn(state=state)
            if payload is None:
                return

            message, image_paths, reply_with_voice = payload
            state.busy = True
            try:
                reply = await self._run_turn(
                    gateway=gateway,
                    state=state,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    message=message,
                    image_paths=image_paths,
                )
            finally:
                state.busy = False

            if reply:
                await self._deliver_turn_reply(
                    text=reply,
                    include_voice=reply_with_voice,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    platform=state.platform,
                    chat_type=state.chat_type,
                )

    async def _pop_batched_turn(self, *, state):
        def merge_many(messages):
            text_parts = []
            image_paths = []
            reply_with_voice = False
            for index, item in enumerate(messages, start=1):
                text_parts.append(self._queued_followup_label(index, item.message))
                image_paths.extend(item.image_paths)
                reply_with_voice = reply_with_voice or item.reply_with_voice
            return "\n\n".join(text_parts), image_paths, reply_with_voice

        return await self._pop_batched_items(
            state=state,
            merge_one=lambda message: (message.message, list(message.image_paths), message.reply_with_voice),
            merge_many=merge_many,
        )

    async def _run_turn(self, *, gateway, state, chat_id, acct_id, message, image_paths):
        from cursor_sdk import CursorAgentError

        settings = self._launch_settings()
        if not settings.api_key:
            return self._secret_prompt()
        try:
            agent = await self._ensure_chat_agent(
                gateway=gateway,
                state=state,
                settings=settings,
                chat_id=chat_id,
                acct_id=acct_id,
            )
            payload = cursor_user_message(text=message, image_paths=image_paths)
            run = await agent.send(payload)
            logger.info(
                "Cursor run started model_id=%s acct_id=%s chat_id=%s agent_id=%s run_id=%s",
                self.model_id,
                acct_id,
                chat_id,
                agent.agent_id,
                run.id,
            )
            result = await run.wait()
        except CursorAgentError as e:
            logger.error(
                "CursorAgentError model_id=%s acct_id=%s chat_id=%s retryable=%s status=%s request_id=%s error=%s",
                self.model_id,
                acct_id,
                chat_id,
                e.is_retryable,
                e.status,
                e.request_id,
                e,
            )
            return "Cursor bridge error: %s" % e

        status = ext_str("cursor run status", result.status)
        if status == "finished":
            text = ext_str("cursor run result", result.result, strip=False).strip()
            return text or "Cursor turn finished."
        if status == "error":
            return "Cursor run failed."
        return "Cursor run %s." % status

    async def _ensure_chat_agent(self, *, gateway, state, settings, chat_id, acct_id):
        from cursor_sdk import CursorAgentError

        key = gateway.chat_key(acct_id, chat_id)
        agent = gateway.agents.get(key)
        if agent is not None:
            return agent

        agent_id = state.session_id or self._load_agent_id(acct_id=acct_id, chat_id=chat_id, workspace=settings.workspace_dir)
        if agent_id:
            try:
                agent = await resume_cursor_agent(gateway=gateway, settings=settings, agent_id=agent_id)
            except CursorAgentError as e:
                logger.info(
                    "Cursor resume failed model_id=%s acct_id=%s chat_id=%s agent_id=%s error=%s",
                    self.model_id,
                    acct_id,
                    chat_id,
                    agent_id,
                    e,
                )
                agent = None

        if agent is None:
            agent = await create_cursor_agent(
                gateway=gateway,
                settings=settings,
                acct_id=acct_id,
                chat_id=chat_id,
            )

        gateway.agents[key] = agent
        state.session_id = agent.agent_id
        self._save_agent_id(
            acct_id=acct_id,
            chat_id=chat_id,
            agent_id=agent.agent_id,
            workspace=settings.workspace_dir,
        )
        return agent

    def _load_agent_id(self, *, acct_id, chat_id, workspace):
        record = load_session_record(self.storage_dir, acct_id, chat_id)
        agent_id = ext_str("chat session_id", record.get("session_id"))
        stored_workspace = ext_str("chat workspace", record.get("workspace"))
        if not agent_id or stored_workspace != workspace:
            return ""
        return agent_id

    def _save_agent_id(self, *, acct_id, chat_id, agent_id, workspace):
        save_session_id(self.storage_dir, acct_id, chat_id, agent_id, workspace=workspace)

    async def _drop_backend_session(self, *, session_id, chat_id, acct_id):
        gateway = self._gateway
        if gateway is None:
            return
        key = gateway.chat_key(acct_id, chat_id)
        agent = gateway.agents.pop(key, None)
        if agent is None:
            return
        try:
            await agent.close()
        except Exception as e:
            logger.error(
                "unexpected where=cursor_agent_drop model_id=%s acct_id=%s chat_id=%s error=%s",
                self.model_id,
                acct_id,
                chat_id,
                e,
                exc_info=e,
            )
