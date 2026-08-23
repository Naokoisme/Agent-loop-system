from pathlib import Path

from scripts.build_exe import write_release_env_example


def _assignments(path: Path) -> dict[str, str]:
    return {
        key: value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
        for key, value in [line.split("=", 1)]
    }


def test_release_env_uses_credentials_and_relative_runtime_profile(tmp_path: Path) -> None:
    template = tmp_path / ".env.example.template"
    template.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY_EXPLORATION=",
                "OPENAI_API_KEY_FIXED=",
                "OPENAI_EXPLORATION_MODEL=",
                "OPENAI_FIXED_MODEL=",
                "ONES_AUTH_TOKEN=",
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
    assert values["OPENAI_API_KEY_EXPLORATION"] == "exploration-secret"
    assert values["OPENAI_API_KEY_FIXED"] == "fixed-secret"
    assert values["OPENAI_EXPLORATION_MODEL"] == "exploration-model"
    assert values["OPENAI_FIXED_MODEL"] == "fixed-model"
    assert values["ONES_AUTH_TOKEN"] == "ones-secret"
    assert values["AGENT_LOOP_LAYOUT_ROOT"] == ""
    assert values["AGENT_LOOP_WORKSPACE_BASE"] == ""
    assert values["W30_SOURCE_ROOT"] == ""
    assert values["W30_HARDWARE_SOURCE_ROOT"] == ""
    assert values["W30_HARDWARE_WORKSPACE_ROOT"] == ""
    assert values["W30_HARDWARE_PROFILE_ROOT"] == "profiles"
    assert values["W30_HARDWARE_PROFILE_VERSION"] == "v1.2.0-dev.3"
