from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from agent_loop_system.tools.hardware_runtime_profile import (
    HardwareRuntimeProfileError,
    load_hardware_runtime_profile,
)
from scripts.publish_profile import publish_profile
from scripts.generate_hardware_runtime_assets import generate_hardware_runtime_assets


class HardwareRuntimeProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        profile_version = patch.dict(
            os.environ,
            {"W30_HARDWARE_PROFILE_VERSION": ""},
            clear=False,
        )
        profile_version.start()
        self.addCleanup(profile_version.stop)

    def _publish(self, base: Path, *, version: str = "v30-test.1") -> Path:
        project = "6202_W5230"
        profile_root = base / "profiles" / project
        profile_root.mkdir(parents=True)
        artifact = base / "w30.up3"
        artifact.write_bytes(b"known-6202-firmware")
        firmware_hash = hashlib.sha256(artifact.read_bytes()).hexdigest().upper()
        commands = base / "commands.json"
        pages = base / "pages.json"
        commands.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project": project,
                    "capabilities": [
                        {
                            "name": "GUI_PING",
                            "handler": "quick_cmd_gui_ping",
                            "available": True,
                            "unavailable_reason": None,
                        },
                        {
                            "name": "ENTER_PAGE",
                            "handler": "quick_cmd_enter_page",
                            "available": True,
                            "unavailable_reason": None,
                        },
                    ],
                    "catalog": (
                        "# commands\n"
                        "GUI_PING|参数=seq|可用\n"
                        "ENTER_PAGE|参数=window,param|可用"
                    ),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        pages.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "project": project,
                    "catalog": "DIAL|完整示例=:ENTER_PAGE:DIAL,0",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        publish_profile(
            profile_root=profile_root,
            version=version,
            artifact=artifact,
            profile_metadata={"id": project, "channel": "test"},
            firmware_metadata={
                "runtime_version": {
                    "ui_firmware_version": "Version 30",
                    "project_semver": "30.0.0",
                }
            },
            source_metadata={"root_commit": "firmware-commit"},
            validation_metadata={"device": "PASS"},
            runtime_assets={"commands": commands, "pages": pages},
            runtime_metadata={
                "project": project,
                "target": "hardware",
                "firmware_version": "Version 30",
                "firmware_sha256": firmware_hash,
                "automation_protocol_version": "w30_test_bridge/1",
                "agent_loop_min_version": "0.4.0",
                "case_map_version": "case-map-commit",
                "verified_capabilities": ["quick_commands", "page_catalog"],
            },
            staging_root=base / "staging",
            publish_id=f"publish-{version}",
            published_at="2026-08-22T00:00:00Z",
        )
        return base / "profiles"

    def _mutate_manifest(self, root: Path, mutate) -> None:
        profile_root = root / "6202_W5230"
        release = profile_root / "releases" / "v30-test.1"
        manifest_path = release / "profile_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        mutate(manifest)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest().upper()
        sums_path = release / "SHA256SUMS.txt"
        sums = [
            f"{manifest_hash}  profile_manifest.json"
            if line.endswith("  profile_manifest.json")
            else line
            for line in sums_path.read_text(encoding="ascii").splitlines()
        ]
        sums_path.write_text("\n".join(sums) + "\n", encoding="ascii")
        latest_path = profile_root / "latest.json"
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        latest["profile_manifest_sha256"] = manifest_hash
        latest_path.write_text(
            json.dumps(latest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_loads_latest_profile_without_source_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._publish(Path(temporary))
            with patch.dict(os.environ, {}, clear=False):
                for key in (
                    "W30_HARDWARE_SOURCE_ROOT",
                    "W30_HARDWARE_WORKSPACE_ROOT",
                    "W30_SOURCE_ROOT",
                ):
                    os.environ.pop(key, None)
                profile = load_hardware_runtime_profile(profiles_root=root)

            self.assertEqual(profile.project, "6202_W5230")
            self.assertEqual(profile.version, "v30-test.1")
            self.assertEqual(profile.firmware_version, "Version 30")
            self.assertIn("GUI_PING", profile.command_capabilities)
            self.assertIn("DIAL", profile.agent_knowledge)
            self.assertEqual(
                profile.compatibility_summary["case_map_version"],
                "case-map-commit",
            )

    def test_explicit_version_does_not_require_latest_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._publish(Path(temporary))
            (root / "6202_W5230" / "latest.json").unlink()
            profile = load_hardware_runtime_profile(
                profiles_root=root,
                version="v30-test.1",
            )
            self.assertEqual(profile.version, "v30-test.1")

    def test_rejects_corrupted_runtime_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._publish(Path(temporary))
            commands = (
                root
                / "6202_W5230"
                / "releases"
                / "v30-test.1"
                / "runtime"
                / "commands.json"
            )
            commands.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(HardwareRuntimeProfileError, "完整性校验失败"):
                load_hardware_runtime_profile(profiles_root=root)

    def test_rejects_profile_for_another_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._publish(Path(temporary))
            with self.assertRaisesRegex(HardwareRuntimeProfileError, "latest.json"):
                load_hardware_runtime_profile(
                    profiles_root=root,
                    project="6204_W5230",
                )

    def test_rejects_an_unsupported_automation_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._publish(Path(temporary))
            self._mutate_manifest(
                root,
                lambda manifest: manifest["runtime"].update({
                    "automation_protocol_version": "w30_test_bridge/99"
                }),
            )
            with self.assertRaisesRegex(HardwareRuntimeProfileError, "自动化协议不兼容"):
                load_hardware_runtime_profile(profiles_root=root)

    def test_rejects_a_profile_that_requires_a_newer_agent_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._publish(Path(temporary))
            self._mutate_manifest(
                root,
                lambda manifest: manifest["runtime"].update({
                    "agent_loop_min_version": "99.0.0"
                }),
            )
            with self.assertRaisesRegex(HardwareRuntimeProfileError, "版本过低"):
                load_hardware_runtime_profile(profiles_root=root)

    def test_generator_filters_commands_that_the_hardware_agent_must_not_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config = SimpleNamespace(
                project="6202_W5230",
                command_source=base / "srv_quick_cmd_handler.c",
                project_cmake=base / "Project.cmake",
                app_windows=base / "windows",
                app_quick_cmd=base / "gui_comm_quick_cmd.c",
            )
            safe = SimpleNamespace(
                name="GUI_PING",
                handler="quick_cmd_gui_ping",
                available=True,
                unavailable_reason=None,
            )
            dangerous = SimpleNamespace(
                name="SYSTEM_REBOOT",
                handler="quick_cmd_system_reboot",
                available=True,
                unavailable_reason=None,
            )
            with (
                patch(
                    "scripts.generate_hardware_runtime_assets.extract_command_capabilities",
                    return_value={"GUI_PING": safe, "SYSTEM_REBOOT": dangerous},
                ),
                patch(
                    "scripts.generate_hardware_runtime_assets.extract_commands",
                    return_value=(
                        "# generated\n"
                        "GUI_PING|参数=seq|可用\n"
                        "SYSTEM_REBOOT|参数=无|可用"
                    ),
                ),
                patch(
                    "scripts.generate_hardware_runtime_assets.extract_windows",
                    return_value="DIAL|完整示例=:ENTER_PAGE:DIAL,0",
                ),
            ):
                outputs = generate_hardware_runtime_assets(config, base / "output")

            commands = json.loads(outputs["commands"].read_text(encoding="utf-8"))
            self.assertEqual(
                [item["name"] for item in commands["capabilities"]],
                ["GUI_PING"],
            )
            self.assertNotIn("SYSTEM_REBOOT", commands["catalog"])
            pages = json.loads(outputs["pages"].read_text(encoding="utf-8"))
            self.assertIn("DIAL", pages["catalog"])


if __name__ == "__main__":
    unittest.main()
