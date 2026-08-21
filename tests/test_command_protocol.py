from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_loop_system.tools.command_protocol import (
    collect_command_json,
    normalize_command,
    validate_agent_command,
    validate_agent_commands,
)
from agent_loop_system.tools.simulator import CommandResult
from sim_tools.extract_kb import CommandCapability


class CommandProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.capabilities = {
            name: CommandCapability(name, f"{name.lower()}_handler", True)
            for name in (
                "ENTER_PAGE",
                "GUI_TREE",
                "SCREENSHOT_PRINT",
                "SLEEP_RECORD_CREATE",
                "WEATHER_CLEAR",
                "WEATHER_SET",
            )
        }

    def _handler_source(self) -> str:
        return (
            self._source_root()
            / "core"
            / "comm"
            / "srv"
            / "test"
            / "hlq_quick_cmd_handler.c"
        ).read_text(encoding="utf-8")

    def _active_tree_handler_source(self) -> str:
        test_dir = self._source_root() / "core" / "comm" / "srv" / "test"
        handler = test_dir / "hlq_quick_cmd_handler.c"
        return handler.read_text(encoding="utf-8")

    def _sim_device_source(self) -> str:
        return (
            self._source_root()
            / "core"
            / "comm"
            / "dal"
            / "sim"
            / "sim_dev.c"
        ).read_text(encoding="utf-8")

    def _sim_gui_main_source(self) -> str:
        return (
            self._source_root() / "core" / "gui" / "main" / "gui_main.c"
        ).read_text(encoding="utf-8")

    def _sdl_window_source(self) -> str:
        return (
            self._source_root()
            / "core"
            / "lvgl"
            / "src"
            / "drivers"
            / "sdl"
            / "lv_sdl_window.c"
        ).read_text(encoding="utf-8")

    def test_pc_simulator_renders_screen_off_as_black(self) -> None:
        gui_main = self._sim_gui_main_source()
        sdl_window = self._sdl_window_source()

        self.assertIn("lv_sdl_window_set_screen_visible", gui_main)
        self.assertIn("sim_dev_screen_is_on() || gui_win_life_cycle_is_screen_on()", gui_main)
        self.assertIn("SDL_SetRenderDrawColor(dsc->renderer, 0, 0, 0, 255)", sdl_window)
        self.assertIn("SDL_RenderPresent(dsc->renderer)", sdl_window)

    def _source_root(self) -> Path:
        source_root = os.environ.get("W30_SOURCE_ROOT")
        if not source_root:
            env_file = Path(__file__).resolve().parents[1] / ".env"
            if not env_file.is_file():
                self.skipTest("需要外部 W30 固件源码：未配置 W30_SOURCE_ROOT")
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("W30_SOURCE_ROOT="):
                    source_root = line.split("=", 1)[1].strip()
                    break
        if not source_root:
            self.skipTest("需要外部 W30 固件源码：W30_SOURCE_ROOT 为空")
        root = Path(source_root)
        if not root.is_dir():
            self.skipTest(f"需要外部 W30 固件源码：目录不存在 {root}")
        return root

    def test_three_forms_normalize_equally(self) -> None:
        quoted_wire = normalize_command('srv_quick_cmd send "TOP5STEP:GUI_TREE:1;"')
        bare_wire = normalize_command("srv_quick_cmd send TOP5STEP:GUI_TREE:1;")
        bare = normalize_command(":GUI_TREE:1")
        self.assertEqual(quoted_wire, ":GUI_TREE:1")
        self.assertEqual(bare_wire, quoted_wire)
        self.assertEqual(bare, quoted_wire)

    def test_sos_state_commands_are_owned_by_the_project_hlq_bridge(self) -> None:
        test_dir = self._source_root() / "core" / "comm" / "srv" / "test"
        hlq = (test_dir / "hlq_quick_cmd_handler.c").read_text(encoding="utf-8")
        self.assertIn('{":SIM_CONNECTION_SET:"', hlq)
        self.assertIn('{":SIM_SOS_CONTACT_SET:"', hlq)

        legacy = test_dir / "srv_quick_cmd_handler.c"
        if legacy.exists():
            legacy_source = legacy.read_text(encoding="utf-8")
            self.assertNotIn('{":SIM_CONNECTION_SET:"', legacy_source)
            self.assertNotIn('{":SIM_SOS_CONTACT_SET:"', legacy_source)

    def test_rejects_invalid_inputs(self) -> None:
        for bad in (
            "",
            "   ",
            ":",
            "::",
            ":GUI_TREE:1\r",
            ":GUI_TREE:1\n",
            "srv_quick_cmd send TOP5STEP:GUI_TREE:1;;",
            ':GUI_TREE:1"',
            'srv_quick_cmd send "srv_quick_cmd send TOP5STEP:GUI_TREE:1;"',
            "GUI_TREE:1",
        ):
            with self.assertRaises(ValueError, msg=repr(bad)):
                normalize_command(bad)

    def test_rejects_wire_over_128_bytes(self) -> None:
        long_args = ",".join("x" * 20 for _ in range(7))
        wire = f"srv_quick_cmd send TOP5STEP:WEATHER_SET:{long_args};"
        with self.assertRaises(ValueError):
            normalize_command(wire)

    def test_zero_arg_commands_handled(self) -> None:
        self.assertEqual(normalize_command(":WEATHER_CLEAR"), ":WEATHER_CLEAR:")
        self.assertEqual(normalize_command(":WEATHER_CLEAR:"), ":WEATHER_CLEAR:")
        self.assertEqual(
            normalize_command("srv_quick_cmd send TOP5STEP:SCREENSHOT_PRINT;"),
            ":SCREENSHOT_PRINT:",
        )
        validate_agent_command(":WEATHER_CLEAR", self.capabilities)
        validate_agent_command(
            "srv_quick_cmd send TOP5STEP:SCREENSHOT_PRINT;", self.capabilities
        )
        validate_agent_command(":SCREENSHOT_PRINT", self.capabilities)

    def test_business_arguments_are_not_locally_judged(self) -> None:
        validate_agent_command(":ENTER_PAGE:WEATHER_HOME", self.capabilities)
        validate_agent_command(":GUI_TREE:1,2", self.capabilities)
        validate_agent_command(
            ":SLEEP_RECORD_CREATE:1,30,30,10,5,45", self.capabilities
        )
        with self.assertRaises(ValueError):
            validate_agent_command(":UNKNOWN_CMD:1", self.capabilities)

    def test_explicitly_unavailable_command_is_rejected(self) -> None:
        capabilities = dict(self.capabilities)
        capabilities["WEATHER_SET"] = CommandCapability(
            "WEATHER_SET", "weather_set_handler", False, "注册表明确标记未实现"
        )
        with self.assertRaisesRegex(ValueError, "命令 WEATHER_SET 不可用"):
            validate_agent_command(":WEATHER_SET:anything", capabilities)

    def test_collect_command_json_ordered_and_deduped(self) -> None:
        result = CommandResult(
            request="gui_tree",
            status="ok",
            raw={"type": "gui_tree_end", "request": "gui_tree"},
            lines=[
                '{"type": "gui_tree_end", "request": "gui_tree"}',
                "not a json line",
                '{"type": "gui_state"}',
                '{"type": "gui_state"}',
                'shell> {"type": "gui_ack", "status": "processed"}',
                '"just a string"',
                "broken json {",
            ],
        )
        out = collect_command_json(result)
        self.assertEqual(
            out,
            [
                {"type": "gui_tree_end", "request": "gui_tree"},
                {"type": "gui_state"},
                {"type": "gui_ack", "status": "processed"},
            ],
        )

    def test_sequence_has_no_required_last_command(self) -> None:
        with self.assertRaises(ValueError):
            validate_agent_commands([], self.capabilities)
        validate_agent_commands([":ENTER_PAGE:WEATHER_HOME,0"], self.capabilities)
        validate_agent_commands(
            [":GUI_TREE:1", ":ENTER_PAGE:WEATHER_HOME,0"], self.capabilities
        )

    def test_sequence_reports_bad_command_index(self) -> None:
        with self.assertRaisesRegex(ValueError, "第 2 条命令无效"):
            validate_agent_commands(
                [":ENTER_PAGE:WEATHER_HOME,0", ":UNKNOWN_CMD:1", ":GUI_TREE:1"],
                self.capabilities,
            )

    def test_qr_hub_simulator_command_uses_standard_protocol(self) -> None:
        capabilities = {
            **self.capabilities,
            "SIM_QR_HUB_SET": CommandCapability("SIM_QR_HUB_SET", "sim_qr_hub_set", True),
        }
        validate_agent_command(":SIM_QR_HUB_SET:9101,upi,QA-UPI-12345", capabilities)

    def test_time_set_source_supports_documented_12_and_14_digit_formats(self) -> None:
        handler = self._handler_source()
        self.assertIn("len != 12 && len != 14", handler)
        self.assertIn("2000 + (str[0] - '0') * 10", handler)
        self.assertIn('\\"reason\\":\\"invalid_time\\"', handler)

    def test_active_tree_time_set_supports_documented_12_and_14_digit_formats(self) -> None:
        handler = self._active_tree_handler_source()
        self.assertIn("len != 12 && len != 14", handler)
        self.assertIn("2000 + (str[0] - '0') * 10", handler)
        self.assertIn("uint16_t date_offset = len == 12 ? 2 : 4;", handler)
        self.assertIn("if(dal_rtc_set_time(&tm) != 0)", handler)

    def test_simulator_power_lock_reset_release_and_timeout_are_consistent(self) -> None:
        source = self._sim_device_source()

        self.assertIn("p->timeout = timeout;", source)
        self.assertNotIn("g_pwmgr_attr->timeout = timeout;", source)
        self.assertEqual(
            source.count("curr->timeout = timeout == 0 ? 1 : timeout;"),
            2,
        )
        self.assertEqual(
            source.count("curr->timecnt = timeout == 0 ? 2 : 0;"),
            2,
        )
        self.assertIn("else if(tmp->timecnt < tmp->timeout)", source)
        self.assertNotIn("else if(tmp->timecnt <= tmp->timeout)", source)

    def test_sport_record_command_supports_bounded_batch_count(self) -> None:
        handler = self._handler_source()
        self.assertIn('{":SET_SPORT_RECORD_DATA:",', handler)
        self.assertIn('sscanf(str, "%d,%d", &sport_id, &count)', handler)
        self.assertIn("count < 1 || count > 20", handler)
        self.assertIn("for (int i = 0; i < count; i++)", handler)
        self.assertIn("srv_sport_handler_record_save_limit(sport_data);", handler)

        capabilities = {
            **self.capabilities,
            "SET_SPORT_RECORD_DATA": CommandCapability(
                "SET_SPORT_RECORD_DATA", "set_sport_record_data", True
            ),
        }
        validate_agent_command(":SET_SPORT_RECORD_DATA:0", capabilities)
        validate_agent_command(":SET_SPORT_RECORD_DATA:4,20", capabilities)

    def test_extended_weather_and_ota_commands_use_shared_business_paths(self) -> None:
        handler = self._handler_source()
        self.assertIn('{":SIM_WEATHER_SET_EXT:",', handler)
        self.assertIn("srv_weather_handler_save_data(&weather);", handler)
        self.assertIn("weather.futureDays_count = (uint8_t)future_days;", handler)
        self.assertIn('{":SIM_OTA_EVENT:",', handler)
        self.assertIn('{":SIM_FACTORY_MODE_SET:",', handler)
        self.assertIn("srv_factory_handler_enter_factory_mode(enabled == 1);", handler)
        self.assertIn("SRV_SYS_OTA_PROGRESS_UPDATE", handler)
        self.assertIn("srv_msg_event_send(SRV_ID_SYS, event_id, event_data);", handler)
        self.assertLess(
            handler.index('{":SIM_WEATHER_SET_EXT:"'),
            handler.index("#endif", handler.index('{":SIM_WEATHER_SET_EXT:"')),
        )

        capabilities = {
            **self.capabilities,
            "SIM_WEATHER_SET_EXT": CommandCapability(
                "SIM_WEATHER_SET_EXT", "sim_weather_set_ext", True
            ),
            "SIM_OTA_EVENT": CommandCapability("SIM_OTA_EVENT", "sim_ota_event", True),
            "SIM_FACTORY_MODE_SET": CommandCapability(
                "SIM_FACTORY_MODE_SET", "sim_factory_mode_set", True
            ),
        }
        validate_agent_command(
            ":SIM_WEATHER_SET_EXT:9201,Delhi,113,32,24,20,28,10,3,2,12,65,50,3",
            capabilities,
        )
        validate_agent_command(":SIM_OTA_EVENT:9202,progress,50", capabilities)
        validate_agent_command(":SIM_FACTORY_MODE_SET:9203,1", capabilities)

    def test_sim_user_setting_command_is_windows_only_and_uses_shared_state(self) -> None:
        handler = self._handler_source()
        self.assertIn('{":SIM_USER_SETTING_SET:",', handler)
        self.assertIn('strcmp(setting, "dnd_enable")', handler)
        self.assertIn('strcmp(setting, "dnd_mode")', handler)
        self.assertIn('strcmp(setting, "dnd_start")', handler)
        self.assertIn('strcmp(setting, "dnd_end")', handler)
        self.assertIn('strcmp(setting, "mute")', handler)
        self.assertIn('strcmp(setting, "wrist_wake")', handler)
        self.assertIn('strcmp(setting, "aod_enable")', handler)
        self.assertIn('strcmp(setting, "gender")', handler)
        self.assertIn('strcmp(setting, "height")', handler)
        self.assertIn('strcmp(setting, "weight")', handler)
        self.assertIn('strcmp(setting, "length_unit")', handler)
        self.assertIn('strcmp(setting, "weight_unit")', handler)

        self.assertIn('strcmp(setting, "birth_year")', handler)
        self.assertIn("user_info->disturb_enable", handler)
        self.assertIn("user_info->mute_switch", handler)
        self.assertIn("user_info->wrist_screen_switch", handler)
        self.assertIn("user_info->screen_off_clock_switch", handler)
        self.assertIn("SRV_USER_SETTING_DND_UPDATE", handler)
        self.assertIn("SRV_USER_SETTING_WEIST_UPDATE", handler)
        self.assertIn("SRV_USER_SETTING_LENGTH_UNIT_FORMAT_UPDATE", handler)
        self.assertIn("SRV_USER_SETTING_WEIGHT_UNIT_FORMAT_UPDATE", handler)
        self.assertIn("srv_algo_handler_user_info_set();", handler)
        self.assertLess(
            handler.index('{":SIM_USER_SETTING_SET:"'),
            handler.index("#endif", handler.index('{":SIM_USER_SETTING_SET:"')),
        )

        capabilities = {
            **self.capabilities,
            "SIM_USER_SETTING_SET": CommandCapability(
                "SIM_USER_SETTING_SET", "sim_user_setting_set", True
            ),
        }
        validate_agent_command(
            ":SIM_USER_SETTING_SET:9301,dnd_enable,1", capabilities
        )
        validate_agent_command(":SIM_USER_SETTING_SET:9302,dnd_mode,1", capabilities)
        validate_agent_command(":SIM_USER_SETTING_SET:9303,dnd_start,480", capabilities)
        validate_agent_command(":SIM_USER_SETTING_SET:9304,dnd_end,1020", capabilities)
        validate_agent_command(":SIM_USER_SETTING_SET:9305,mute,0", capabilities)
        validate_agent_command(":SIM_USER_SETTING_SET:9306,wrist_wake,1", capabilities)
        validate_agent_command(":SIM_USER_SETTING_SET:9307,aod_enable,0", capabilities)
        validate_agent_command(":SIM_USER_SETTING_SET:9308,gender,1", capabilities)
        validate_agent_command(":SIM_USER_SETTING_SET:9309,height,165", capabilities)
        validate_agent_command(":SIM_USER_SETTING_SET:9310,birth_year,1927", capabilities)
        validate_agent_command(":SIM_USER_SETTING_SET:9311,weight,0", capabilities)
        validate_agent_command(":SIM_USER_SETTING_SET:9312,weight_unit,1", capabilities)

    def test_simulator_button_press_supports_exact_long_and_split_hold(self) -> None:
        source = (
            self._source_root() / "core" / "comm" / "dal" / "sim" / "dal_key.c"
        ).read_text(encoding="utf-8")
        self.assertIn("case 2: /* 长按（press_time 毫秒） */", source)
        self.assertIn("#include \"os_common_api.h\"", source)
        self.assertIn("k_msleep(1000);", source)
        self.assertIn("gui_send_key_msg(key_code + 3, 1000);", source)
        self.assertIn("gui_send_key_msg(key_code + 4, press_time);", source)
        self.assertIn("gui_send_key_msg(key_code + 5, press_time);", source)
        self.assertIn("case 4: /* 按下 */", source)
        self.assertIn("gui_send_key_msg(key_code, 0);", source)
        self.assertIn("case 5: /* 长按释放 */", source)
        self.assertIn("gui_send_key_msg(key_code + 5, 0);", source)

    def test_simulator_power_off_key_uses_requested_hold_duration_when_supported(self) -> None:
        source = (
            self._source_root() / "core" / "comm" / "dal" / "sim" / "dal_key.c"
        ).read_text(encoding="utf-8")
        if "sim_power_get_state" not in source:
            self.skipTest("当前模拟器没有独立电源状态模型")
        self.assertIn(
            "sim_power_try_boot_by_key(press_type == 2 ? press_time : 0);", source
        )

    def test_simulator_button_press_from_black_screen_only_wakes_display(self) -> None:
        handler = self._active_tree_handler_source()
        self.assertIn("if(!sim_dev_screen_is_on())", handler)
        self.assertIn("_quick_cmd_handler_owns_screen_wake", handler)
        self.assertIn('strcmp(cmd_str, ":BUTTON_PRESS:") == 0', handler)
        self.assertIn(
            "&& !_quick_cmd_handler_owns_screen_wake(quick_test_cmd_table[idx].cmd_str)",
            handler,
        )

    def test_passive_observation_commands_do_not_wake_screen(self) -> None:
        handler = self._handler_source()
        self.assertIn("_quick_cmd_should_wake_screen", handler)
        for command in (
            ":GUI_PING:",
            ":GUI_STATE:",
            ":GUI_TREE:",
            ":SCREENSHOT_PRINT:",
            ":SIM_WAIT:",
            ":BUSINESS_GET:",
        ):
            self.assertIn(f'"{command}"', handler)
        self.assertIn(
            "if (_quick_cmd_should_wake_screen(quick_test_cmd_table[idx].cmd_str)",
            handler,
        )

    def test_qdec_command_supports_bounded_repeat_count(self) -> None:
        handler = self._handler_source()
        self.assertIn('sscanf(str, "%u,%u%n"', handler)
        self.assertIn("repeat_count > 256U", handler)
        self.assertIn("i < repeat_count", handler)

    def test_alarm_and_call_record_state_commands_are_windows_only(self) -> None:
        handler = self._handler_source()
        self.assertIn('{":SIM_ALARM_SET:",', handler)
        self.assertIn("index >= SRV_ALARM_MAX_COUNT", handler)
        self.assertIn("alarm->start_time = hour * 60U + minute;", handler)
        self.assertIn('{":SIM_CALL_RECORD_SET:",', handler)
        self.assertIn("age_seconds > 157680000U", handler)
        self.assertIn("current_time_ms - (uint64_t)age_seconds * 1000U", handler)
        self.assertLess(
            handler.index('{":SIM_ALARM_SET:"'),
            handler.index("#endif", handler.index('{":SIM_ALARM_SET:"')),
        )
        self.assertLess(
            handler.index('{":SIM_CALL_RECORD_SET:"'),
            handler.index("#endif", handler.index('{":SIM_CALL_RECORD_SET:"')),
        )

        capabilities = {
            **self.capabilities,
            "SIM_ALARM_SET": CommandCapability(
                "SIM_ALARM_SET", "sim_alarm_set", True
            ),
            "SIM_CALL_RECORD_SET": CommandCapability(
                "SIM_CALL_RECORD_SET", "sim_call_record_set", True
            ),
        }
        validate_agent_command(":SIM_ALARM_SET:9501,0,6,30,127,1", capabilities)
        validate_agent_command(
            ":SIM_CALL_RECORD_SET:9502,Anaya,1234567890,0,600", capabilities
        )

    def test_contact_data_underscore_means_empty_display_name(self) -> None:
        handler = self._handler_source()
        self.assertIn(
            'const char *display_name = strcmp(name, "_") == 0 ? "" : name;',
            handler,
        )
        self.assertIn('number, display_name);', handler)

    def test_sim_timer_state_command_is_windows_only_and_uses_timer_business_path(self) -> None:
        handler = self._handler_source()
        source_root = self._source_root()
        timer_source = (
            source_root / "app" / "comm" / "TuoBu" / "timer" / "gui_comm_timer.c"
        ).read_text(encoding="utf-8")
        quick_cmd_source = (
            source_root
            / "app"
            / "comm"
            / "TuoBu"
            / "quick_cmd"
            / "gui_comm_quick_cmd.c"
        ).read_text(encoding="utf-8")

        self.assertIn('{":SIM_TIMER_STATE_SET:",', handler)
        self.assertIn('strcmp(state_name, "running")', handler)
        self.assertIn('strcmp(state_name, "paused")', handler)
        self.assertIn('strcmp(state_name, "finished")', handler)
        self.assertIn("SRV_QUICK_CMD_MSG_TIMER_STATE_SET", handler)
        self.assertLess(
            handler.index('{":SIM_TIMER_STATE_SET:"'),
            handler.index("#endif", handler.index('{":SIM_TIMER_STATE_SET:"')),
        )
        self.assertIn("gui_comm_timer_sim_state_set", timer_source)
        self.assertIn("srv_time_ms_create(_timer_callback", timer_source)
        self.assertIn("_timer_callback(NULL);", timer_source)
        self.assertIn("gui_comm_timer_sim_state_set(", quick_cmd_source)

        capabilities = {
            **self.capabilities,
            "SIM_TIMER_STATE_SET": CommandCapability(
                "SIM_TIMER_STATE_SET", "sim_timer_state_set", True
            ),
        }
        validate_agent_command(
            ":SIM_TIMER_STATE_SET:9401,running,3599,3599", capabilities
        )
        validate_agent_command(
            ":SIM_TIMER_STATE_SET:9402,finished,300,0", capabilities
        )

    def test_command_registry_is_loaded_from_current_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = (
                Path(temporary)
                / "core"
                / "comm"
                / "srv"
                / "test"
                / "hlq_quick_cmd_handler.c"
            )
            source.parent.mkdir(parents=True)
            source.write_text(
                """
static bool live_handler(const char *str, unsigned short len)
{
    return true;
}
static bool disabled_handler(const char *str, unsigned short len)
{
    return true;
}
static const quick_test_cmd_t quick_test_cmd_table[] = {
    {":LIVE_ONLY:", live_handler},
    {":DISABLED:", disabled_handler}, // 不可用
};
""",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"W30_SOURCE_ROOT": temporary}):
                validate_agent_command(":LIVE_ONLY:any,business,values")
                with self.assertRaisesRegex(ValueError, "当前真实源码未注册"):
                    validate_agent_command(":OLD_STATIC_COMMAND:1")
                with self.assertRaisesRegex(ValueError, "命令 DISABLED 不可用"):
                    validate_agent_command(":DISABLED:1")


if __name__ == "__main__":
    unittest.main()
