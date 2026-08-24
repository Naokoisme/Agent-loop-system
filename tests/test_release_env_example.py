from pathlib import Path

import json
import re
import sqlite3
import tomllib

from scripts.build_exe import (
    copy_release_case_map,
    find_embedded_release_secrets,
    find_release_case_map_local_paths,
    sanitize_supercom_release_db,
    write_internal_hardware_env,
    write_release_env_example,
)


def _assignments(path: Path) -> dict[str, str]:
    return {
        key: value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
        for key, value in [line.split("=", 1)]
    }


def test_release_version_sources_are_synchronized() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads(
        (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    lockfile = tomllib.loads(
        (repository_root / "uv.lock").read_text(encoding="utf-8")
    )
    version_source = (
        repository_root / "src" / "agent_loop_system" / "version.py"
    ).read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', version_source, re.MULTILINE)
    assert match is not None

    project_version = pyproject["project"]["version"]
    locked_project = next(
        package
        for package in lockfile["package"]
        if package["name"] == "agent-loop-system"
    )
    assert match.group(1) == project_version
    assert locked_project["version"] == project_version


def test_release_env_clears_credentials_local_paths_and_device_identity(tmp_path: Path) -> None:
    template = tmp_path / ".env.example.template"
    template.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY_EXPLORATION=",
                "OPENAI_API_KEY_FIXED=",
                "OPENAI_EXPLORATION_MODEL=",
                "OPENAI_FIXED_MODEL=",
                "ONES_AUTH_TOKEN=",
                "ONES_TEAM_UUID=",
                "ONES_USER_ID=",
                "W30_HARDWARE_PORT=COM7",
                "W30_HARDWARE_BLE_ADDRESS=",
                r"AGENT_LOOP_LAYOUT_ROOT=..",
                r"AGENT_LOOP_WORKSPACE_BASE=../workspaces/firmware",
                r"W30_SOURCE_ROOT=D:\\source",
                r"W30_HARDWARE_SOURCE_ROOT=D:\\firmware",
                r"W30_HARDWARE_WORKSPACE_ROOT=D:\\workspace",
                r"W30_HARDWARE_PROFILE_ROOT=D:\\profiles",
                "W30_HARDWARE_PROFILE_VERSION=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    source = tmp_path / ".env"
    source.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY_EXPLORATION=exploration-secret",
                "OPENAI_API_KEY_FIXED=fixed-secret",
                "OPENAI_EXPLORATION_MODEL=exploration-model",
                "OPENAI_FIXED_MODEL=fixed-model",
                "ONES_AUTH_TOKEN=ones-secret",
                "ONES_TEAM_UUID=team-secret",
                "ONES_USER_ID=user-secret",
                "W30_HARDWARE_PORT=COM19",
                "W30_HARDWARE_BLE_ADDRESS=AA:BB:CC:DD:EE:FF",
                r"AGENT_LOOP_LAYOUT_ROOT=..",
                r"AGENT_LOOP_WORKSPACE_BASE=../workspaces/firmware",
                r"W30_HARDWARE_SOURCE_ROOT=D:\\local-firmware",
                r"W30_HARDWARE_WORKSPACE_ROOT=D:\\local-workspace",
                "W30_HARDWARE_PROFILE_VERSION=v1.2.0-dev.3",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    destination = tmp_path / ".env.example"

    write_release_env_example(template, source, destination)

    values = _assignments(destination)
    assert values["OPENAI_API_KEY_EXPLORATION"] == ""
    assert values["OPENAI_API_KEY_FIXED"] == ""
    assert values["OPENAI_EXPLORATION_MODEL"] == "exploration-model"
    assert values["OPENAI_FIXED_MODEL"] == "fixed-model"
    assert values["ONES_AUTH_TOKEN"] == ""
    assert values["ONES_TEAM_UUID"] == ""
    assert values["ONES_USER_ID"] == ""
    assert values["W30_HARDWARE_PORT"] == ""
    assert values["W30_HARDWARE_BLE_ADDRESS"] == ""
    assert values["AGENT_LOOP_LAYOUT_ROOT"] == ""
    assert values["AGENT_LOOP_WORKSPACE_BASE"] == ""
    assert values["W30_SOURCE_ROOT"] == ""
    assert values["W30_HARDWARE_SOURCE_ROOT"] == ""
    assert values["W30_HARDWARE_WORKSPACE_ROOT"] == ""
    assert values["W30_HARDWARE_PROFILE_ROOT"] == "profiles"
    assert values["W30_HARDWARE_PROFILE_VERSION"] == "v1.2.0-dev.3"


def test_release_env_without_source_is_still_portable(tmp_path: Path) -> None:
    template = tmp_path / ".env.example.template"
    template.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=",
                "ONES_AUTH_TOKEN=",
                "AGENT_LOOP_LAYOUT_ROOT=..",
                "AGENT_LOOP_WORKSPACE_BASE=../workspaces/firmware",
                "W30_SOURCE_ROOT=../workspaces/firmware/620C_W6830",
                "W30_HARDWARE_SOURCE_ROOT=../workspaces/firmware/6202_W5230",
                "W30_HARDWARE_WORKSPACE_ROOT=../workspaces/firmware/6202_W5230",
                "W30_HARDWARE_PROFILE_ROOT=profiles",
                "W30_HARDWARE_PROFILE_VERSION=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    destination = tmp_path / ".env.example"

    write_release_env_example(template, None, destination)

    values = _assignments(destination)
    assert values["OPENAI_API_KEY"] == ""
    assert values["ONES_AUTH_TOKEN"] == ""
    assert values["AGENT_LOOP_LAYOUT_ROOT"] == ""
    assert values["AGENT_LOOP_WORKSPACE_BASE"] == ""
    assert values["W30_SOURCE_ROOT"] == ""
    assert values["W30_HARDWARE_SOURCE_ROOT"] == ""
    assert values["W30_HARDWARE_WORKSPACE_ROOT"] == ""
    assert values["W30_HARDWARE_PROFILE_ROOT"] == "profiles"


def test_internal_hardware_env_copies_model_keys_but_clears_machine_state(
    tmp_path: Path,
) -> None:
    template = tmp_path / ".env.example.template"
    template.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=",
                "OPENAI_API_KEY_EXPLORATION=",
                "OPENAI_API_KEY_FIXED=",
                "OPENAI_MODEL=shared-template-model",
                "OPENAI_EXPLORATION_MODEL=",
                "OPENAI_FIXED_MODEL=",
                "ONES_AUTH_TOKEN=",
                "W30_HARDWARE_PORT=",
                "W30_HARDWARE_BLE_ADDRESS=",
                r"W30_HARDWARE_SOURCE_ROOT=D:\\firmware",
                r"W30_HARDWARE_WORKSPACE_ROOT=D:\\workspace",
                r"W30_HARDWARE_PROFILE_ROOT=D:\\profiles",
                "W30_HARDWARE_PROFILE_VERSION=",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    source = tmp_path / ".env.source"
    source.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY_EXPLORATION=exploration-secret",
                "OPENAI_API_KEY_FIXED=fixed-secret",
                "OPENAI_EXPLORATION_MODEL=exploration-model",
                "OPENAI_FIXED_MODEL=fixed-model",
                "ONES_AUTH_TOKEN=ones-secret",
                "W30_HARDWARE_PORT=COM19",
                "W30_HARDWARE_BLE_ADDRESS=AA:BB:CC:DD:EE:FF",
                r"W30_HARDWARE_SOURCE_ROOT=D:\\local-firmware",
                r"W30_HARDWARE_WORKSPACE_ROOT=D:\\local-workspace",
                "W30_HARDWARE_PROFILE_VERSION=v1.2.0-dev.3",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    destination = tmp_path / ".env"

    write_internal_hardware_env(template, source, destination)

    values = _assignments(destination)
    assert values["OPENAI_API_KEY"] == "exploration-secret"
    assert values["OPENAI_API_KEY_EXPLORATION"] == "exploration-secret"
    assert values["OPENAI_API_KEY_FIXED"] == "fixed-secret"
    assert values["OPENAI_MODEL"] == "exploration-model"
    assert values["ONES_AUTH_TOKEN"] == ""
    assert values["W30_HARDWARE_PORT"] == ""
    assert values["W30_HARDWARE_BLE_ADDRESS"] == ""
    assert values["W30_HARDWARE_SOURCE_ROOT"] == ""
    assert values["W30_HARDWARE_WORKSPACE_ROOT"] == ""
    assert values["W30_HARDWARE_PROFILE_ROOT"] == "profiles"
    assert values["W30_HARDWARE_PROFILE_VERSION"] == "v1.2.0-dev.3"


def test_release_source_rejects_embedded_api_key_literals(tmp_path: Path) -> None:
    source = tmp_path / "src" / "demo.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        'TOKEN = "sk-example-release-secret-123456789"\n',
        encoding="utf-8",
    )

    assert find_embedded_release_secrets(tmp_path) == ["src/demo.py:1"]


def test_release_case_map_removes_machine_local_provenance(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "demo.json").write_text(
        json.dumps(
            {
                "profile": "demo",
                "cases": [
                    {
                        "case_id": "DEMO_001",
                        "coordinate_source": (
                            r"D:\\Agent-loop\\workspaces\\firmware\\demo.json"
                        ),
                        "note": (
                            r"历史证据 D:\\Agent-loop\\data\\legacy-evidence\\demo"
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (source / "external_execution_history.jsonl").write_text(
        json.dumps(
            {
                "case_id": "DEMO_001",
                "evidence_root": "D:/Agent-loop/data/legacy-evidence",
                "evidence_paths": ["demo/result.json"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    copy_release_case_map(source, destination)

    case_data = json.loads((destination / "demo.json").read_text(encoding="utf-8"))
    ledger = json.loads(
        (destination / "external_execution_history.jsonl")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert "coordinate_source" not in case_data["cases"][0]
    assert "D:" not in case_data["cases"][0]["note"]
    assert "./Agent-loop" in case_data["cases"][0]["note"]
    assert ledger["evidence_root"] == "."
    assert find_release_case_map_local_paths(destination) == []


def test_release_supercom_database_disables_saved_auto_connect(tmp_path: Path) -> None:
    database = tmp_path / "user_data.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE com_settings (PortName TEXT, Connected INTEGER)"
        )
        connection.executemany(
            "INSERT INTO com_settings VALUES (?, ?)",
            [("COM7", 1), ("COM9", 0)],
        )
        connection.execute(
            "CREATE TABLE advanced_send (ProjectName TEXT, Commands TEXT)"
        )
        connection.execute(
            "INSERT INTO advanced_send VALUES (?, ?)",
            ("保留命令", "TOP5STEP:GUI_PING:;"),
        )

    sanitize_supercom_release_db(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT PortName, Connected FROM com_settings ORDER BY PortName"
        ).fetchall() == [("COM7", 0), ("COM9", 0)]
        assert connection.execute(
            "SELECT ProjectName, Commands FROM advanced_send"
        ).fetchall() == [("保留命令", "TOP5STEP:GUI_PING:;")]
