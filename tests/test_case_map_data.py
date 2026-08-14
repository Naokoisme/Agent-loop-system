from __future__ import annotations

import json
import unittest
from pathlib import Path

from agent_loop_system.tools.case_map import CaseEntry, effective_case_entries
from agent_loop_system.tools.command_protocol import normalize_command


CASE_MAP_ROOT = Path(__file__).resolve().parents[1] / "case_map"
CASE_MAP_DIR = CASE_MAP_ROOT / "620C_case_map"
HARDWARE_CASE_MAP_DIR = CASE_MAP_ROOT / "6202_case_map"
SIMULATOR_6202_CASE_MAP_DIR = CASE_MAP_ROOT / "6202_simulator_case_map"
LEGACY_ACTIVITY_COMMANDS = {"STEP", "CALORIES", "DISTANCE"}
LANGUAGE_ID_ZH_CN = 1
LANGUAGE_ID_EN_US = 3


def _entries():
    for path in sorted(CASE_MAP_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_entries = data.get("cases", data) if isinstance(data, dict) else data
        for raw in raw_entries:
            yield path, CaseEntry(**raw)


def _hardware_entries():
    for path in sorted(HARDWARE_CASE_MAP_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise AssertionError(f"{path.name}: 6202 case map must use an object wrapper")
        if data.get("profile") != "6202_W5230":
            raise AssertionError(f"{path.name}: wrong hardware profile")
        for raw in data.get("cases", []):
            yield path, CaseEntry(**raw)


class CaseMapDataTest(unittest.TestCase):
    def test_case_ids_are_nonempty_and_unique(self) -> None:
        seen: set[str] = set()
        for path, case in _entries():
            self.assertTrue(case.case_id, path.name)
            self.assertNotIn(case.case_id, seen, f"{path.name}: {case.case_id}")
            seen.add(case.case_id)

    def test_all_commands_use_canonical_safe_wire_format(self) -> None:
        for path, case in _entries():
            for phase in ("setup", "actions", "collect"):
                for wire in getattr(case, phase):
                    label = f"{path.name}:{case.case_id}:{phase}"
                    self.assertTrue(
                        wire.startswith("srv_quick_cmd send TOP5STEP:"), label
                    )
                    self.assertTrue(wire.endswith(";"), label)
                    normalized = normalize_command(wire)
                    self.assertNotEqual(normalized, ":TP_CLICK:0,0,1", label)

    def test_unable_cases_do_not_keep_fake_actions(self) -> None:
        for path, case in _entries():
            if not case.unable:
                continue
            self.assertEqual(case.actions, [], f"{path.name}:{case.case_id}")
            self.assertTrue(case.note.strip(), f"{path.name}:{case.case_id}")

    def test_windows_cases_do_not_use_legacy_activity_commands(self) -> None:
        for path, case in _entries():
            for phase in ("setup", "actions", "collect"):
                for wire in getattr(case, phase):
                    command = normalize_command(wire)[1:].partition(":")[0]
                    self.assertNotIn(
                        command,
                        LEGACY_ACTIVITY_COMMANDS,
                        f"{path.name}:{case.case_id}:{phase}",
                    )

    def test_all_6202_maps_keep_hardware_and_screenshot_boundaries(self) -> None:
        seen: set[str] = set()
        for path, case in _hardware_entries():
            self.assertNotIn(case.case_id, seen, f"{path.name}: {case.case_id}")
            seen.add(case.case_id)
            if case.unable:
                self.assertEqual(case.actions, [], f"{path.name}:{case.case_id}")
                self.assertTrue(case.note.strip(), f"{path.name}:{case.case_id}")
                continue

            wires = case.setup + case.actions + case.collect
            self.assertTrue(wires, f"{path.name}:{case.case_id}")
            names = [normalize_command(wire)[1:].partition(":")[0] for wire in wires]
            self.assertIn("HOST_SCREENSHOT", names, f"{path.name}:{case.case_id}")
            self.assertFalse(
                any(name.startswith("SIM_") for name in names),
                f"{path.name}:{case.case_id}",
            )
            self.assertNotIn("GUI_TREE", names, f"{path.name}:{case.case_id}")
            self.assertNotIn("SCREENSHOT_PRINT", names, f"{path.name}:{case.case_id}")

    def test_6202_calculator_map_is_isolated_and_hardware_safe(self) -> None:
        path = HARDWARE_CASE_MAP_DIR / "计算器.json"
        raw_text = path.read_text(encoding="utf-8")
        self.assertNotIn("??", raw_text)
        data = json.loads(raw_text)
        self.assertEqual(data["profile"], "6202_W5230")
        self.assertEqual(data["sheet"], "计算器")
        cases = [CaseEntry(**raw) for raw in data["cases"]]
        self.assertEqual(len(cases), 37)

        for case in cases:
            self.assertFalse(case.unable, case.case_id)
            wires = case.setup + case.actions + case.collect
            self.assertTrue(wires, case.case_id)
            self.assertTrue(
                any(normalize_command(wire).startswith(":HOST_SCREENSHOT:") for wire in wires),
                case.case_id,
            )
            for wire in wires:
                command = normalize_command(wire)[1:].partition(":")[0]
                self.assertFalse(command.startswith("SIM_"), case.case_id)
                self.assertNotEqual(command, "SCREENSHOT_PRINT", case.case_id)
                self.assertNotEqual(command, "GUI_TREE", case.case_id)

        calc_001 = {case.case_id: case for case in cases}["CALC_001"]
        self.assertEqual(
            calc_001.actions,
            ["srv_quick_cmd send TOP5STEP:ENTER_PAGE:CALCULATOR,0;"],
        )
        self.assertEqual(
            calc_001.setup,
            [
                "srv_quick_cmd send TOP5STEP:LANGUAGE_SET:1;",
                "srv_quick_cmd send TOP5STEP:ENTER_PAGE:DIAL,0;",
            ],
        )
        self.assertEqual(
            calc_001.collect,
            ["srv_quick_cmd send TOP5STEP:HOST_SCREENSHOT:1;"],
        )
        self.assertEqual(
            calc_001.expected_text,
            "1.页面显示数值0和完整计算键盘（五行按键）",
        )

        calc_003 = {case.case_id: case for case in cases}["CALC_003"]
        self.assertEqual(
            calc_003.actions,
            [
                "srv_quick_cmd send TOP5STEP:TP_CLICK:64,365,1;",
                "srv_quick_cmd send TOP5STEP:TP_CLICK:159,365,1;",
                "srv_quick_cmd send TOP5STEP:TP_CLICK:253,365,1;",
            ],
        )
        self.assertIn("探索性执行已解锁", calc_003.note)

        executable = [case for case in cases if not case.unable]
        self.assertEqual(len(executable), 37)

    def test_6202_simulator_calculator_map_is_isolated_and_screenshot_only(self) -> None:
        path = SIMULATOR_6202_CASE_MAP_DIR / "计算器.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["profile"], "6202_W5230_SIMULATOR")
        self.assertEqual(data["window_version"], "5803_SBYH_FP410X502")
        cases = [CaseEntry(**raw) for raw in data["cases"]]
        self.assertEqual(len(cases), 37)
        self.assertTrue(all(not case.unable for case in cases))
        for case in cases:
            names = [
                normalize_command(wire)[1:].partition(":")[0]
                for wire in case.setup + case.actions + case.collect
            ]
            self.assertIn("HOST_SCREENSHOT", names, case.case_id)
            self.assertNotIn("GUI_TREE", names, case.case_id)
            self.assertNotIn("SCREENSHOT_PRINT", names, case.case_id)

    def test_6202_simulator_calendar_is_disabled_by_project_configuration(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "日历.json").read_text(encoding="utf-8")
        )
        self.assertIs(data["supported"], False)
        self.assertIn("已移除日历模块", data["unavailable_reason"])

    def test_6202_simulator_disabled_control_center_and_flashlight_preconditions(self) -> None:
        control = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "控制中心.json").read_text(encoding="utf-8")
        )
        self.assertIs(control["execution_supported"], False)
        find_watch = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "查找手表.json").read_text(encoding="utf-8")
        )
        self.assertIs(find_watch["execution_supported"], False)
        self.assertIn("SRV_REMIND_FIND_WATCH_START", find_watch["unavailable_reason"])
        flashlight = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "手电筒.json").read_text(encoding="utf-8")
        )
        blocked = {"FLASH_005", "FLASH_011", "FLASH_021", "FLASH_022", "FLASH_024"}
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in flashlight["cases"]}
        for case_id in blocked:
            case = cases[case_id]
            self.assertTrue(case.unable, case_id)
            self.assertEqual(case.setup + case.actions + case.collect, [], case_id)

    def test_6202_simulator_migration_keeps_all_modules_and_screenshot_evidence(self) -> None:
        paths = sorted(SIMULATOR_6202_CASE_MAP_DIR.glob("*.json"))
        self.assertEqual(len(paths), 40)

        total = 0
        executable = 0
        for path in paths:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["profile"], "6202_W5230_SIMULATOR", path.name)
            for raw in effective_case_entries(data):
                total += 1
                case = CaseEntry(**raw)
                if case.unable:
                    continue
                executable += 1
                names = [
                    normalize_command(wire)[1:].partition(":")[0]
                    for wire in case.setup + case.actions + case.collect
                ]
                self.assertIn("HOST_SCREENSHOT", names, case.case_id)
                self.assertNotIn("GUI_TREE", names, case.case_id)
                self.assertNotIn("SCREENSHOT_PRINT", names, case.case_id)
                self.assertNotIn("SCREENSHOT_CAPTURE", names, case.case_id)
                self.assertNotIn("SCREENSHOT_CAPTURE_FILE", names, case.case_id)

        self.assertEqual(total, 3164)
        self.assertEqual(executable, 779)

    def test_6202_sos_cases_use_real_state_and_screenshot_checkpoints(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "SOS.json").read_text(encoding="utf-8")
        )
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}
        executable = {case_id for case_id, case in cases.items() if not case.unable}
        self.assertEqual(
            executable,
            {"SOS_002", "SOS_012", "SOS_016", "SOS_017", "SOS_023", "SOS_024"},
        )

        for case_id in executable:
            case = cases[case_id]
            names = [
                normalize_command(wire)[1:].partition(":")[0]
                for wire in case.setup + case.actions + case.collect
            ]
            self.assertIn("SIM_SOS_CONTACT_SET", names, case_id)
            self.assertIn("HOST_SCREENSHOT", names, case_id)

        for case_id in {"SOS_002", "SOS_012", "SOS_016", "SOS_017"}:
            commands = cases[case_id].setup + cases[case_id].actions
            self.assertTrue(
                any("BUTTON_PRESS:2,2,5000" in command for command in commands),
                case_id,
            )

        for case_id in {"SOS_011", "SOS_031", "SOS_032"}:
            case = cases[case_id]
            self.assertTrue(case.unable, case_id)
            self.assertEqual(case.setup + case.actions + case.collect, [], case_id)

    def test_6202_activity_cases_rebuild_visible_preconditions_and_chart_page(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "活动记录.json").read_text(encoding="utf-8")
        )
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}

        activity_inputs = {
            "FIT_010": ":SIM_ACTIVITY_SET:1001,fixed,0,20,0",
            "FIT_012": ":SIM_ACTIVITY_SET:1201,fixed,100,0,0",
            "FIT_013": ":SIM_ACTIVITY_SET:1301,fixed,101,0,0",
            "FIT_014": ":SIM_ACTIVITY_SET:1401,fixed,0,0,0",
            "FIT_016": ":SIM_ACTIVITY_SET:1601,fixed,2000,0,0",
            "FIT_017": ":SIM_ACTIVITY_SET:1701,fixed,0,20,0",
            "FIT_019": ":SIM_ACTIVITY_SET:1901,fixed,100,0,0",
            "FIT_021": ":SIM_ACTIVITY_SET:2101,fixed,4000,0,0",
            "FIT_022": ":SIM_ACTIVITY_SET:2201,fixed,0,0,0",
        }
        for case_id, expected in activity_inputs.items():
            setup = [normalize_command(wire) for wire in cases[case_id].setup]
            self.assertIn(expected, setup, case_id)

        chart_cases = (
            "FIT_028", "FIT_029", "FIT_030", "FIT_032",
            "FIT_033", "FIT_034", "FIT_035",
        )
        for case_id in chart_cases:
            actions = [normalize_command(wire) for wire in cases[case_id].actions]
            self.assertEqual(actions, [":QDEC_SET:1"] * 3, case_id)

        for case_id in ("FIT_046", "FIT_047", "FIT_048", "FIT_050", "FIT_051"):
            actions = [normalize_command(wire) for wire in cases[case_id].actions]
            self.assertEqual(actions, [":QDEC_SET:1"] * 2, case_id)

        for case_id in ("FIT_058", "FIT_059", "FIT_060", "FIT_061", "FIT_062", "FIT_063"):
            actions = [normalize_command(wire) for wire in cases[case_id].actions]
            self.assertEqual(actions.count(":QDEC_SET:1"), 4, case_id)

        high_values = {
            "FIT_068": ":SIM_ACTIVITY_SET:6801,fixed,99999,0,0",
            "FIT_069": ":SIM_ACTIVITY_SET:6901,fixed,100000,0,0",
            "FIT_070": ":SIM_ACTIVITY_SET:7001,fixed,200000,0,0",
            "FIT_071": ":SIM_ACTIVITY_SET:7101,fixed,100000,0,0",
        }
        for case_id, expected in high_values.items():
            setup = [normalize_command(wire) for wire in cases[case_id].setup]
            self.assertIn(expected, setup, case_id)

        for case_id in (
            "FIT_020", "FIT_023", "FIT_024", "FIT_026", "FIT_072",
            "FIT_083", "FIT_084", "FIT_085", "FIT_086", "FIT_087",
            "FIT_088", "FIT_089", "FIT_090", "FIT_091", "FIT_092",
            "FIT_101", "FIT_103", "FIT_117", "FIT_118", "FIT_120",
        ):
            case = cases[case_id]
            self.assertTrue(case.unable, case_id)
            self.assertEqual(case.setup + case.actions + case.collect, [], case_id)

        for case_id in ("FIT_094", "FIT_097", "FIT_100"):
            self.assertFalse(cases[case_id].unable, case_id)
        self.assertIn(
            ":EXERCISE_TIME:29",
            [normalize_command(wire) for wire in cases["FIT_094"].setup],
        )
        self.assertIn(
            ":EXERCISE_TIME:30",
            [normalize_command(wire) for wire in cases["FIT_094"].actions],
        )
        self.assertIn(
            ":MSG_TEST:31",
            [normalize_command(wire) for wire in cases["FIT_097"].setup],
        )
        self.assertEqual(
            sum(
                "HOST_SCREENSHOT" in wire
                for wire in cases["FIT_100"].setup + cases["FIT_100"].collect
            ),
            2,
        )

        for case_id in ("FIT_107", "FIT_108", "FIT_109"):
            wires = [
                normalize_command(wire)
                for wire in cases[case_id].setup + cases[case_id].actions
            ]
            self.assertIn(":TIME_SET:20260813235900", wires, case_id)
            self.assertIn(":TIME_SET:20260814000000", wires, case_id)
        self.assertEqual(
            sum("HOST_SCREENSHOT" in wire for wire in cases["FIT_108"].actions),
            3,
        )

    def test_6202_process_only_cases_are_unavailable_and_sleep_uses_real_pages(self) -> None:
        unavailable = {
            "活动记录.json": ("FIT_123", "FIT_124", "FIT_126"),
            "电源管理.json": ("POWER_027", "POWER_028", "POWER_034", "POWER_035"),
            "睡眠.json": (
                "SLEEP_004", "SLEEP_005", "SLEEP_006", "SLEEP_007",
                "SLEEP_019", "SLEEP_021", "SLEEP_022", "SLEEP_023",
                "SLEEP_030", "SLEEP_031", "SLEEP_032", "SLEEP_033",
                "SLEEP_036", "SLEEP_037", "SLEEP_038", "SLEEP_039",
                "SLEEP_040", "SLEEP_041", "SLEEP_046", "SLEEP_047",
            ),
        }
        for filename, case_ids in unavailable.items():
            data = json.loads(
                (SIMULATOR_6202_CASE_MAP_DIR / filename).read_text(encoding="utf-8")
            )
            cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}
            for case_id in case_ids:
                case = cases[case_id]
                self.assertTrue(case.unable, case_id)
                self.assertEqual(case.setup + case.actions + case.collect, [], case_id)

        sleep_data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "睡眠.json").read_text(encoding="utf-8")
        )
        sleep_cases = {
            raw["case_id"]: CaseEntry(**raw) for raw in sleep_data["cases"]
        }
        expected_profiles = {
            "SLEEP_018": ":SLEEP_RECORD_CREATE:1,120,240,120,60,0",
            "SLEEP_020": ":SLEEP_RECORD_CREATE:1,120,240,120,0,0",
        }
        for case_id, expected_profile in expected_profiles.items():
            case = sleep_cases[case_id]
            self.assertFalse(case.unable, case_id)
            setup = [normalize_command(wire) for wire in case.setup]
            actions = [normalize_command(wire) for wire in case.actions]
            self.assertIn(expected_profile, setup, case_id)
            self.assertEqual(actions, [":QDEC_SET:1", ":QDEC_SET:1"], case_id)

        power_data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "电源管理.json").read_text(encoding="utf-8")
        )
        power_cases = {
            raw["case_id"]: CaseEntry(**raw) for raw in power_data["cases"]
        }
        self.assertFalse(power_cases["POWER_004"].unable)
        self.assertEqual(
            [normalize_command(wire) for wire in power_cases["POWER_004"].actions],
            [":BUTTON_PRESS:1,2,2900"],
        )

    def test_6202_simulator_uses_language_ids_matching_case_semantics(self) -> None:
        for path in sorted(SIMULATOR_6202_CASE_MAP_DIR.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for raw in data["cases"]:
                case = CaseEntry(**raw)
                if case.unable:
                    continue
                language_commands = [
                    normalize_command(wire)
                    for wire in case.setup
                    if normalize_command(wire).startswith(":LANGUAGE_SET:")
                ]
                self.assertEqual(
                    len(language_commands),
                    1,
                    f"{path.name}:{case.case_id}",
                )
                semantics = "\n".join(
                    (case.precondition_text, case.steps_text, case.expected_text)
                )
                chinese_only = ("中文" in semantics) and not any(
                    marker in semantics for marker in ("英文", "English")
                )
                expected_language_id = (
                    LANGUAGE_ID_ZH_CN if chinese_only else LANGUAGE_ID_EN_US
                )
                self.assertEqual(
                    language_commands[0],
                    f":LANGUAGE_SET:{expected_language_id}",
                    f"{path.name}:{case.case_id}",
                )

    def test_6202_simulator_ocm_card_views_use_result_window(self) -> None:
        for filename in ("一键测量.json", "主表盘与导航.json"):
            data = json.loads(
                (SIMULATOR_6202_CASE_MAP_DIR / filename).read_text(encoding="utf-8")
            )
            for raw in data["cases"]:
                case = CaseEntry(**raw)
                if case.unable:
                    continue
                semantics = "\n".join(
                    (case.precondition_text, case.steps_text, case.expected_text)
                )
                if not any(
                    marker in semantics
                    for marker in ("OCM主页面", "One Click Measurement主页面", "卡片")
                ):
                    continue
                commands = [
                    normalize_command(wire)
                    for wire in case.setup + case.actions + case.collect
                ]
                if not any("ONE_CLICK_MEASURE" in command for command in commands):
                    continue
                self.assertNotIn(
                    ":ENTER_PAGE:ONE_CLICK_MEASURE,0",
                    commands,
                    f"{filename}:{case.case_id}",
                )
                self.assertIn(
                    ":ENTER_PAGE:ONE_CLICK_MEASURE_RESULT,0",
                    commands,
                    f"{filename}:{case.case_id}",
                )

    def test_6202_simulator_my_cycle_observation_cases_enter_female_health(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "主表盘与导航.json").read_text(
                encoding="utf-8"
            )
        )
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}

        for case_id in (
            "WFACE_064",
            "WFACE_065",
            "WFACE_066",
            "WFACE_067",
            "WFACE_068",
            "WFACE_069",
        ):
            commands = [normalize_command(wire) for wire in cases[case_id].setup]
            self.assertIn(":ENTER_PAGE:FEMALE_HEALTH,0", commands, case_id)

    def test_6202_simulator_global_button_cases_establish_their_source_page(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "全局交互与系统规则.json").read_text(
                encoding="utf-8"
            )
        )
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}
        source_pages = {
            "GLOB_039": "SIDEBAR",
            "GLOB_040": "SHORTCUT",
            "GLOB_041": "MESSAGE_LIST",
            "GLOB_042": "SIDEBAR",
        }

        for case_id, source_page in source_pages.items():
            commands = [normalize_command(wire) for wire in cases[case_id].setup]
            self.assertIn(f":ENTER_PAGE:{source_page},0", commands, case_id)

    def test_6202_simulator_main_menu_operations_establish_the_menu_first(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "全局交互与系统规则.json").read_text(
                encoding="utf-8"
            )
        )
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}

        for case_id in ("GLOB_043", "GLOB_074"):
            setup = [normalize_command(wire) for wire in cases[case_id].setup]
            self.assertEqual(setup[-2:], [":ENTER_PAGE:DIAL,0", ":BUTTON_PRESS:1,1,0"])

    def test_6202_simulator_scroll_cases_keep_before_and_after_screenshots(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "全局交互与系统规则.json").read_text(
                encoding="utf-8"
            )
        )
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}

        for case_id in ("GLOB_076", "GLOB_077"):
            setup = [normalize_command(wire) for wire in cases[case_id].setup]
            self.assertIn(":ENTER_PAGE:SYSTEM_INFO,0", setup, case_id)
            self.assertTrue(any(command.startswith(":HOST_SCREENSHOT:") for command in setup))
            self.assertTrue(cases[case_id].collect)

        brightness_setup = [normalize_command(wire) for wire in cases["GLOB_078"].setup]
        self.assertIn(":ENTER_PAGE:DISPLAY_BRIGHTNESS_ADJUST,0", brightness_setup)
        self.assertTrue(any(command.startswith(":HOST_SCREENSHOT:") for command in brightness_setup))
        self.assertTrue(cases["GLOB_078"].collect)

    def test_6202_simulator_screen_off_uses_power_key_not_encoder_key(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "全局交互与系统规则.json").read_text(
                encoding="utf-8"
            )
        )
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}
        actions = [normalize_command(wire) for wire in cases["GLOB_091"].actions]
        self.assertEqual(actions, [":BUTTON_PRESS:0,1,0"])

    def test_6202_simulator_relative_time_cases_capture_before_and_after(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "全局交互与系统规则.json").read_text(
                encoding="utf-8"
            )
        )
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}

        for case_id in ("GLOB_183", "GLOB_184"):
            case = cases[case_id]
            self.assertTrue(
                any(":HOST_SCREENSHOT:" in command for command in case.setup), case_id
            )
            self.assertTrue(case.collect, case_id)
            self.assertEqual(len(case.verification_points), 2, case_id)
        self.assertIn(
            ":ENTER_PAGE:CALENDAR,0",
            [normalize_command(wire) for wire in cases["GLOB_184"].actions],
        )

    def test_6202_simulator_timed_reminders_capture_visible_lifecycle(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "全局交互与系统规则.json").read_text(
                encoding="utf-8"
            )
        )
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}

        for case_id, message_id in (("GLOB_170", 22), ("GLOB_171", 23)):
            case = cases[case_id]
            actions = [normalize_command(wire) for wire in case.actions]
            self.assertIn(f":MSG_TEST:{message_id}", actions, case_id)
            self.assertTrue(any(command.startswith(":HOST_SCREENSHOT:") for command in actions))
            self.assertTrue(any(command.endswith(",5500") for command in actions))
            self.assertEqual(len(case.verification_points), 2, case_id)

    def test_6202_simulator_pressure_observation_stays_on_main_page(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "压力.json").read_text(encoding="utf-8")
        )
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}

        self.assertIn("TOP5STEP:SET_STRESS_VALUE:45", "\n".join(cases["STRS_002"].setup))
        self.assertEqual(cases["STRS_004"].actions, [])
        self.assertIn("TOP5STEP:ENTER_PAGE:STRESS,0", "\n".join(cases["STRS_004"].setup))

        for case_id in ("STRS_042", "STRS_043", "STRS_044", "STRS_045", "STRS_048", "STRS_051", "STRS_052", "STRS_058", "STRS_059"):
            setup = "\n".join(cases[case_id].setup)
            self.assertIn("TOP5STEP:SET_STRESS_VALUE:", setup, case_id)
            self.assertIn("TOP5STEP:ENTER_PAGE:STRESS,0", setup, case_id)

        for case_id in ("STRS_042", "STRS_044", "STRS_048", "STRS_052", "STRS_058", "STRS_059"):
            self.assertIn("TOP5STEP:TIME_SET:", "\n".join(cases[case_id].setup), case_id)

    def test_6202_simulator_detail_and_setting_cases_use_the_required_window(self) -> None:
        pressure_data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "压力.json").read_text(encoding="utf-8")
        )
        pressure = {raw["case_id"]: CaseEntry(**raw) for raw in pressure_data["cases"]}

        for case_id in ("STRS_068", "STRS_069"):
            self.assertIn(
                ":ENTER_PAGE:STRESS_DETAIL,0",
                [normalize_command(wire) for wire in pressure[case_id].setup],
                case_id,
            )
        self.assertEqual(len(pressure["STRS_068"].verification_points), 4)
        self.assertEqual(
            sum("HOST_SCREENSHOT" in wire for wire in pressure["STRS_068"].actions),
            3,
        )
        self.assertIn("TOP5STEP:SET_STRESS_VALUE:45", "\n".join(pressure["STRS_088"].setup))

        breathe_data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "呼吸训练.json").read_text(encoding="utf-8")
        )
        breathe = {raw["case_id"]: CaseEntry(**raw) for raw in breathe_data["cases"]}
        for case_id in ("BRTH_002", "BRTH_003", "BRTH_004", "BRTH_005"):
            self.assertIn(
                ":ENTER_PAGE:BREATH_TRAIN_SETTING,0",
                [normalize_command(wire) for wire in breathe[case_id].setup],
                case_id,
            )

        for case_id in ("BRTH_057", "BRTH_060"):
            self.assertIn(
                ":ENTER_PAGE:BREATH_TRAIN_END,0",
                [normalize_command(wire) for wire in breathe[case_id].setup],
                case_id,
            )

    def test_6202_simulator_observation_cases_recreate_their_visible_preconditions(self) -> None:
        hydration_data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "喝水提醒.json").read_text(encoding="utf-8")
        )
        hydration = {
            raw["case_id"]: CaseEntry(**raw) for raw in hydration_data["cases"]
        }
        for case_id in ("HYD_024", "HYD_025", "HYD_052"):
            self.assertIn(
                ":MSG_TEST:23",
                [normalize_command(wire) for wire in hydration[case_id].setup],
                case_id,
            )
            self.assertTrue(hydration[case_id].verification_points, case_id)

        weather_data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "天气.json").read_text(encoding="utf-8")
        )
        weather = {raw["case_id"]: CaseEntry(**raw) for raw in weather_data["cases"]}
        scroll = weather["WTHR_012"]
        self.assertIn("TOP5STEP:WEATHER_SET:", "\n".join(scroll.setup))
        self.assertEqual(
            sum("HOST_SCREENSHOT" in wire for wire in scroll.setup),
            1,
        )
        self.assertEqual(len(scroll.verification_points), 2)

    def test_6202_simulator_factory_home_enters_registered_product_page(self) -> None:
        data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "工厂与船运模式.json").read_text(
                encoding="utf-8"
            )
        )
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data["cases"]}
        factory_home = cases["FACT_006"]
        self.assertIn(
            ":ENTER_PAGE:FACTORY_TEST,0",
            [normalize_command(wire) for wire in factory_home.setup],
        )
        self.assertNotIn(
            ":ENTER_PAGE:DIAL,0",
            [normalize_command(wire) for wire in factory_home.setup],
        )

    def test_6202_simulator_visual_changes_and_heart_rate_recreate_preconditions(self) -> None:
        startup_data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "开机绑定.json").read_text(
                encoding="utf-8"
            )
        )
        startup = {raw["case_id"]: CaseEntry(**raw) for raw in startup_data["cases"]}
        encoder = startup["START_014"]
        self.assertEqual(
            sum("HOST_SCREENSHOT" in wire for wire in encoder.setup + encoder.actions),
            3,
        )
        self.assertEqual(len(encoder.verification_points), 3)

        heart_data = json.loads(
            (SIMULATOR_6202_CASE_MAP_DIR / "心率.json").read_text(encoding="utf-8")
        )
        heart = {raw["case_id"]: CaseEntry(**raw) for raw in heart_data["cases"]}
        for case_id, value in (("HR_004", 60), ("HR_005", 180)):
            case = heart[case_id]
            self.assertIn(
                f":HR_SET:{value}",
                [normalize_command(wire) for wire in case.setup],
                case_id,
            )
        for case_id in ("HR_004", "HR_005"):
            self.assertIn(
                ":QDEC_SET:0",
                [normalize_command(wire) for wire in heart[case_id].actions],
                case_id,
            )

        screen_on = heart["HR_018"]
        self.assertIn(
            ":DISPLAY_TIME_SET:5",
            [normalize_command(wire) for wire in screen_on.setup],
        )
        self.assertTrue(any("HOST_WAIT:18002,5500" in wire for wire in screen_on.actions))
        self.assertEqual(
            sum("HOST_SCREENSHOT" in wire for wire in screen_on.setup + screen_on.actions),
            2,
        )
        self.assertEqual(len(screen_on.verification_points), 2)

        chart_cases = ("HR_007", "HR_047", "HR_049", "HR_050", "HR_052", "HR_056", "HR_057", "HR_058")
        for case_id in chart_cases:
            self.assertIn(
                ":QDEC_SET:0",
                [normalize_command(wire) for wire in heart[case_id].actions],
                case_id,
            )

        expected_values = {
            "HR_049": (72,),
            "HR_052": tuple(range(70, 81)),
            "HR_056": (60, 100),
            "HR_057": (201,),
            "HR_058": (63, 157),
        }
        for case_id, values in expected_values.items():
            setup = [normalize_command(wire) for wire in heart[case_id].setup]
            for value in values:
                self.assertIn(f":HR_SET:{value}", setup, case_id)

        for case_id in ("HR_070", "HR_071"):
            setup = [normalize_command(wire) for wire in heart[case_id].setup]
            self.assertIn(":HR_SET:120", setup, case_id)
            self.assertIn(":MSG_TEST:28", setup, case_id)
            self.assertNotIn(":ENTER_PAGE:HEART_RATE,0", setup, case_id)

        resting_no_data = [
            normalize_command(wire) for wire in heart["HR_079"].actions
        ]
        self.assertEqual(resting_no_data.count(":QDEC_SET:1"), 2)

    def test_spo2_screen_timeout_cases_wait_until_measurement_finishes(self) -> None:
        data = json.loads((CASE_MAP_DIR / "血氧.json").read_text(encoding="utf-8"))
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data}

        for case_id, seq in (("SPO2_034", 34003), ("SPO2_035", 35003)):
            setup = cases[case_id].setup
            self.assertIn(
                f"srv_quick_cmd send TOP5STEP:SIM_WAIT:{seq},49000;",
                setup,
            )
            self.assertLess(
                setup.index("srv_quick_cmd send TOP5STEP:DISPLAY_TIME_SET:60;"),
                setup.index("srv_quick_cmd send TOP5STEP:ENTER_PAGE:BLOOD_OXYGEN,0;"),
            )
            self.assertLess(
                setup.index(f"srv_quick_cmd send TOP5STEP:SIM_WAIT:{seq},49000;"),
                setup.index("srv_quick_cmd send TOP5STEP:DISPLAY_TIME_SET:5;"),
            )

    def test_ota_reboot_case_is_not_faked_in_a_single_session(self) -> None:
        data = json.loads((CASE_MAP_DIR / "固件升级.json").read_text(encoding="utf-8"))
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data}

        reboot = cases["OTA_028"]
        self.assertTrue(reboot.unable)
        self.assertEqual(reboot.setup, [])
        self.assertEqual(reboot.actions, [])
        self.assertEqual(reboot.collect, [])

    def test_sos_status_matrix_has_real_simulator_injection(self) -> None:
        data = json.loads((CASE_MAP_DIR / "SOS.json").read_text(encoding="utf-8"))
        cases = {raw["case_id"]: CaseEntry(**raw) for raw in data}

        matrix = cases["SOS_001"]
        self.assertFalse(matrix.unable)
        self.assertEqual(len(matrix.verification_points), 3)
        matrix_wires = matrix.setup + matrix.actions
        self.assertIn(
            "srv_quick_cmd send TOP5STEP:SIM_SOS_CONTACT_SET:101,0;",
            matrix_wires,
        )
        self.assertIn(
            "srv_quick_cmd send TOP5STEP:SIM_CONNECTION_SET:104,hfp,0;",
            matrix_wires,
        )
        self.assertIn(
            "srv_quick_cmd send TOP5STEP:SIM_CONNECTION_SET:105,hfp,1;",
            matrix_wires,
        )
        self.assertEqual(
            sum(normalize_command(wire).startswith(":GUI_TREE:") for wire in matrix.actions),
            3,
        )

        for case_id in ("SOS_004", "SOS_007", "SOS_023"):
            self.assertFalse(cases[case_id].unable, case_id)
            wires = "\n".join(cases[case_id].setup + cases[case_id].actions)
            self.assertIn("SIM_SOS_CONTACT_SET", wires, case_id)
            self.assertIn("SIM_CONNECTION_SET", wires, case_id)

        for case_id in ("SOS_001", "SOS_004", "SOS_007"):
            setup = "\n".join(cases[case_id].setup)
            actions = "\n".join(cases[case_id].actions)
            self.assertNotIn("ENTER_PAGE:TELEPHONY_SOS", setup, case_id)
            self.assertIn("ENTER_PAGE:DIAL", setup, case_id)
            self.assertIn("SWIPE_SIM", setup, case_id)
            self.assertIn("TP_CLICK:305,184,1", actions, case_id)

        self.assertIn(
            "ENTER_PAGE:TELEPHONY_SOS",
            "\n".join(cases["SOS_023"].actions),
        )


if __name__ == "__main__":
    unittest.main()
