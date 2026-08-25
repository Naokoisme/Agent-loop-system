"""Generate portable hardware runtime assets from an engineering workspace."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

if __package__ in {None, ""}:
    repository_root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(repository_root / "src"), str(repository_root)]

from agent_loop_system.tools.hardware_target import (
    HardwareTargetConfig,
    hardware_command_allowed,
)
from sim_tools.extract_kb import (
    extract_command_capabilities,
    extract_commands,
    extract_windows,
)


def generate_hardware_runtime_assets(
    config: HardwareTargetConfig,
    output_dir: Path,
) -> dict[str, Path]:
    """Extract commands and pages once; runtime consumers never read source."""

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    command_capabilities = extract_command_capabilities(config.command_source)
    filtered = {
        name: capability
        for name, capability in command_capabilities.items()
        if hardware_command_allowed(name)[0]
    }
    command_lines: list[str] = []
    for line in extract_commands(config.command_source).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            command_lines.append(line)
            continue
        if stripped.partition("|")[0].strip().upper() in filtered:
            command_lines.append(line)

    commands_path = output_dir / "commands.json"
    pages_path = output_dir / "pages.json"
    existing = [path for path in (commands_path, pages_path) if path.exists()]
    if existing:
        raise FileExistsError(
            "refusing to overwrite existing runtime assets: "
            + ", ".join(str(path) for path in existing)
        )
    commands_payload = {
        "schema_version": 1,
        "project": config.project,
        "capabilities": [
            {
                "name": capability.name,
                "handler": capability.handler,
                "available": capability.available,
                "unavailable_reason": capability.unavailable_reason,
            }
            for capability in filtered.values()
        ],
        "catalog": "\n".join(command_lines).strip(),
    }
    pages_payload = {
        "schema_version": 1,
        "project": config.project,
        "catalog": extract_windows(
            project_cmake=config.project_cmake,
            app_windows=config.app_windows,
            app_quick_cmd=config.app_quick_cmd,
            project=config.project,
        ).strip(),
    }
    with commands_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(commands_payload, ensure_ascii=False, indent=2) + "\n")
    with pages_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(pages_payload, ensure_ascii=False, indent=2) + "\n")
    return {"commands": commands_path, "pages": pages_path}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate source-free hardware runtime command and page assets"
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = HardwareTargetConfig.from_env()
    outputs = generate_hardware_runtime_assets(config, args.output_dir)
    print(json.dumps({key: str(path) for key, path in outputs.items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
