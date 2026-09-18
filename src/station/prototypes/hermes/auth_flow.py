import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from station import logger
from station.prototypes.grok.oauth import begin_device_login, poll_device_code_token
from station.prototypes.launch_settings import LOCAL_LLM_PROVIDERS

from . import auth_store
from .config import XAI_DEFAULT_MODEL, XAI_PROVIDERS, xai_model_or_default

CLOUD_API_ENV_KEYS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "XAI_API_KEY",
    "MISTRAL_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
)


@dataclass(frozen=True, slots=True)
class DeviceCodeLoginOption:
    id: str
    label: str
    provider: str
    aliases: tuple
    default_model: str = ""
    implemented: bool = False


DEVICE_CODE_LOGINS = (
    DeviceCodeLoginOption(
        id="nous",
        label="Nous Portal",
        provider="nous",
        aliases=("nous",),
    ),
    DeviceCodeLoginOption(
        id="openai-codex",
        label="ChatGPT / Codex",
        provider="openai-codex",
        aliases=("openai-codex", "codex", "chatgpt", "openai"),
    ),
    DeviceCodeLoginOption(
        id="minimax",
        label="MiniMax",
        provider="minimax-oauth",
        aliases=("minimax", "minimax-oauth"),
    ),
    DeviceCodeLoginOption(
        id="xai",
        label="xAI / Grok",
        provider="xai-oauth",
        aliases=("xai", "xai-oauth", "grok", "grok-oauth"),
        default_model=XAI_DEFAULT_MODEL,
        implemented=True,
    ),
)


@dataclass(slots=True)
class HermesAuthState:
    pending: object = None
    poll_task: object = None
    option_id: str = ""
    reply_to: object = None
    guard: asyncio.Lock = field(default_factory=asyncio.Lock)


def _login_index_map():
    mapping = {}
    for index, option in enumerate(DEVICE_CODE_LOGINS, start=1):
        mapping[str(index)] = option
        mapping[option.id] = option
        mapping[option.provider] = option
        mapping[option.label.lower()] = option
        for alias in option.aliases:
            mapping[alias.lower()] = option
    return mapping


LOGIN_CHOICE_MAP = _login_index_map()


