import json
import os

from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_require, ext_str
from station.prototypes.fs_paths import write_atomic

OVERLAY_NAME = "model_overlay.json"
SECRET_DELETE_HINT = "Delete messages containing secrets after configuration completes."
CONFIG_POLICY_HINT = (
    "Configuration is optional. Prefer chat commands or messages. "
    "Sokoyuku settings are optional and not required. "
    "Do not set model keys in environment variables."
)
CONFIG_COMMANDS = {"/key", "/model", "/provider", "/base_url", "/config", "/settings"}


def overlay_path(storage_dir):
    return os.path.join(storage_dir, OVERLAY_NAME)


def load_overlay(storage_dir):
    path = overlay_path(storage_dir)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ExternalError("model overlay is not valid JSON")
    return ext_dict("model overlay", data)


def save_overlay(storage_dir, overlay):
    ext_require("model overlay", overlay, (dict,))
    write_atomic(overlay_path(storage_dir), json.dumps(overlay, indent=2, ensure_ascii=False) + "\n")


def clear_overlay(storage_dir):
    try:
        os.remove(overlay_path(storage_dir))
    except FileNotFoundError:
        pass


def overlay_put(overlay, path, value):
    parts = path.split(".")
    if not parts or not all(parts):
        raise ExternalError("invalid overlay path")
    cur = overlay
    for part in parts[:-1]:
        if part not in cur:
            cur[part] = {}
        nxt = cur[part]
        ext_require("overlay.%s" % part, nxt, (dict,))
        cur = nxt
    cur[parts[-1]] = value
    return overlay


def overlay_get(mapping, path):
    cur = mapping
    parts = path.split(".")
    for i, part in enumerate(parts):
        if part not in cur:
            return None
        cur = cur[part]
        if i < len(parts) - 1:
            ext_require("overlay.%s" % part, cur, (dict,))
    return cur


def looks_like_secret(text):
    value = ext_str("secret", text)
    if not value or " " in value or "\n" in value:
        return False
    if len(value) < 16:
        return False
    prefixes = ("sk-", "sk-or-", "xai-", "key_", "cursor_", "crsr_", "Bearer")
    return value.startswith(prefixes) or len(value) >= 32


def _redact(path, value):
    if value is None or value == "":
        return "(not set)"
    key = path.split(".")[-1].lower()
    if "key" in key or "token" in key or "password" in key or "secret" in key:
        return "(set)"
    return str(value)


