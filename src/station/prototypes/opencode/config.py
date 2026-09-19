import json
import os
import shlex
import shutil
from dataclasses import dataclass

from station.config.config import mapping_get
from station.prototypes.fs_paths import write_atomic
from station.prototypes.launch_settings import (
    LOCAL_LLM_PROVIDERS,
    collect_provider_env,
    env_str,
    launch_sections,
    merge_launch_settings,
    parse_command_args,
    first_existing_command,
    resolve_local_llm,
    split_provider_model,
    uses_xai as provider_uses_xai,
)

WORKSPACE_OVERRIDE_NAME = "opencode_workspace"

PORT_ENV = "OPENCODE_BRIDGE_PORT"

def _env_port():
    raw = env_str(PORT_ENV)
    if not raw:
        return 0
    return int(raw)

def is_windows_interop_path(path):
    normalized = path.replace("\\", "/").lower()
    if normalized.endswith(".exe"):
        return True
    if normalized.startswith("/mnt/c/"):
        return True
    if "/nvm4w/" in normalized:
        return True
    return False

def _linux_opencode_candidates():
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".local", "bin", "opencode"),
        os.path.join(home, ".local", "share", "opencode-linux", "opencode"),
        os.path.join(home, ".opencode", "bin", "opencode"),
    ]

def _usable_linux_opencode(path):
    if not os.path.exists(path):
        return ""
    resolved = os.path.realpath(path)
    if not os.path.isfile(resolved) or not os.access(resolved, os.X_OK):
        return ""
    if is_windows_interop_path(resolved):
        return ""
    return resolved

def _resolve_opencode_command(server):
    explicit = mapping_get(server, "command", (str, list), None, allow_none=True)
    if explicit is None or explicit == "" or explicit == []:
        explicit = env_str("OPENCODE_BRIDGE_OPENCODE_COMMAND")
        if explicit:
            explicit = shlex.split(explicit)
        else:
            explicit = None
    else:
        explicit = parse_command_args(explicit, "server.command")

    if explicit:
        if is_windows_interop_path(explicit[0]):
            raise RuntimeError(
                "server.command points at a Windows/interop binary; "
                "install a Linux OpenCode binary for WSL (e.g. ~/.local/bin/opencode)"
            )
        return explicit

    found = first_existing_command(path for path in _linux_opencode_candidates() if _usable_linux_opencode(path))
    if found:
        return found

    installed = shutil.which("opencode")
    if installed:
        usable = _usable_linux_opencode(installed)
        if usable:
            return [usable]
        if is_windows_interop_path(installed):
            raise RuntimeError(
                "PATH opencode is a Windows/interop binary; "
                "install a Linux OpenCode binary for WSL (e.g. ~/.local/bin/opencode)"
            )

    raise RuntimeError(
        "Linux OpenCode binary not found. Install with: "
        "curl -fsSL https://opencode.ai/install | bash "
        "or place a Linux binary at ~/.local/bin/opencode"
    )

# Local OpenAI-compatible backends use a custom provider id so they are not
# confused with OpenCode built-in providers.
OPENCODE_LOCAL_PROVIDER = "local-llm"


def write_local_llm_opencode_config(path, *, settings):
    model_id = settings.model or "default"
    provider_id = settings.gateway_provider()
    api_key = settings.local_llm_api_key or "local"
    model_entry = {
        "name": model_id.rsplit("/", 1)[-1],
        "tool_call": True,
        "reasoning": False,
        "attachment": False,
        "limit": {
            "context": settings.local_llm_context_window or 32768,
            "output": settings.local_llm_max_output or 8192,
        },
    }
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "model": "%s/%s" % (provider_id, model_id),
        "provider": {
            provider_id: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Local LLM",
                "key": api_key,
                "options": {
                    "name": provider_id,
                    "baseURL": settings.local_llm_base_url,
                    "apiKey": api_key,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                "models": {
                    model_id: model_entry,
                },
            }
        },
        "permission": {"*": "allow"},
    }
    extra = dict(settings.opencode_overrides or {})
    extra_provider = extra.pop("provider", None)
    payload.update(extra)
    if isinstance(extra_provider, dict):
        merged = dict(payload.get("provider") or {})
        merged.update(extra_provider)
        payload["provider"] = merged
    write_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return path


