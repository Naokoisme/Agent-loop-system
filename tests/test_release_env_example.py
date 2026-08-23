from pathlib import Path

from scripts.build_exe import write_release_env_example


def _assignments(path: Path) -> dict[str, str]:
    return {
        key: value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
        for key, value in [line.split("=", 1)]
    }


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
