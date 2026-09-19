import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from station.prototypes.boundary import ext_mapping_get
from station.prototypes.launch_settings import (
    collect_provider_env,
    env_str,
    first_existing_command,
    launch_sections,
    merge_launch_settings,
    parse_command_args,
    split_provider_model,
)

PI_ROOT = Path(__file__).resolve().parents[5] / "pi"
PI_CODING_AGENT = PI_ROOT / "packages" / "coding-agent"

def _resolve_pi_command(pi_section):
    explicit = ext_mapping_get(pi_section, "command", (str, list), None, allow_none=True)
    if explicit is None or explicit == "" or explicit == []:
        env_cmd = env_str("PI_BRIDGE_PI_COMMAND")
        if env_cmd:
            return shlex.split(env_cmd)
    else:
        parsed = parse_command_args(explicit, "pi.command")
        if parsed:
            return parsed

    tsx = PI_ROOT / "node_modules" / ".bin" / "tsx"
    cli_ts = PI_CODING_AGENT / "src" / "cli.ts"
    node = shutil.which("node")
    bash = shutil.which("bash")
    found = first_existing_command(
        [
            [str(tsx), "--tsconfig", str(PI_ROOT / "tsconfig.json"), str(cli_ts)] if tsx.exists() and cli_ts.exists() else None,
            [node, str(PI_CODING_AGENT / "dist/bundle/cli.js")] if node and (PI_CODING_AGENT / "dist/bundle/cli.js").exists() else None,
            [node, str(PI_CODING_AGENT / "dist/cli.js")] if node and (PI_CODING_AGENT / "dist/cli.js").exists() else None,
            [bash, str(PI_ROOT / "pi-test.sh")] if bash and (PI_ROOT / "pi-test.sh").exists() else None,
            shutil.which("pi"),
        ]
    )
    if found:
        return found

    raise RuntimeError(
        "Pi binary not found. Build the pi monorepo, install the pi CLI, "
        "or set pi.command / PI_BRIDGE_PI_COMMAND."
    )

@dataclass(slots=True)
class PiLaunchSettings:
    command: list
    command_cwd: Path
    workspace_dir: str
    session_root: Path
    agent_home: Path
    model: str
    provider: str
    thinking_level: str
    extra_env: dict
    rpc_args: list
    no_session: bool

    @classmethod
    def from_model_settings(
        cls,
        model_settings,
        *,
        default_workspace,
        default_session_root,
        default_agent_home,
        config_file=None,
    ):
        raw = merge_launch_settings(model_settings or {}, config_file=config_file)

        sections = launch_sections(raw, "pi", "model", "workspace", "keys", "env")
        pi_section = sections["pi"]
        model = sections["model"]
        workspace = sections["workspace"]
        keys = sections["keys"]
        env_section = sections["env"]

        workspace_dir = (
            ext_mapping_get(workspace, "dir", (str,), "").strip()
            or env_str("PI_BRIDGE_WORKSPACE")
            or default_workspace
        )
        workspace_path = Path(workspace_dir).expanduser()

        session_root = Path(
            ext_mapping_get(pi_section, "session_root", (str,), "").strip()
            or env_str("PI_BRIDGE_SESSION_ROOT")
            or default_session_root
        ).expanduser()
        agent_home = Path(
            ext_mapping_get(pi_section, "home", (str,), "").strip()
            or env_str("PI_BRIDGE_AGENT_HOME")
            or default_agent_home
        ).expanduser()

        provider, model_id = split_provider_model(
            ext_mapping_get(model, "model", (str,), "").strip(),
            ext_mapping_get(model, "provider", (str,), "").strip(),
        )

        rpc_args = parse_command_args(
            ext_mapping_get(pi_section, "rpc_args", (str, list), None, allow_none=True),
            "pi.rpc_args",
        )
        if not rpc_args:
            raw_args = env_str("PI_BRIDGE_RPC_ARGS")
            if raw_args:
                rpc_args = shlex.split(raw_args)

        no_session = ext_mapping_get(pi_section, "no_session", (bool,), False)
        if env_str("PI_BRIDGE_NO_SESSION").lower() in {"1", "true", "yes"}:
            no_session = True

        if workspace_path.is_dir():
            command_cwd = workspace_path
        elif PI_ROOT.exists():
            command_cwd = PI_ROOT
        else:
            command_cwd = Path.cwd()

        return cls(
            command=_resolve_pi_command(pi_section),
            command_cwd=command_cwd,
            workspace_dir=str(workspace_path),
            session_root=session_root,
            agent_home=agent_home,
            model=model_id,
            provider=provider,
            thinking_level=ext_mapping_get(model, "thinking_level", (str,), "").strip(),
            extra_env=collect_provider_env(keys, env_section),
            rpc_args=rpc_args,
            no_session=no_session,
        )