@dataclass(slots=True)
class OpenCodeLaunchSettings:
    command: list
    command_cwd: str
    workspace_dir: str
    hostname: str
    port: int
    model: str
    provider: str
    agent: str
    variant: str
    approvals_mode: str
    server_password: str
    server_username: str
    extra_env: dict
    serve_args: list
    api_key: str
    local_llm_base_url: str
    local_llm_api_key: str
    local_llm_context_window: int
    local_llm_max_output: int
    opencode_overrides: dict

    def uses_local_llm(self):
        if self.local_llm_base_url:
            return True
        return self.provider.lower() in LOCAL_LLM_PROVIDERS

    def gateway_provider(self):
        if self.uses_local_llm():
            return OPENCODE_LOCAL_PROVIDER
        return self.provider

    def uses_xai(self):
        return provider_uses_xai(self.provider, self.model, local_llm=self.uses_local_llm())

    @classmethod
    def from_model_settings(cls, model_settings, *, default_workspace, config_file=None, workspace_override=None):
        raw = merge_launch_settings(model_settings, config_file=config_file)

        sections = launch_sections(raw, "model", "server", "workspace", "keys", "env", "local_llm", "opencode")
        model = sections["model"]
        server = sections["server"]
        workspace = sections["workspace"]
        keys = sections["keys"]
        env_section = sections["env"]
        local_llm = sections["local_llm"]
        opencode_overrides = sections["opencode"]

        workspace_dir = (
            (workspace_override.strip() if workspace_override else "")
            or mapping_get(workspace, "dir", (str,), "")
            or env_str("OPENCODE_BRIDGE_WORKSPACE")
            or default_workspace
        )
        workspace_path = os.path.expanduser(workspace_dir)

        hostname = (
            mapping_get(server, "hostname", (str,), "")
            or env_str("OPENCODE_BRIDGE_HOSTNAME")
            or "127.0.0.1"
        )
        port = mapping_get(server, "port", (int,), None, allow_none=True)
        if port is None:
            port = _env_port()

        serve_args = parse_command_args(
            mapping_get(server, "serve_args", (str, list), None, allow_none=True),
            "server.serve_args",
        )
        if not serve_args:
            raw_args = env_str("OPENCODE_BRIDGE_SERVE_ARGS")
            if raw_args:
                serve_args = shlex.split(raw_args)

        provider, model_id = split_provider_model(
            mapping_get(model, "model", (str,), ""),
            mapping_get(model, "provider", (str,), ""),
        )
        if provider.lower() == "grok":
            provider = "xai"
        if not provider and model_id.lower().startswith("grok"):
            provider = "xai"

        extra_env = collect_provider_env(keys, env_section)
        (
            provider,
            local_llm_base_url,
            local_llm_api_key,
            uses_local_llm,
            local_llm_context_window,
            local_llm_max_output,
        ) = resolve_local_llm(
            model=model,
            keys=keys,
            local_llm=local_llm,
            extra_env=extra_env,
            provider=provider,
            empty_provider=OPENCODE_LOCAL_PROVIDER,
            max_output_default=8192,
        )
        api_key = (
            (local_llm_api_key or "local")
            if uses_local_llm
            else (
                mapping_get(keys, "xai_api_key", (str,), "")
                or mapping_get(model, "api_key", (str,), "")
                or env_str("XAI_API_KEY")
                or env_str("GROK_API_KEY")
            )
        )
        if uses_local_llm:
            local_llm_api_key = api_key
        elif api_key:
            extra_env.setdefault("XAI_API_KEY", api_key)

        return cls(
            command=_resolve_opencode_command(server),
            command_cwd=workspace_path if os.path.isdir(workspace_path) else os.getcwd(),
            workspace_dir=workspace_path,
            hostname=hostname,
            port=port,
            model=model_id,
            provider=provider,
            agent=mapping_get(model, "agent", (str,), ""),
            variant=mapping_get(model, "variant", (str,), ""),
            approvals_mode=mapping_get(model, "approvals_mode", (str,), "always").lower(),
            server_password=(
                mapping_get(server, "password", (str,), "")
                or env_str("OPENCODE_SERVER_PASSWORD")
            ),
            server_username=(
                mapping_get(server, "username", (str,), "")
                or env_str("OPENCODE_SERVER_USERNAME")
                or "opencode"
            ),
            extra_env=extra_env,
            serve_args=serve_args,
            api_key=api_key,
            local_llm_base_url=local_llm_base_url,
            local_llm_api_key=local_llm_api_key,
            local_llm_context_window=local_llm_context_window,
            local_llm_max_output=local_llm_max_output,
            opencode_overrides=dict(opencode_overrides),
        )

    def model_payload(self):
        provider = self.gateway_provider()
        if not self.model or not provider:
            return None
        payload = {"providerID": provider, "modelID": self.model}
        if self.variant:
            payload["variant"] = self.variant
        return payload

    def message_model_payload(self):
        provider = self.gateway_provider()
        if not self.model or not provider:
            return None
        return {"providerID": provider, "modelID": self.model}

def workspace_override_path(storage_dir):
    return os.path.join(storage_dir, WORKSPACE_OVERRIDE_NAME)

def load_workspace_override(storage_dir):
    path = workspace_override_path(storage_dir)
    if not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

def save_workspace_override(storage_dir, workspace):
    write_atomic(workspace_override_path(storage_dir), workspace.strip() + "\n")

def clear_workspace_override(storage_dir):
    try:
        os.remove(workspace_override_path(storage_dir))
    except FileNotFoundError:
        pass
