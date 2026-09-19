from dataclasses import dataclass
from pathlib import Path
import json

from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_mapping_get, ext_require, ext_str
from station.prototypes.launch_settings import (
    XAI_PROVIDERS,
    apply_local_provider_env,
    collect_provider_env,
    env_str,
    launch_sections,
    merge_launch_settings,
    resolve_local_llm,
    split_provider_model,
    uses_xai as provider_uses_xai,
    voice_settings_from_mapping,
)

HERMES_ROOT = Path(__file__).resolve().parents[5] / "projects" / "hermes-agent"
HERMES_CUSTOM_PROVIDER = "custom"
DEFAULT_TUI_TOOLSETS = ("file", "terminal", "web", "memory")
DEVICE_CODE_PROVIDERS = ("nous", "openai-codex", "minimax-oauth", "xai-oauth")


def hermes_home_path(storage_dir):
    return Path(storage_dir).resolve() / "hermes_home"


def load_saved_provider(storage_dir):
    path = hermes_home_path(storage_dir) / "config.yaml"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ExternalError("hermes config is not valid JSON")
    data = ext_dict("hermes config", data)
    model = data.get("model")
    if model is None:
        return None
    model = ext_dict("hermes config model", model)
    provider = ext_str("hermes config model.provider", model.get("provider"))
    if provider not in DEVICE_CODE_PROVIDERS and provider not in XAI_PROVIDERS:
        return None
    return {"provider": provider, "model": ext_str("hermes config model.default", model.get("default"))}


def _resolve_python(hermes):
    explicit = ext_mapping_get(hermes, "python", (str,), "").strip()
    if explicit:
        return explicit
    for key in ("HERMES_BRIDGE_HERMES_PYTHON", "HERMES_PYTHON", "PYTHON"):
        value = env_str(key)
        if value:
            return value

    venv = env_str("VIRTUAL_ENV")
    if venv:
        for rel in ("bin/python", "bin/python3"):
            candidate = Path(venv) / rel
            if candidate.exists():
                return str(candidate)

    for rel in (".venv/bin/python", ".venv/bin/python3"):
        candidate = HERMES_ROOT / rel
        if candidate.exists():
            return str(candidate)

    return "python3"


@dataclass(slots=True)
class HermesLaunchSettings:
    hermes_root: Path
    hermes_python: str
    workspace_dir: str
    model: str
    provider: str
    reasoning_effort: str
    fast: bool
    approvals_mode: str
    voice_reply_mode: str
    voice_delivery_method: str
    extra_env: dict
    config_overrides: dict
    local_llm_base_url: str
    local_llm_api_key: str
    local_llm_context_window: int
    local_llm_max_output: int
    disabled_toolsets: list

    def uses_local_llm(self):
        return bool(self.local_llm_base_url)

    def uses_xai(self):
        return provider_uses_xai(self.provider, self.model, local_llm=self.uses_local_llm())

    def gateway_provider(self):
        if self.uses_local_llm():
            return HERMES_CUSTOM_PROVIDER
        if self.uses_xai():
            return "xai-oauth"
        return self.provider

    def tui_toolsets(self):
        disabled = {name.strip().lower() for name in self.disabled_toolsets}
        return ",".join(name for name in DEFAULT_TUI_TOOLSETS if name not in disabled)

    def apply_process_env(self, env):
        merged = dict(env)
        merged.update(self.extra_env)
        if self.uses_local_llm():
            return apply_local_provider_env(merged, base_url=self.local_llm_base_url)
        return merged

    @classmethod
    def from_model_settings(cls, model_settings, *, default_workspace, config_file=None):
        raw = merge_launch_settings(model_settings, config_file=config_file)

        sections = launch_sections(raw, "hermes", "model", "voice", "keys", "env", "local_llm", "config")
        hermes = sections["hermes"]
        model = sections["model"]
        voice = sections["voice"]
        keys = sections["keys"]
        env_section = sections["env"]
        local_llm = sections["local_llm"]
        config_overrides = dict(sections["config"])
        disabled_toolsets = ext_mapping_get(config_overrides, "disabled_toolsets", (list,), None, allow_none=True)
        if disabled_toolsets is None:
            disabled_toolsets = ext_mapping_get(model, "disabled_toolsets", (list,), [])
        disabled_toolsets = [
            ext_require("disabled_toolsets[]", item, (str,)).strip()
            for item in disabled_toolsets
        ]
        disabled_toolsets = [item for item in disabled_toolsets if item]
        config_overrides["disabled_toolsets"] = disabled_toolsets
        if "memory_enabled" not in config_overrides and "memory_enabled" in model:
            config_overrides["memory_enabled"] = ext_mapping_get(model, "memory_enabled", (bool,), False)

        hermes_root = Path(
            ext_mapping_get(hermes, "root", (str,), "").strip()
            or env_str("HERMES_BRIDGE_HERMES_ROOT")
            or str(HERMES_ROOT)
        ).expanduser()

        workspace_dir = (
            ext_mapping_get(hermes, "workspace", (str,), "").strip()
            or env_str("HERMES_BRIDGE_WORKSPACE")
            or default_workspace
        )

        voice_reply_mode, voice_delivery_method = voice_settings_from_mapping(voice)

        provider, model_id = split_provider_model(
            ext_mapping_get(model, "model", (str,), "").strip(),
            ext_mapping_get(model, "provider", (str,), "").strip(),
        )
        saved = load_saved_provider(default_workspace)
        if saved is not None:
            provider = saved["provider"]
            model_id = saved["model"] or model_id

        extra_env = collect_provider_env(keys, env_section)
        if (provider or "").strip().lower() in DEVICE_CODE_PROVIDERS:
            local_llm_base_url = ""
            local_llm_api_key = ""
            local_llm_context_window = 0
            local_llm_max_output = 0
        else:
            (
                provider,
                local_llm_base_url,
                local_llm_api_key,
                _uses_local,
                local_llm_context_window,
                local_llm_max_output,
            ) = resolve_local_llm(
                model=model,
                keys=keys,
                local_llm=local_llm,
                extra_env=extra_env,
                provider=provider,
                empty_provider=HERMES_CUSTOM_PROVIDER,
            )

        return cls(
            hermes_root=hermes_root,
            hermes_python=_resolve_python(hermes),
            workspace_dir=workspace_dir,
            model=model_id,
            provider=provider,
            reasoning_effort=ext_mapping_get(model, "reasoning_effort", (str,), "").strip(),
            fast=ext_mapping_get(model, "fast", (bool,), False),
            approvals_mode=ext_mapping_get(model, "approvals_mode", (str,), "").strip() or "always",
            voice_reply_mode=voice_reply_mode,
            voice_delivery_method=voice_delivery_method,
            extra_env=extra_env,
            config_overrides=config_overrides,
            local_llm_base_url=local_llm_base_url,
            local_llm_api_key=local_llm_api_key,
            local_llm_context_window=local_llm_context_window,
            local_llm_max_output=local_llm_max_output,
            disabled_toolsets=disabled_toolsets,
        )
