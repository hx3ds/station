import json
from dataclasses import dataclass
from pathlib import Path

from station import logger
from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_str
from station.prototypes.device_auth import PrototypeDeviceAuth, format_device_login_start, local_llm_auth_reply

from .config import DEVICE_CODE_PROVIDERS, XAI_PROVIDERS, hermes_home_path

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


DEVICE_CODE_LOGINS = (
    DeviceCodeLoginOption(id="nous", label="Nous Portal", provider="nous", aliases=("nous",)),
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
    ),
)


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


class HermesAuthFlow(PrototypeDeviceAuth):
    def _init_auth_flow(self):
        self._init_device_auth()
        self._provider_ready = None

    def _hermes_home_path(self):
        if self._hermes_home is not None:
            return Path(self._hermes_home)
        return hermes_home_path(self.storage_dir)

    def _has_cloud_api_key(self, settings):
        for key in CLOUD_API_ENV_KEYS:
            if (settings.extra_env.get(key) or "").strip():
                return True
        return False

    async def _provider_is_ready(self):
        settings = self._launch_settings()
        if settings.uses_local_llm() or self._has_cloud_api_key(settings):
            return True
        provider = (settings.provider or "").strip().lower()
        if provider and provider not in DEVICE_CODE_PROVIDERS and provider not in XAI_PROVIDERS:
            return False
        if self._provider_ready is not None:
            return self._provider_ready
        gateway = await self._ensure_gateway()
        try:
            result = await gateway.request("auth.status", {"provider": settings.gateway_provider()})
        except RuntimeError as e:
            logger.error("Hermes auth.status failed model_id=%s error=%s", self.model_id, e)
            return False
        self._provider_ready = bool((result or {}).get("logged_in"))
        return self._provider_ready

    def _is_missing_provider_error(self, message):
        text = (message or "").lower()
        return "no llm provider configured" in text

    def _default_login_option(self, settings):
        if settings.uses_xai():
            return LOGIN_CHOICE_MAP["xai"]
        provider = (settings.provider or "").strip().lower()
        if not provider:
            return None
        return LOGIN_CHOICE_MAP.get(provider)

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

    async def _before_teardown_gateway(self):
        await self._cancel_device_auth()
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
            await self.send_outbound(
                text=local_llm_auth_reply(
                    "Hermes",
                    logout=cmd == "/logout",
                    base_url=settings.local_llm_base_url,
                    provider=settings.gateway_provider(),
                    model=settings.model or settings.provider,
                ),
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
        if choice is None and not (args or "").strip():
            choice = self._default_login_option(settings)
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

    def _strip_saved_provider(self):
        path = self._hermes_home_path() / "config.yaml"
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raise ExternalError("hermes config is not valid JSON")
        data = ext_dict("hermes config", data)
        model = data.get("model")
        if model is not None:
            model = ext_dict("hermes config model", model)
            model.pop("provider", None)
            model.pop("default", None)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def _logout(self, *, chat_id, acct_id, reply_to=None, platform="", chat_type=""):
        await self._cancel_device_auth(acct_id=acct_id)
        gateway = await self._ensure_gateway()
        await gateway.request("auth.clear", {})
        self._strip_saved_provider()
        self._settings = None
        self._provider_ready = False
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
        if await self._provider_is_ready():
            return True
        settings = self._launch_settings()
        choice = self._default_login_option(settings)
        if choice is None:
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
        reply = await self._start_device_login(
            option,
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

    async def _run_device_login_op(self, op, option, pending=None):
        gateway = await self._ensure_gateway()
        method = "auth.begin" if op == "auth_begin" else "auth.poll"
        params = {"provider": option.provider}
        if pending is not None:
            params["pending"] = pending
        result = await gateway.request(method, params)
        if result is None:
            return {}
        return result

    async def _start_device_login(self, option, *, acct_id, chat_id="", reply_to=None, platform="", chat_type=""):
        name = "Hermes %s" % option.label

        async def begin():
            return await self._run_device_login_op("auth_begin", option)

        async def poll(pending):
            return await self._run_device_login_op("auth_poll", option, pending=pending)

        async def on_success(result):
            model = ext_str("model", result.get("model"))
            settings = self._launch_settings()
            settings.provider = option.provider
            settings.model = model or settings.model
            self._write_hermes_config(hermes_home=self._hermes_home_path(), settings=settings)
            self._settings = None
            self._provider_ready = True
            try:
                await self._reload_gateway_after_login()
            except Exception:
                logger.exception(
                    "Hermes gateway restart after %s login failed acct_id=%s",
                    option.label,
                    acct_id,
                )
            if chat_id:
                text = "Signed in to %s. Send a message to start." % option.label
                if model:
                    text = "Signed in to %s (%s). Send a message to start." % (option.label, model)
                await self.send_outbound(
                    text=text,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type,
                )

        def start_message(pending):
            return format_device_login_start(
                title="Sign in to %s (device login):" % option.label,
                verification_uri=ext_str("verification_uri", pending.get("verification_uri")),
                user_code=ext_str("user_code", pending.get("user_code")),
                verification_uri_complete=ext_str(
                    "verification_uri_complete",
                    pending.get("verification_uri_complete"),
                ),
            )

        return await self._run_device_auth(
            acct_id=acct_id,
            chat_id=chat_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
            name=name,
            begin=begin,
            poll=poll,
            on_success=on_success,
            start_message=start_message,
        )
