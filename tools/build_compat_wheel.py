"""Build and hash-lock an Agent-loop wheel for a platform repository."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]


def _run_git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def python_with_pip(explicit: str | None = None) -> str:
    """Choose a Python 3.12 builder even when the test venv omits pip."""

    candidates = [explicit, sys.executable, shutil.which("python")]
    checked: set[str] = set()
    for candidate in candidates:
        if not candidate:
            continue
        resolved = str(Path(candidate).resolve())
        if resolved in checked:
            continue
        checked.add(resolved)
        probe = subprocess.run(
            [resolved, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
        )
        version = subprocess.run(
            [resolved, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0 and version.stdout.strip() == "3.12":
            return resolved
    raise RuntimeError("找不到带 pip 的 Python 3.12 wheel 构建器")


def source_tree_sha256(root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    files = [root / "pyproject.toml", root / ".env.example"]
    for directory in (
        root / "src" / "agent_loop_system",
        root / "frontend",
        root / "case_map" / "579_case_map",
        root / "tools",
    ):
        files.extend(sorted(directory.rglob("*")))
    for path in files:
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_lock_payload(*, wheel: Path, source_commit: str, source_dirty: bool) -> dict:
    asset_manifest = ROOT / "src" / "agent_loop_system" / "platform_data" / "579" / "import_manifest.json"
    return {
        "kind": "AgentLoopDependencyLock",
        "schema_version": 1,
        "package": "agent-loop-system",
        "wheel_file": wheel.name,
        "wheel_sha256": sha256_file(wheel),
        "source_commit": source_commit,
        "source_dirty": source_dirty,
        "source_tree_sha256": source_tree_sha256(),
        "platform_579_import_manifest_sha256": sha256_file(asset_manifest),
        "python": "3.12",
        "release_eligible": not source_dirty,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--builder-python")
    args = parser.parse_args(list(argv) if argv is not None else None)
    status = _run_git("status", "--porcelain")
    dirty = bool(status)
    if dirty and not args.allow_dirty:
        print("工作区存在未提交改动；正式 wheel 构建被阻止", file=sys.stderr)
        return 2
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    before = set(output.glob("agent_loop_system-*.whl"))
    builder_python = python_with_pip(args.builder_python)
    subprocess.run([
        builder_python, "-m", "pip", "wheel", str(ROOT), "--no-deps",
        "--wheel-dir", str(output),
    ], cwd=ROOT, check=True)
    wheels = sorted(set(output.glob("agent_loop_system-*.whl")) - before)
    if not wheels:
        wheels = sorted(output.glob("agent_loop_system-*.whl"), key=lambda item: item.stat().st_mtime)
    if not wheels:
        raise RuntimeError("wheel 构建完成但未找到产物")
    wheel = wheels[-1]
    lock = build_lock_payload(
        wheel=wheel,
        source_commit=_run_git("rev-parse", "HEAD"),
        source_dirty=dirty,
    )
    lock_path = output / "agent-loop-dependency-lock.json"
    lock_path.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"wheel": str(wheel), "lock": str(lock_path), **lock}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
