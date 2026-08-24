from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.publish_profile import PublishError, publish_download_entry, publish_profile


class PublishProfileTests(unittest.TestCase):
    def _metadata(self) -> dict[str, dict[str, object]]:
        return {
            "profile": {"id": "TEST_TARGET", "channel": "development"},
            "firmware": {"runtime_version": "T1.2.3", "build_type": "debug"},
            "source": {"root_commit": "abc123", "source_dirty": True},
            "validation": {
                "build": "PASS",
                "flash": "NOT_RUN",
                "device": "NOT_RUN",
            },
        }

    def _publish(
        self,
        root: Path,
        artifact: Path,
        *,
        version: str = "v1.0.0-dev.1",
        publish_id: str = "testpublish01",
        dry_run: bool = False,
        artifact_name: str | None = None,
        download_root: Path | None = None,
        latest_artifact_name: str | None = None,
    ):
        metadata = self._metadata()
        return publish_profile(
            profile_root=root,
            version=version,
            artifact=artifact,
            artifact_name=artifact_name,
            download_root=download_root,
            latest_artifact_name=latest_artifact_name,
            staging_root=root.parent / "staging",
            profile_metadata=metadata["profile"],
            firmware_metadata=metadata["firmware"],
            source_metadata=metadata["source"],
            validation_metadata=metadata["validation"],
            dry_run=dry_run,
            publish_id=publish_id,
            published_at="2026-08-20T09:00:00Z",
        )

    def _runtime_assets(self, base: Path) -> tuple[Path, Path]:
        commands = base / "commands.json"
        pages = base / "pages.json"
        commands.write_text(
            json.dumps({
                "schema_version": 1,
                "project": "TEST_TARGET",
                "capabilities": [{
                    "name": "GUI_PING",
                    "handler": "quick_cmd_gui_ping",
                    "available": True,
                    "unavailable_reason": None,
                }],
                "catalog": "GUI_PING|参数=seq|可用",
            }),
            encoding="utf-8",
        )
        pages.write_text(
            json.dumps({
                "schema_version": 1,
                "project": "TEST_TARGET",
                "catalog": "DIAL|完整示例=:ENTER_PAGE:DIAL,0",
            }),
            encoding="utf-8",
        )
        return commands, pages

    def test_publishes_verified_immutable_release_then_latest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "profile"
            root.mkdir()
            legacy = root / "firmware" / "6202_latest.up3"
            legacy.parent.mkdir()
            legacy.write_bytes(b"legacy-must-not-change")
            artifact = base / "new.up3"
            artifact.write_bytes(b"new-firmware-content")

            result = self._publish(root, artifact)

            release = root / "releases" / result.version
            published = release / "firmware" / "new.up3"
            self.assertEqual(published.read_bytes(), artifact.read_bytes())
            self.assertEqual(legacy.read_bytes(), b"legacy-must-not-change")
            self.assertFalse((root / ".incoming" / result.publish_id).exists())

            profile_manifest = json.loads(
                (release / "profile_manifest.json").read_text(encoding="utf-8")
            )
            artifact_manifest = profile_manifest["firmware"]["artifact"]
            self.assertEqual(artifact_manifest["path"], "firmware/new.up3")
            self.assertEqual(artifact_manifest["size"], len(b"new-firmware-content"))
            self.assertEqual(
                artifact_manifest["sha256"],
                hashlib.sha256(b"new-firmware-content").hexdigest().upper(),
            )
            self.assertEqual(profile_manifest["source"]["source_dirty"], True)
            self.assertEqual(profile_manifest["validation"]["flash"], "NOT_RUN")

            latest = json.loads((root / "latest.json").read_text(encoding="utf-8"))
            self.assertEqual(latest["version"], result.version)
            self.assertEqual(latest["release"], f"releases/{result.version}")
            expected_profile_hash = hashlib.sha256(
                (release / "profile_manifest.json").read_bytes()
            ).hexdigest().upper()
            self.assertEqual(latest["profile_manifest_sha256"], expected_profile_hash)

            expected_sums = {
                line.split("  ", 1)[1]
                for line in (release / "SHA256SUMS.txt")
                .read_text(encoding="ascii")
                .splitlines()
            }
            self.assertEqual(
                expected_sums,
                {
                    "firmware/new.up3",
                    "firmware_manifest.json",
                    "profile_manifest.json",
                },
            )

    def test_publishes_source_free_runtime_assets_bound_to_firmware(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "profiles" / "TEST_TARGET"
            root.mkdir(parents=True)
            artifact = base / "new.up3"
            artifact.write_bytes(b"runtime-bound-firmware")
            commands, pages = self._runtime_assets(base)
            metadata = self._metadata()
            firmware_hash = hashlib.sha256(artifact.read_bytes()).hexdigest().upper()

            result = publish_profile(
                profile_root=root,
                version="v1.0.0-dev.1",
                artifact=artifact,
                staging_root=base / "staging",
                profile_metadata=metadata["profile"],
                firmware_metadata=metadata["firmware"],
                source_metadata=metadata["source"],
                validation_metadata=metadata["validation"],
                runtime_assets={"commands": commands, "pages": pages},
                runtime_metadata={
                    "project": "TEST_TARGET",
                    "target": "hardware",
                    "firmware_version": "T1.2.3",
                    "firmware_sha256": firmware_hash,
                    "automation_protocol_version": "w30_test_bridge/1",
                    "agent_loop_min_version": "0.4.0",
                    "case_map_version": "case-map-abc123",
                    "verified_capabilities": ["quick_commands", "page_catalog"],
                },
                publish_id="runtimepublish01",
            )

            release = Path(result.release)
            manifest = json.loads(
                (release / "profile_manifest.json").read_text(encoding="utf-8")
            )
            runtime = manifest["runtime"]
            self.assertEqual(runtime["firmware_sha256"], firmware_hash)
            self.assertEqual(
                runtime["assets"]["commands"]["path"],
                "runtime/commands.json",
            )
            self.assertEqual(
                runtime["assets"]["pages"]["path"],
                "runtime/pages.json",
            )
            self.assertEqual(
                (release / "runtime" / "commands.json").read_bytes(),
                commands.read_bytes(),
            )
            sums = (release / "SHA256SUMS.txt").read_text(encoding="ascii")
            self.assertIn("  runtime/commands.json\n", sums)
            self.assertIn("  runtime/pages.json\n", sums)

    def test_rejects_runtime_metadata_for_a_different_firmware(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "profile"
            root.mkdir()
            artifact = base / "new.up3"
            artifact.write_bytes(b"firmware")
            commands, pages = self._runtime_assets(base)
            metadata = self._metadata()

            with self.assertRaisesRegex(PublishError, "firmware_sha256"):
                publish_profile(
                    profile_root=root,
                    version="v1.0.0-dev.1",
                    artifact=artifact,
                    staging_root=base / "staging",
                    profile_metadata=metadata["profile"],
                    firmware_metadata=metadata["firmware"],
                    source_metadata=metadata["source"],
                    validation_metadata=metadata["validation"],
                    runtime_assets={"commands": commands, "pages": pages},
                    runtime_metadata={
                        "project": "TEST_TARGET",
                        "target": "hardware",
                        "firmware_version": "T1.2.3",
                        "firmware_sha256": "0" * 64,
                        "automation_protocol_version": "w30_test_bridge/1",
                        "agent_loop_min_version": "0.4.0",
                        "case_map_version": "case-map-abc123",
                        "verified_capabilities": ["quick_commands"],
                    },
                    publish_id="mismatchpublish01",
                )

    def test_existing_version_hard_stops_without_touching_latest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "profile"
            existing = root / "releases" / "v1.0.0-dev.1"
            existing.mkdir(parents=True)
            marker = existing / "marker.txt"
            marker.write_text("old", encoding="utf-8")
            latest = root / "latest.json"
            latest.write_text('{"version":"old"}\n', encoding="utf-8")
            artifact = base / "new.up3"
            artifact.write_bytes(b"new")

            with self.assertRaisesRegex(PublishError, "already exists"):
                self._publish(root, artifact)

            self.assertEqual(marker.read_text(encoding="utf-8"), "old")
            self.assertEqual(latest.read_text(encoding="utf-8"), '{"version":"old"}\n')
            self.assertFalse((root / ".incoming").exists())

    def test_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "profile"
            root.mkdir()
            artifact = base / "new.up3"
            artifact.write_bytes(b"new")

            result = self._publish(root, artifact, dry_run=True)

            self.assertTrue(result.dry_run)
            self.assertEqual(list(root.iterdir()), [])
            self.assertFalse((base / "staging").exists())

    def test_rejects_path_traversal_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "profile"
            root.mkdir()
            artifact = base / "new.up3"
            artifact.write_bytes(b"new")

            with self.assertRaisesRegex(PublishError, "safe path segment"):
                self._publish(root, artifact, version="../escape")
            with self.assertRaisesRegex(PublishError, "safe path segment"):
                self._publish(root, artifact, artifact_name="../escape.up3")
            self.assertEqual(list(root.iterdir()), [])

    def test_rejects_local_staging_inside_profile_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "profile"
            root.mkdir()
            artifact = base / "new.up3"
            artifact.write_bytes(b"new")
            metadata = self._metadata()

            with self.assertRaisesRegex(PublishError, "outside the profile root"):
                publish_profile(
                    profile_root=root,
                    version="v1.0.0-dev.1",
                    artifact=artifact,
                    staging_root=root / ".staging",
                    profile_metadata=metadata["profile"],
                    firmware_metadata=metadata["firmware"],
                    source_metadata=metadata["source"],
                    validation_metadata=metadata["validation"],
                    publish_id="testpublish01",
                )
            self.assertEqual(list(root.iterdir()), [])

    def test_rejects_firmware_metadata_that_spoofs_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "profile"
            root.mkdir()
            artifact = base / "new.up3"
            artifact.write_bytes(b"new")
            metadata = self._metadata()
            metadata["firmware"]["artifact"] = {"sha256": "fake"}

            with self.assertRaisesRegex(PublishError, "reserved"):
                publish_profile(
                    profile_root=root,
                    version="v1.0.0-dev.1",
                    artifact=artifact,
                    staging_root=base / "staging",
                    profile_metadata=metadata["profile"],
                    firmware_metadata=metadata["firmware"],
                    source_metadata=metadata["source"],
                    validation_metadata=metadata["validation"],
                    publish_id="testpublish01",
                )

    def test_publishes_short_download_entry_after_immutable_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "profiles" / "TEST_TARGET"
            root.mkdir(parents=True)
            artifact = base / "new.up3"
            artifact.write_bytes(b"new-firmware-content")

            result = self._publish(
                root,
                artifact,
                download_root=base / "下载",
                latest_artifact_name="TEST_TARGET_最新.up3",
            )

            short_artifact = base / "下载" / "TEST_TARGET" / "TEST_TARGET_最新.up3"
            self.assertEqual(short_artifact.read_bytes(), artifact.read_bytes())
            self.assertEqual(
                Path(result.download_artifact).resolve(), short_artifact.resolve()
            )
            info_path = base / "下载" / "TEST_TARGET" / "版本信息.json"
            info = json.loads(info_path.read_text(encoding="utf-8"))
            self.assertEqual(info["profile_id"], "TEST_TARGET")
            self.assertEqual(info["profile_version"], "v1.0.0-dev.1")
            self.assertEqual(info["runtime_version"], "T1.2.3")
            self.assertEqual(info["root_commit"], "abc123")
            self.assertEqual(info["artifact"]["path"], "TEST_TARGET_最新.up3")
            self.assertEqual(
                info["artifact"]["sha256"],
                hashlib.sha256(b"new-firmware-content").hexdigest().upper(),
            )
            self.assertEqual(
                info["immutable_release"],
                "profiles/TEST_TARGET/releases/v1.0.0-dev.1",
            )

    def test_backfills_and_replaces_short_entry_from_existing_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "profiles" / "TEST_TARGET"
            root.mkdir(parents=True)
            artifact = base / "new.up3"
            artifact.write_bytes(b"new-firmware-content")
            result = self._publish(root, artifact)
            download_root = base / "下载"
            existing = download_root / "TEST_TARGET" / "TEST_TARGET_最新.up3"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"old")

            entry = publish_download_entry(
                download_root=download_root,
                release=Path(result.release),
                latest_artifact_name="TEST_TARGET_最新.up3",
                publish_id="backfill01",
            )

            self.assertEqual(existing.read_bytes(), artifact.read_bytes())
            self.assertEqual(entry.artifact.size, len(b"new-firmware-content"))
            self.assertFalse((existing.parent / ".download-entry.lock").exists())

    def test_corrupt_release_cannot_replace_existing_short_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "profiles" / "TEST_TARGET"
            root.mkdir(parents=True)
            artifact = base / "new.up3"
            artifact.write_bytes(b"new-firmware-content")
            result = self._publish(root, artifact)
            release_artifact = Path(result.release) / "firmware" / "new.up3"
            release_artifact.write_bytes(b"corrupt")
            download_root = base / "下载"
            existing = download_root / "TEST_TARGET" / "TEST_TARGET_最新.up3"
            existing.parent.mkdir(parents=True)
            existing.write_bytes(b"old-must-survive")

            with self.assertRaisesRegex(PublishError, "immutable firmware verification"):
                publish_download_entry(
                    download_root=download_root,
                    release=Path(result.release),
                    latest_artifact_name="TEST_TARGET_最新.up3",
                    publish_id="corrupt01",
                )

            self.assertEqual(existing.read_bytes(), b"old-must-survive")


if __name__ == "__main__":
    unittest.main()
