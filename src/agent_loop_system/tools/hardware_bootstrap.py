"""External owner for establishing one hardware test session before a batch."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from agent_loop_system.runtime_root import RuntimePaths, resolve_config_path
from agent_loop_system.tools.hardware_serial import SerialTransportError
from agent_loop_system.tools.hardware_target import HardwareTargetConfig
from agent_loop_system.tools.real_device import (
    TestSessionBootstrapError,
    bootstrap_test_session,
)


DEFAULT_EVIDENCE_ROOT = RuntimePaths.from_root().evidence / "hardware_bootstrap"


def _default_evidence_dir() -> Path:
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
    return DEFAULT_EVIDENCE_ROOT / stamp


def _validate_environment() -> tuple[HardwareTargetConfig, str]:
    config = HardwareTargetConfig.from_env()

    for name in ("W30_SOURCE_ROOT", "W30_AGENT_WORKSPACE_ROOT"):
        value = os.environ.get(name, "").strip()
        if not value:
            raise ValueError(f"{name} must explicitly point to the 6202 workspace")
        if resolve_config_path(value) != config.source_root:
            raise ValueError(f"{name} must match W30_HARDWARE_SOURCE_ROOT")

    for name in ("W30_PROJECT", "W30_HARDWARE_PROJECT"):
        value = os.environ.get(name, "").strip()
        if value != config.project:
            raise ValueError(f"{name} must explicitly equal {config.project}")

    transport = os.environ.get("W30_HARDWARE_TRANSPORT", "").strip().lower()
    if transport != "supercom":
        raise ValueError("W30_HARDWARE_TRANSPORT must explicitly equal supercom")
    port = os.environ.get("W30_HARDWARE_PORT", "").strip()
    if not port:
        raise ValueError("W30_HARDWARE_PORT must be explicitly configured")
    return config, port


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_loop_system.tools.hardware_bootstrap",
        description=(
            "Establish the runner-controlled hardware TEST_SESSION, then release "
            "the SuperCom pipe without stopping the watch session."
        ),
    )
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--startup-timeout", type=float, default=180.0)
    parser.add_argument("--command-timeout", type=float, default=8.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    evidence_dir = (args.evidence_dir or _default_evidence_dir()).resolve()
    try:
        config, port = _validate_environment()
        result = bootstrap_test_session(
            evidence_dir=evidence_dir,
            startup_timeout=args.startup_timeout,
            command_timeout=args.command_timeout,
        )
    except TestSessionBootstrapError as exc:
        payload: dict[str, object] = {
            "ok": False,
            "code": exc.code,
            "message": str(exc),
            "evidence_dir": str(evidence_dir),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    except SerialTransportError as exc:
        payload = {
            "ok": False,
            "code": "BLOCKED_SUPERCOM_PIPE",
            "message": str(exc),
            "evidence_dir": str(evidence_dir),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    except ValueError as exc:
        payload = {
            "ok": False,
            "code": "BOOTSTRAP_CONFIGURATION_ERROR",
            "message": str(exc),
            "evidence_dir": str(evidence_dir),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    except Exception as exc:
        payload = {
            "ok": False,
            "code": "BOOTSTRAP_ERROR",
            "error_type": type(exc).__name__,
            "message": str(exc),
            "evidence_dir": str(evidence_dir),
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1

    payload = {
        "ok": True,
        "status": "active",
        "lease_seconds": result.status.lease_seconds,
        "start_sent": result.start_sent,
        "gui_ping_attempts": result.gui_ping_attempts,
        "bootstrap_event_seen": result.bootstrap_event_seen,
        "transport": "supercom",
        "port": port,
        "project": config.project,
        "evidence_dir": str(evidence_dir),
    }
    reason = result.status.raw.get("reason")
    if reason is not None:
        payload["reason"] = str(reason)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
