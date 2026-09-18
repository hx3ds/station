from dataclasses import dataclass
from pathlib import Path

from station.config.config import mapping_get
from station.prototypes.launch_settings import (
    LOCAL_LLM_PROVIDERS,
    apply_local_provider_env,
    collect_provider_env,
    env_str,
    merge_launch_settings,
    normalize_openai_base_url,
    split_provider_model,
    voice_settings_from_mapping,
)

from .auth_store import load_selected_provider

HERMES_ROOT = Path(__file__).resolve().parents[5] / "projects" / "hermes-agent"
HERMES_CUSTOM_PROVIDER = "custom"
XAI_PROVIDERS = {"xai", "grok", "xai-oauth", "grok-oauth"}
XAI_DEFAULT_MODEL = "grok-4.6"


def xai_model_or_default(model_id):
    model_id = (model_id or "").strip()
    if model_id.lower().startswith("grok"):
        return model_id
    return XAI_DEFAULT_MODEL


def _resolve_python(hermes):
    explicit = mapping_get(hermes, "python", (str,), "").strip()
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

    def uses_local_llm(self):
        return bool(self.local_llm_base_url)

    def uses_xai(self):
        if self.uses_local_llm():
            return False
        if self.provider.lower() in XAI_PROVIDERS:
            return True
        return self.model.lower().startswith("grok")

    def gateway_provider(self):
        if self.uses_local_llm():
            return HERMES_CUSTOM_PROVIDER
        if self.uses_xai():
            return "xai-oauth"
        return self.provider

    def apply_process_env(self, env):
        merged = dict(env)
        merged.update(self.extra_env)
        if self.uses_local_llm():
            return apply_local_provider_env(merged, base_url=self.local_llm_base_url)
        return merged

    @classmethod
    def from_model_settings(cls, model_settings, *, default_workspace, config_file=None):
        raw = merge_launch_settings(model_settings, config_file=config_file)

        hermes = mapping_get(raw, "hermes", (dict,), {})
        model = mapping_get(raw, "model", (dict,), {})
        voice = mapping_get(raw, "voice", (dict,), {})
        keys = mapping_get(raw, "keys", (dict,), {})
        env_section = mapping_get(raw, "env", (dict,), {})
        local_llm = mapping_get(raw, "local_llm", (dict,), {})
        config_overrides = mapping_get(raw, "config", (dict,), {})

        hermes_root = Path(
            mapping_get(hermes, "root", (str,), "").strip()
            or env_str("HERMES_BRIDGE_HERMES_ROOT")
            or str(HERMES_ROOT)
        ).expanduser()

        workspace_dir = (
            mapping_get(hermes, "workspace", (str,), "").strip()
            or env_str("HERMES_BRIDGE_WORKSPACE")
            or default_workspace
        )

        voice_reply_mode, voice_delivery_method = voice_settings_from_mapping(voice)

        provider, model_id = split_provider_model(
            mapping_get(model, "model", (str,), "").strip(),
            mapping_get(model, "provider", (str,), "").strip(),
        )
        saved = load_selected_provider(default_workspace)
        if saved is not None:
            provider = saved["provider"]
            if saved["model"]:
                model_id = saved["model"]
        if (provider or "").strip().lower() in XAI_PROVIDERS:
            model_id = xai_model_or_default(model_id)

        local_llm_base_url = normalize_openai_base_url(
            mapping_get(local_llm, "base_url", (str,), "")
            or mapping_get(model, "base_url", (str,), "")
            or env_str("LOCAL_LLM_BASE_URL")
        )
        local_llm_api_key = (
            mapping_get(keys, "local_llm_api_key", (str,), "")
            or mapping_get(local_llm, "api_key", (str,), "")
            or mapping_get(model, "api_key", (str,), "")
            or env_str("LOCAL_LLM_API_KEY")
        )
        if not provider and local_llm_base_url:
            provider = HERMES_CUSTOM_PROVIDER

        extra_env = collect_provider_env(keys, env_section)
        uses_local_llm = bool(local_llm_base_url) or provider.lower() in LOCAL_LLM_PROVIDERS
        if uses_local_llm:
            api_key = local_llm_api_key or "local"
            extra_env.setdefault("LOCAL_LLM_API_KEY", api_key)
            extra_env.setdefault("OPENAI_API_KEY", api_key)
            if local_llm_base_url:
                extra_env.setdefault("LOCAL_LLM_BASE_URL", local_llm_base_url)
            local_llm_api_key = api_key

        return cls(
            hermes_root=hermes_root,
            hermes_python=_resolve_python(hermes),
            workspace_dir=workspace_dir,
            model=model_id,
            provider=provider,
            reasoning_effort=mapping_get(model, "reasoning_effort", (str,), "").strip(),
            fast=mapping_get(model, "fast", (bool,), False),
            approvals_mode=mapping_get(model, "approvals_mode", (str,), "").strip() or "always",
            voice_reply_mode=voice_reply_mode,
            voice_delivery_method=voice_delivery_method,
            extra_env=extra_env,
            config_overrides=dict(config_overrides),
            local_llm_base_url=local_llm_base_url,
            local_llm_api_key=local_llm_api_key,
            local_llm_context_window=mapping_get(local_llm, "context_window", (int,), 0),
            local_llm_max_output=mapping_get(local_llm, "max_output", (int,), 0),
        )