class HermesAuthFlow:
    def _init_auth_flow(self):
        self._auth_states = {}
        self._auth_states_guard = asyncio.Lock()

    def _hermes_home_path(self):
        if self._hermes_home is not None:
            return Path(self._hermes_home)
        return Path(self.storage_dir).resolve() / "hermes_home"

    def _has_cloud_api_key(self, settings):
        for key in CLOUD_API_ENV_KEYS:
            if (settings.extra_env.get(key) or "").strip():
                return True
        return False

    def _has_usable_inference(self, settings):
        if settings.uses_xai() or (settings.provider or "").strip().lower() in XAI_PROVIDERS:
            return self._has_xai_credentials(settings)
        if self._has_xai_oauth_tokens():
            return True
        if settings.local_llm_base_url:
            return True
        provider = (settings.provider or "").strip().lower()
        if not provider or provider in LOCAL_LLM_PROVIDERS:
            return False
        return self._has_cloud_api_key(settings)

    def _is_missing_provider_error(self, message):
        text = (message or "").lower()
        return "no llm provider configured" in text

    def _provider_is_configured(self, settings):
        return self._has_usable_inference(settings)

    def _has_xai_oauth_tokens(self):
        return auth_store.load_xai_credentials(self._hermes_home_path()) is not None

    def _has_xai_credentials(self, settings=None):
        if settings is None:
            settings = self._launch_settings()
        if (settings.extra_env.get("XAI_API_KEY") or "").strip():
            return True
        return self._has_xai_oauth_tokens()

    def _login_picker_keyboard(self):
        return [
            [{"text": option.label, "id": "/login %s" % option.id}]
            for option in DEVICE_CODE_LOGINS
        ]

    def _login_picker_text(self):
        lines = [
            "No inference provider is configured.",
            "",
            "Hermes supports these device-code logins:",
        ]
        for index, option in enumerate(DEVICE_CODE_LOGINS, start=1):
            lines.append("%d. %s" % (index, option.label))
        lines.extend(
            [
                "",
                "Choose a provider to sign in.",
                "Commands: /login · /logout",
            ]
        )
        return "\n".join(lines)

    def _resolve_device_login_choice(self, text):
        raw = (text or "").strip()
        if not raw:
            return None
        if raw.startswith("/"):
            cmd, args = self._slash_command(raw)
            if cmd == "/login":
                raw = args.strip()
            else:
                return None
        if not raw:
            return None
        return LOGIN_CHOICE_MAP.get(raw.lower())

    async def _get_auth_state(self, acct_id):
        async with self._auth_states_guard:
            state = self._auth_states.get(acct_id)
            if state is None:
                state = HermesAuthState()
                self._auth_states[acct_id] = state
            return state

    async def _await_cancelled_task(self, task):
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _cancel_auth_login(self, *, acct_id=None):
        if acct_id is None:
            async with self._auth_states_guard:
                states = list(self._auth_states.values())
        else:
            states = [await self._get_auth_state(acct_id)]
        for state in states:
            async with state.guard:
                old_task = state.poll_task
                state.poll_task = None
                state.pending = None
                state.option_id = ""
                state.reply_to = None
            await self._await_cancelled_task(old_task)

    async def _before_teardown_gateway(self):
        await self._cancel_auth_login()
        await super()._before_teardown_gateway()

    async def _reload_gateway_after_login(self):
        await self._restart_gateway()

    async def _send_login_picker(self, *, chat_id, acct_id, reply_to=None, platform="", chat_type=""):
        await self.send_outbound(
            text=self._login_picker_text(),
            chat_id=chat_id,
            acct_id=acct_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
            keyboard=self._login_picker_keyboard(),
        )

    async def _handle_auth_command(self, *, cmd, args="", chat_id, acct_id, reply_to=None, platform="", chat_type=""):
        settings = self._launch_settings()
        if settings.uses_local_llm():
            if cmd == "/logout":
                text = "Hermes local LLM uses an API key. There is no cloud session to sign out of."
            else:
                text = (
                    "Hermes is using a local LLM at %s (provider %s, model %s). No cloud login is required."
                    % (
                        settings.local_llm_base_url or "LOCAL_LLM_BASE_URL",
                        settings.gateway_provider(),
                        settings.model or settings.provider,
                    )
                )
            await self.send_outbound(
                text=text,
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return
        if cmd == "/logout":
            await self._logout(
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return
        choice = self._resolve_device_login_choice(args)
        if choice is None and not (args or "").strip() and settings.uses_xai():
            choice = LOGIN_CHOICE_MAP["xai"]
        if choice is None:
            await self._send_login_picker(
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return
        await self._start_provider_login(
            choice,
            acct_id=acct_id,
            chat_id=chat_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
        )

    async def _logout(self, *, chat_id, acct_id, reply_to=None, platform="", chat_type=""):
        await self._cancel_auth_login(acct_id=acct_id)
        auth_store.delete_selected_provider(self.storage_dir)
        auth_store.delete_device_login_credentials(self._hermes_home_path())
        self._settings = None
        logger.info("Hermes device login removed acct_id=%s", acct_id)
        await self.send_outbound(
            text="Signed out of Hermes device login. Send /login to choose a provider.",
            chat_id=chat_id,
            acct_id=acct_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
        )

    async def _ensure_provider_auth(self, *, acct_id, chat_id, text="", reply_to=None, platform="", chat_type=""):
        settings = self._launch_settings()
        if self._has_usable_inference(settings):
            return True
        if settings.uses_xai():
            await self._start_provider_login(
                LOGIN_CHOICE_MAP["xai"],
                acct_id=acct_id,
                chat_id=chat_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return False
        choice = self._resolve_device_login_choice(text)
        if choice is not None:
            await self._start_provider_login(
                choice,
                acct_id=acct_id,
                chat_id=chat_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return False
        await self._send_login_picker(
            chat_id=chat_id,
            acct_id=acct_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
        )
        return False

    async def _start_provider_login(self, option, *, acct_id, chat_id="", reply_to=None, platform="", chat_type=""):
        if not option.implemented:
            await self.send_outbound(
                text=(
                    "Station's Hermes bridge currently supports xAI / Grok device login.\n"
                    "For %s, set `[model].provider = \"%s\"` or an API key, then send another message."
                    % (option.label, option.provider)
                ),
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return
        reply = await self._start_xai_login(
            acct_id=acct_id,
            chat_id=chat_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
        )
        await self.send_outbound(
            text=reply,
            chat_id=chat_id,
            acct_id=acct_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
        )

    async def _start_xai_login(self, *, acct_id, chat_id="", reply_to=None, platform="", chat_type=""):
        state = await self._get_auth_state(acct_id)
        async with state.guard:
            old_task = state.poll_task
            state.poll_task = None
            state.pending = None
            state.option_id = ""
            state.reply_to = None
        await self._await_cancelled_task(old_task)
        try:
            pending = await asyncio.to_thread(begin_device_login)
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            logger.error("Hermes xAI device OAuth login start failed acct_id=%s error=%s", acct_id, e)
            return "Hermes xAI OAuth login could not start: %s" % e
        async with state.guard:
            state.pending = pending
            state.option_id = "xai"
            state.reply_to = reply_to
            state.poll_task = asyncio.create_task(
                self._poll_xai_device_login(
                    acct_id=acct_id,
                    chat_id=chat_id,
                    pending=pending,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type,
                )
            )
        lines = [
            "Sign in to xAI / Grok (device login):",
            "Open %s on any device and enter code: %s" % (pending.verification_uri, pending.user_code),
        ]
        if pending.verification_uri_complete:
            lines.append("Or open: %s" % pending.verification_uri_complete)
        lines.extend(
            [
                "",
                "Waiting for authorization...",
                "Commands: /login · /logout",
            ]
        )
        return "\n".join(lines)

    async def _poll_xai_device_login(self, *, acct_id, chat_id, pending, reply_to=None, platform="", chat_type=""):
        try:
            creds = await asyncio.to_thread(poll_device_code_token, pending)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            logger.error("Hermes xAI device OAuth poll failed acct_id=%s error=%s", acct_id, e)
            state = await self._get_auth_state(acct_id)
            async with state.guard:
                if state.pending is pending:
                    state.pending = None
                    state.poll_task = None
                    state.option_id = ""
                    state.reply_to = None
            if chat_id:
                await self.send_outbound(
                    text="Hermes xAI OAuth failed: %s\nSend /login to try again." % e,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type,
                )
            return

        state = await self._get_auth_state(acct_id)
        async with state.guard:
            if state.pending is not pending:
                return
            hermes_home = self._hermes_home_path()
            auth_store.save_xai_credentials(hermes_home, creds)
            auth_store.save_selected_provider(
                self.storage_dir,
                provider="xai-oauth",
                model=self._xai_model_name(),
            )
            self._settings = None
            state.pending = None
            state.poll_task = None
            state.option_id = ""
            state.reply_to = None
        try:
            await self._reload_gateway_after_login()
        except Exception:
            logger.exception("Hermes gateway restart after xAI login failed acct_id=%s", acct_id)
        if chat_id:
            await self.send_outbound(
                text="Signed in to xAI / Grok. Send a message to start.",
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )

    def _xai_model_name(self):
        return xai_model_or_default(self._launch_settings().model)