class PrototypeModelConfig:
    def _config_overlay(self):
        return load_overlay(self.storage_dir)

    def _config_api_key_path(self):
        return "keys.api_key"

    def _config_model_path(self):
        return "model.model"

    def _config_provider_path(self):
        return "model.provider"

    def _config_base_url_path(self):
        return "local_llm.base_url"

    def _config_requires_key(self, settings):
        return False

    def _config_accepts_key_message(self, settings):
        return self._config_requires_key(settings)

    def _config_settings(self):
        return self._build_settings()

    def _awaiting_secret_keys(self):
        return self._awaiting_secret

    def _secret_prompt(self):
        return "\n".join(
            [
                "This model needs an API key.",
                "Send /key <api_key> or send the key as a message.",
                CONFIG_POLICY_HINT,
                SECRET_DELETE_HINT,
            ]
        )

    def _config_saved_reply(self, label, *, secret=False):
        if secret:
            return "%s saved. %s" % (label, SECRET_DELETE_HINT)
        return "%s saved." % label

    async def _after_overlay_change(self):
        self._settings = None
        await self._restart_gateway()

    async def _put_overlay_value(self, path, value):
        overlay = self._config_overlay()
        overlay_put(overlay, path, value)
        save_overlay(self.storage_dir, overlay)
        await self._after_overlay_change()

    async def _handle_model_config_command(
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
        if cmd not in CONFIG_COMMANDS:
            return False
        key = (acct_id, chat_id)
        if cmd == "/key":
            value = args.strip()
            if not value:
                await self.send_outbound(
                    text="Usage: /key <api_key>\n%s" % SECRET_DELETE_HINT,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type)
                return True
            await self._put_overlay_value(self._config_api_key_path(), value)
            self._awaiting_secret_keys().discard(key)
            await self.send_outbound(
                text=self._config_saved_reply("API key", secret=True),
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type)
            return True
        if cmd == "/model":
            value = args.strip()
            if not value:
                settings = self._config_settings()
                current = settings.model or "(not set)"
                await self.send_outbound(
                    text="Model: %s\nUsage: /model <name>" % current,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type)
                return True
            await self._put_overlay_value(self._config_model_path(), value)
            await self.send_outbound(
                text=self._config_saved_reply("Model %s" % value),
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type)
            return True
        if cmd == "/provider":
            value = args.strip()
            if not value:
                settings = self._config_settings()
                current = settings.provider or "(not set)"
                await self.send_outbound(
                    text="Provider: %s\nUsage: /provider <name>" % current,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type)
                return True
            await self._put_overlay_value(self._config_provider_path(), value)
            await self.send_outbound(
                text=self._config_saved_reply("Provider %s" % value),
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type)
            return True
        if cmd == "/base_url":
            value = args.strip()
            if not value:
                settings = self._config_settings()
                current = settings.local_llm_base_url or settings.base_url or "(not set)"
                await self.send_outbound(
                    text="Base URL: %s\nUsage: /base_url <url>" % current,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type)
                return True
            await self._put_overlay_value(self._config_base_url_path(), value)
            await self.send_outbound(
                text=self._config_saved_reply("Base URL %s" % value),
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type)
            return True
        raw = args.strip()
        if raw.lower() in {"reset", "clear", "default"}:
            clear_overlay(self.storage_dir)
            await self._after_overlay_change()
            await self.send_outbound(
                text="Model overlay cleared.",
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type)
            return True
        await self.send_outbound(
            text=self._config_status_text(),
            chat_id=chat_id,
            acct_id=acct_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type)
        return True

    def _config_status_text(self):
        settings = self._config_settings()
        key_path = self._config_api_key_path()
        model = settings.model
        provider = settings.provider
        api_key = settings.api_key
        if not api_key:
            api_key = overlay_get(self._config_overlay(), key_path) or ""
        base_url = settings.local_llm_base_url or settings.base_url
        lines = [
            "Model: %s" % _redact("model.model", model),
            "Provider: %s" % _redact("model.provider", provider),
            "API key: %s" % _redact(key_path, api_key),
            "Base URL: %s" % _redact("base_url", base_url),
            "Commands: /help · /settings · /key · /model · /provider · /base_url · /config reset",
            "Or send an API key as a message.",
            CONFIG_POLICY_HINT,
            SECRET_DELETE_HINT,
        ]
        lines.extend(self._settings_extra_lines())
        return "\n".join(lines)

    async def _consume_or_request_secret(self, ctx):
        text = ctx.text
        if not text or text.startswith("/"):
            return False
        settings = self._config_settings()
        requires = self._config_requires_key(settings)
        accepts = requires or self._config_accepts_key_message(settings)
        if not requires and not accepts:
            return False
        key = (ctx.acct_id, ctx.chat_id)
        pending = self._awaiting_secret_keys()
        if key in pending or looks_like_secret(text):
            await self._put_overlay_value(self._config_api_key_path(), text.strip())
            pending.discard(key)
            await self.send_outbound(
                text=self._config_saved_reply("API key", secret=True),
                chat_id=ctx.chat_id,
                acct_id=ctx.acct_id,
                reply_to=ctx.reply_to,
                platform=ctx.platform,
                chat_type=ctx.chat_type)
            return True
        if not requires:
            return False
        pending.add(key)
        await self.send_outbound(
            text=self._secret_prompt(),
            chat_id=ctx.chat_id,
            acct_id=ctx.acct_id,
            reply_to=ctx.reply_to,
            platform=ctx.platform,
            chat_type=ctx.chat_type)
        return True
