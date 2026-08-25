from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_loop_system.tools.agent import (
    Patch,
    decide_reproduction_action,
    generate_patch,
    load_simulator_knowledge,
)
from agent_loop_system.tools.llm_retry import LLMRetryError
from agent_loop_system.reproduction import (
    ReproductionAction,
    ReproductionDecision,
    ReproductionTrace,
    StepObservation,
)
from sim_tools.extract_kb import (
    extract_command_capabilities,
    extract_commands,
    extract_windows,
)


class SimulatorKnowledgeTests(unittest.TestCase):
    def test_extract_commands_keeps_exact_zero_arg_wire_format(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hlq_quick_cmd_handler.c"
            source.write_text(
                "static const quick_test_cmd_t quick_test_cmd_table[] = {\n"
                '    {":SCREENSHOT_PRINT:", screenshot_handler}, // 截屏打印，无参数\n'
                '    {":WEATHER_SET:", weather_handler},\n'
                "};\n",
                encoding="utf-8",
            )

            result = extract_commands(source)

        self.assertIn("# wire=srv_quick_cmd send", result)
        self.assertIn(
            "SCREENSHOT_PRINT | handler=screenshot_handler | 说明=截屏打印，无参数 | 参数=无",
            result,
        )
        self.assertIn(
            "WEATHER_SET | handler=weather_handler | 说明=源码注册表未说明 | 参数=handler未找到",
            result,
        )

    def test_extract_commands_keeps_twelve_digit_time_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hlq_quick_cmd_handler.c"
            source.write_text(
                "static bool set_time(const char *str, unsigned short len) { return true; }\n"
                "static const quick_test_cmd_t quick_test_cmd_table[] = {\n"
                '    {":TIME_SET:", set_time}, // 时间设置 必须为12位长度 "如：190801120809"\n'
                "};\n",
                encoding="utf-8",
            )

            result = extract_commands(source)

        self.assertIn("参数=YYMMDDHHmmss（12位，按源码示例）", result)

    def test_extract_commands_includes_variants_actions_and_source_prerequisites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "hlq_quick_cmd_handler.c"
            source.write_text(
                """
static bool set_target(const char *str, unsigned short len)
{
    int goal_type, value;
    if (sscanf(str, "%d,%d", &goal_type, &value) != 2) return false;
    activity_data_t *p_activity = sys_info_config_get_activity();
    switch(goal_type) {
    case 0: // 时间
        p_activity->target_exercise_time = value;
        break;
    case 1: // 步数
        p_activity->target_step = value;
        break;
    }
    return true;
}
static bool msg_test(const char *str, unsigned short len)
{
    int type = atoi(str);
    switch(type) {
    case 33: // 活动时长达成
        srv_msg_event_send(SRV_ID_REMIND, SRV_REMIND_ACTIVITY_TARGET,
                           SRV_ACTIVITY_TARGET_TYPE_EXERCISE_TIME);
        break;
    }
    return true;
}
static bool unavailable(const char *str, unsigned short len)
{
    _hlq_json("unavailable", 0, "rejected", "handler_failed");
    return false;
}
static const quick_test_cmd_t quick_test_cmd_table[] = {
    {":SET_DAILY_TARGET:", set_target}, // 设置目标
    {":MSG_TEST:", msg_test}, // 消息触发
    {":UNAVAILABLE:", unavailable}, // 尚未实现
};
""",
                encoding="utf-8",
            )
            app_root = root / "app" / "comm" / "TuoBu"
            app_root.mkdir(parents=True)
            (app_root / "consumer.c").write_text(
                """
case SRV_REMIND_ACTIVITY_TARGET:
    if (msg->data == SRV_ACTIVITY_TARGET_TYPE_EXERCISE_TIME) {
        param.value_u32 = sys_info_config_get_activity()->target_exercise_time;
    }
    break;
""",
                encoding="utf-8",
            )

            result = extract_commands(source, app_root=app_root)
            capabilities = extract_command_capabilities(source)

        self.assertIn("参数=goal_type,value", result)
        self.assertIn("goal_type=0:时间", result)
        self.assertIn("goal_type=0->target_exercise_time", result)
        self.assertIn("type=33:活动时长达成", result)
        self.assertIn(
            "type=33->srv_msg_event_send(SRV_ID_REMIND,SRV_REMIND_ACTIVITY_TARGET,"
            "SRV_ACTIVITY_TARGET_TYPE_EXERCISE_TIME)",
            result,
        )
        self.assertIn(
            "type=33->target_exercise_time<=(SET_DAILY_TARGET[goal_type=0])",
            result,
        )
        self.assertIn("不可用=handler当前只返回rejected", result)
        self.assertTrue(capabilities["SET_DAILY_TARGET"].available)
        self.assertFalse(capabilities["UNAVAILABLE"].available)
        self.assertEqual(
            capabilities["UNAVAILABLE"].unavailable_reason,
            "handler当前只返回rejected",
        )

    def test_extract_commands_combines_operation_code_and_suffix_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hlq_quick_cmd_handler.c"
            source.write_text(
                """
static bool create_record(const char *str, unsigned short len)
{
    int cmd = atoi(str);
    if (cmd == 0) {
        memset(record, 0, sizeof(*record));
        _hlq_json("record", 0, "accepted", NULL);
        return true;
    }
    if (cmd != 1) {
        _hlq_json("record", 0, "rejected", "handler_failed");
        return false;
    }
    const char *param = strchr(str, ',');
    if (param != NULL) param++;
    unsigned v1, v2, v3;
    if (sscanf(param, "%u,%u,%u", &v1, &v2, &v3) >= 1) {
        deep = v1;
        light = v2;
        nap_total = v3;
    }
    return true;
}
static const quick_test_cmd_t quick_test_cmd_table[] = {
    {":RECORD_CREATE:", create_record}, // 生成记录
};
""",
                encoding="utf-8",
            )

            result = extract_commands(source)

        self.assertIn("参数=cmd,deep,light,nap_total", result)
        self.assertIn("cmd=0:清空或重置数据", result)
        self.assertIn("cmd=1:有效值（其他值rejected）", result)

    def test_extract_commands_reads_supported_string_variants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hlq_quick_cmd_handler.c"
            source.write_text(
                """
static bool sensor_set(const char *str, unsigned short len)
{
    char sensor[9] = {0};
    char profile_name[9] = {0};
    if (strcmp(sensor, "spo2") == 0) return true;
    if (strcmp(sensor, "hr") == 0) return true;
    if (strcmp(profile_name, "default") == 0) return true;
    if (strcmp(profile_name, "silent") == 0) return true;
    if (strcmp(profile_name, "fixed") == 0) return true;
    if (strncmp(event_name, "day_pass", event_name_len) == 0) return true;
    return false;
}
static const quick_test_cmd_t quick_test_cmd_table[] = {
    {":SIM_SENSOR_SET:", sensor_set}, // 传感器注入
};
""",
                encoding="utf-8",
            )

            result = extract_commands(source)

        self.assertIn("sensor=spo2:源码支持", result)
        self.assertIn("sensor=hr:源码支持", result)
        self.assertIn("profile_name=fixed:源码支持", result)
        self.assertIn("event_name=day_pass:源码支持", result)

    def test_extract_commands_exposes_persistent_activity_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "hlq_quick_cmd_handler.c"
            source.write_text(
                r'''
static bool activity_set(const char *str, unsigned short len)
{
    char args[96] = {0};
    char profile_name[8] = {0};
    unsigned int seq, steps, calories, distance;
    int consumed = 0;
    if (sscanf(args, "%u,%7[^,],%u,%u,%u%n",
               &seq, profile_name, &steps, &calories, &distance, &consumed) != 5) {
        return false;
    }
    if (strcmp(profile_name, "default") == 0) return true;
    if (strcmp(profile_name, "silent") == 0) return true;
    if (strcmp(profile_name, "fixed") == 0) {
        return dal_algo_activity_sim_profile_set(profile, snapshot);
    }
    return false;
}
static const quick_test_cmd_t quick_test_cmd_table[] = {
    {":SIM_ACTIVITY_SET:", activity_set}, // persistent activity source; units=steps/Cal/meter; fixed=until profile change or process exit; side_effect=goal popup
};
''',
                encoding="utf-8",
            )

            result = extract_commands(source)

        self.assertIn("SIM_ACTIVITY_SET | handler=activity_set", result)
        self.assertIn("seq,profile_name,steps,calories,distance", result)
        self.assertIn("profile_name=fixed", result)
        self.assertIn("units=steps/Cal/meter", result)
        self.assertIn("fixed=until profile change or process exit", result)

    def test_extract_commands_reads_event_consumer_values_and_pointer_risk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "hlq_quick_cmd_handler.c"
            source.write_text(
                """
static bool swipe(const char *str, unsigned short len)
{
    int parsed_dir_val, parsed_type_val;
    if (sscanf(str, "%d,%d", &parsed_dir_val, &parsed_type_val) != 2) return false;
    uint8_t dir = parsed_dir_val;
    srv_msg_event_send(SRV_ID_QUICK_CMD, SRV_QUICK_CMD_MSG_TP_SWIPE, dir);
    return true;
}
static bool pointer_swipe(const char *str, unsigned short len)
{
    srv_msg_event_send(SRV_ID_QUICK_CMD, SRV_QUICK_CMD_MSG_TP_SWIPE_POS,
                       (uint32_t)&payload);
    return true;
}
static const quick_test_cmd_t quick_test_cmd_table[] = {
    {":SWIPE:", swipe},
    {":POINTER_SWIPE:", pointer_swipe},
};
""",
                encoding="utf-8",
            )
            app_root = root / "app" / "comm" / "TuoBu"
            app_root.mkdir(parents=True)
            (app_root / "consumer.c").write_text(
                """
case SRV_QUICK_CMD_MSG_TP_SWIPE:
    switch ((uint8_t)msg->data) {
    case 0:
        swipe_up();
        break;
    case 1:
        swipe_down();
        break;
    }
    break;
""",
                encoding="utf-8",
            )

            result = extract_commands(source, app_root=app_root)

        self.assertIn("参数=dir,type", result)
        self.assertIn("dir=0:swipe_up;dir=1:swipe_down", result)
        self.assertIn("POINTER_SWIPE", result)
        self.assertIn("64位模拟器存在指针截断风险", result)

    def test_extract_windows_uses_registered_name_and_source_param_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            windows = root / "windows" / "TEST_WINDOWS"
            windows.mkdir(parents=True)
            (root / "Project.cmake").write_text(
                "set(WINDOWS_VERSION TEST_WINDOWS)\n", encoding="utf-8"
            )
            (windows / "gui_win_active_goal.c").write_text(
                'GUI_WIN_DEFINE(GUI_WIN_ACTIVE_GOAL, "ACTIVE_GOAL", '
                "GUI_WIN_TYPE_POPUP, handler);\n",
                encoding="utf-8",
            )
            (windows / "gui_win_calculator.c").write_text(
                'GUI_WIN_DEFINE(GUI_WIN_CALCULATOR, "CALCULATOR", '
                "GUI_WIN_TYPE_NORMAL, handler);\n",
                encoding="utf-8",
            )
            (windows / "gui_win_unknown.c").write_text(
                'GUI_WIN_DEFINE(GUI_WIN_UNKNOWN, "UNKNOWN", '
                "GUI_WIN_TYPE_NORMAL, handler);\n",
                encoding="utf-8",
            )
            quick_cmd = root / "gui_comm_quick_cmd.c"
            quick_cmd.write_text(
                """
static void _open_active_goal(uint32_t param)
{
    switch(param) {
    case 1:
        win.type = ACTIVE_GOAL_TYPE_STEPS;
        break;
    case 3:
        win.type = ACTIVE_GOAL_TYPE_TIME;
        break;
    }
}
static const gui_comm_quick_special_win_t special_win[] = {
    { "ACTIVE_GOAL", _open_active_goal }
};
""",
                encoding="utf-8",
            )

            result = extract_windows(
                project_cmake=root / "Project.cmake",
                app_windows=root / "windows",
                app_quick_cmd=quick_cmd,
            )

        self.assertIn("ACTIVE_GOAL -> ACTIVE_GOAL", result)
        self.assertIn("id=GUI_WIN_ACTIVE_GOAL", result)
        self.assertIn("命令=:ENTER_PAGE:ACTIVE_GOAL,1", result)
        self.assertIn(
            "合法值=1=ACTIVE_GOAL_TYPE_STEPS, 3=ACTIVE_GOAL_TYPE_TIME",
            result,
        )
        self.assertIn("完整示例=:ENTER_PAGE:ACTIVE_GOAL,1", result)
        self.assertIn("命令=:ENTER_PAGE:CALCULATOR,0", result)
        self.assertIn("param含义=计算器页面不使用启动用户数据", result)
        self.assertIn("合法值=0=规范占位值", result)
        self.assertIn("完整示例=:ENTER_PAGE:CALCULATOR,0", result)
        self.assertIn("语法=:ENTER_PAGE:UNKNOWN,<uint32_param>", result)
        self.assertIn("合法值=未知，禁止猜测", result)
        self.assertIn("完整示例=无", result)

    def test_extract_windows_uses_business_catalog_and_checks_source_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            windows = root / "windows" / "TEST_WINDOWS"
            windows.mkdir(parents=True)
            (root / "Project.cmake").write_text(
                "set(WINDOWS_VERSION TEST_WINDOWS)\n", encoding="utf-8"
            )
            (windows / "gui_win_shortcut.c").write_text(
                'GUI_WIN_DEFINE(GUI_WIN_SHORTCUT, "SHORTCUT", '
                "GUI_WIN_TYPE_PAGE, handler);\n",
                encoding="utf-8",
            )
            (windows / "gui_win_goal.c").write_text(
                'GUI_WIN_DEFINE(GUI_WIN_ACTIVE_GOAL, "ACTIVE_GOAL", '
                "GUI_WIN_TYPE_POPUP, handler);\n",
                encoding="utf-8",
            )
            quick_cmd = root / "gui_comm_quick_cmd.c"
            quick_cmd.write_text("static int unused;\n", encoding="utf-8")
            catalog = root / "catalog.json"
            catalog.write_text(
                json.dumps(
                    {
                        "kind": "EnterPageCapabilityCatalog",
                        "schema_version": 1,
                        "catalog_id": "test.enter_page",
                        "project": "TEST",
                        "windows_version": "TEST_WINDOWS",
                        "source_method": "test",
                        "expected_window_count": 2,
                        "expected_entry_count": 3,
                        "entries": [
                            {
                                "business_name": "控制中心",
                                "window_name": "SHORTCUT",
                                "param": 0,
                            },
                            {
                                "business_name": "活动步数目标弹窗",
                                "window_name": "ACTIVE_GOAL",
                                "param": 1,
                            },
                            {
                                "business_name": "活动卡路里目标弹窗",
                                "window_name": "ACTIVE_GOAL",
                                "param": 2,
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = extract_windows(
                project_cmake=root / "Project.cmake",
                app_windows=root / "windows",
                app_quick_cmd=quick_cmd,
                project="TEST",
                capability_catalog_path=catalog,
            )

        self.assertEqual(len(result.splitlines()), 3)
        self.assertIn(
            "控制中心 -> SHORTCUT | id=GUI_WIN_SHORTCUT | GUI_WIN_TYPE_PAGE",
            result,
        )
        self.assertIn("命令=:ENTER_PAGE:SHORTCUT,0", result)
        self.assertIn("完整示例=:ENTER_PAGE:ACTIVE_GOAL,2", result)
        self.assertIn(
            "合法值=1=活动步数目标弹窗, 2=活动卡路里目标弹窗",
            result,
        )

    def test_load_simulator_knowledge_reads_only_shared_command_and_window_kb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            kb = Path(tmp)
            (kb / "commands.txt").write_text("ENTER_PAGE | 进入页面", encoding="utf-8")
            (kb / "windows.txt").write_text(
                "ACTIVE_GOAL -> ACTIVE_GOAL | param: 3=TIME", encoding="utf-8"
            )
            (kb / "coords.txt").write_text("SHOULD_NOT_BE_INCLUDED", encoding="utf-8")

            result = load_simulator_knowledge(kb)

        self.assertIn("ENTER_PAGE | 进入页面", result)
        self.assertIn("ACTIVE_GOAL -> ACTIVE_GOAL", result)
        self.assertNotIn("SHOULD_NOT_BE_INCLUDED", result)

    def test_load_simulator_knowledge_builds_from_configured_source_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command_source = root / "core" / "comm" / "srv" / "test"
            command_source.mkdir(parents=True)
            (command_source / "hlq_quick_cmd_handler.c").write_text(
                "static bool ping(const char *str, unsigned short len) { return true; }\n"
                "static const quick_test_cmd_t quick_test_cmd_table[] = {\n"
                '    {":PING:", ping}, // 源码中的命令\n'
                "};\n",
                encoding="utf-8",
            )
            project = root / "app" / "projects" / "TEST"
            project.mkdir(parents=True)
            (project / "Project.cmake").write_text(
                "set(WINDOWS_VERSION TEST_WINDOWS)\n", encoding="utf-8"
            )
            windows = root / "app" / "windows" / "TEST_WINDOWS"
            windows.mkdir(parents=True)
            (windows / "gui_win_demo.c").write_text(
                'GUI_WIN_DEFINE(GUI_WIN_DEMO, "DEMO", GUI_WIN_TYPE_PAGE, handler);\n',
                encoding="utf-8",
            )
            quick = root / "app" / "comm" / "TuoBu" / "quick_cmd"
            quick.mkdir(parents=True)
            (quick / "gui_comm_quick_cmd.c").write_text(
                "static const gui_comm_quick_special_win_t special_win[] = {\n};\n",
                encoding="utf-8",
            )

            with mock.patch.dict(
                "os.environ",
                {"W30_SOURCE_ROOT": str(root), "W30_PROJECT": "TEST"},
            ):
                result = load_simulator_knowledge()

        self.assertIn(
            "source=" + str((command_source / "hlq_quick_cmd_handler.c").resolve()),
            result,
        )
        self.assertIn("PING | handler=ping | 说明=源码中的命令", result)
        self.assertIn("DEMO -> DEMO | id=GUI_WIN_DEMO | GUI_WIN_TYPE_PAGE", result)

    def test_generate_patch_reuses_verified_commands(self) -> None:
        captured: dict[str, str] = {}
        expected = Patch(
            root_cause_analysis="analysis",
            file_path="app/example.c",
            before="old",
            after="new",
            reason="reason",
            test_commands=[":GUI_TREE:7"],
        )

        class FakeStructuredModel:
            def invoke(self, prompt):
                captured["prompt"] = prompt
                return expected

        class FakeChatOpenAI:
            def __init__(self, **_kwargs):
                pass

            def with_structured_output(self, _schema):
                return FakeStructuredModel()

        commands = [":GUI_PING:7", ":GUI_TREE:7"]
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            mock.patch("langchain_openai.ChatOpenAI", FakeChatOpenAI),
        ):
            result = generate_patch(
                objective="bug",
                source_files=[{"path": "app/example.c", "content": "old"}],
                verified_test_commands=commands,
            )

        self.assertEqual(result, expected)
        self.assertIn(str(commands), captured["prompt"])
        self.assertIn("不得重新设计复现步骤", captured["prompt"])

    def test_decide_reproduction_action_requests_only_one_business_action(self) -> None:
        captured: dict[str, str] = {}
        expected = ReproductionDecision(
            action=ReproductionAction.EXECUTE,
            command=":ENTER_PAGE:NAP_TIPS,0",
            reason="进入目标窗口",
        )

        class FakeStructuredModel:
            def invoke(self, prompt):
                captured["prompt"] = prompt
                return expected

        class FakeChatOpenAI:
            def __init__(self, **_kwargs):
                pass

            def with_structured_output(self, _schema):
                return FakeStructuredModel()

        trace = ReproductionTrace(
            task_id="196883",
            steps=[StepObservation(step=0, screenshot_ok=False)],
        )
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            mock.patch("langchain_openai.ChatOpenAI", FakeChatOpenAI),
            mock.patch(
                "agent_loop_system.tools.agent.load_simulator_knowledge",
                return_value="ENTER_PAGE | NAP_TIPS",
            ),
        ):
            result = decide_reproduction_action(
                objective="Nap 文案错误",
                source_files=[{"path": "app/nap.c", "content": "old"}],
                trace=trace,
                defect_image_paths=[],
            )

        self.assertEqual(result, expected)
        self.assertIn("只决定下一步一个动作", captured["prompt"])
        self.assertIn("不能一次规划整套命令", captured["prompt"])
        self.assertIn("GUI_PING、GUI_TREE、GUI_STATE、SCREENSHOT_PRINT", captured["prompt"])
        self.assertIn("不得额外读取 BUSINESS_GET", captured["prompt"])
        self.assertIn("必须原样复制页面目录的完整示例", captured["prompt"])
        self.assertIn("完整示例=无", captured["prompt"])
        self.assertIn("不得猜测 param", captured["prompt"])
        self.assertIn("仅供非 Windows 真机兼容", captured["prompt"])

    def test_decide_reproduction_action_labels_api_failure(self) -> None:
        class FakeChatOpenAI:
            def __init__(self, **_kwargs):
                pass

        trace = ReproductionTrace(
            task_id="api-error",
            steps=[StepObservation(step=0, screenshot_ok=False)],
        )
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            mock.patch("langchain_openai.ChatOpenAI", FakeChatOpenAI),
            mock.patch(
                "agent_loop_system.tools.agent.load_simulator_knowledge",
                return_value="ENTER_PAGE | DIAL",
            ),
            mock.patch(
                "agent_loop_system.tools.agent._invoke_structured_with_images",
                side_effect=LLMRetryError("APIConnectionError: Connection error."),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "执行 Agent API 出错"):
                decide_reproduction_action(
                    objective="控制中心",
                    source_files=[],
                    trace=trace,
                    defect_image_paths=[],
                )

    def test_decide_reproduction_action_adds_runtime_redirect_source(self) -> None:
        captured: dict[str, str] = {}
        expected = ReproductionDecision(
            action=ReproductionAction.EXECUTE,
            command=":SIM_CONNECTION_SET:3,hfp,1",
            reason="入口源码要求 HFP 连接",
        )

        class FakeStructuredModel:
            def invoke(self, prompt):
                captured["prompt"] = prompt
                return expected

        class FakeChatOpenAI:
            def __init__(self, **_kwargs):
                pass

            def with_structured_output(self, _schema):
                return FakeStructuredModel()

        trace = ReproductionTrace(
            task_id="197052",
            steps=[
                StepObservation(
                    step=1,
                    decision=ReproductionDecision(
                        action=ReproductionAction.EXECUTE,
                        command=":ENTER_PAGE:VOICE_ASSISTANT_INTERACTION,0",
                        reason="进入语音助手",
                    ),
                    screenshot_ok=False,
                    window_name="DISCONNECT_TIP",
                )
            ],
        )
        runtime_source = [{
            "path": "app/comm/TuoBu/voice_assistant/gui_comm_voice_assistant.c",
            "content": "if (!device->hfp_connect) return GUI_WIN_DISCONNECT_TIP;",
        }]
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}),
            mock.patch("langchain_openai.ChatOpenAI", FakeChatOpenAI),
            mock.patch(
                "agent_loop_system.tools.agent.load_simulator_knowledge",
                return_value="SIM_CONNECTION_SET | channel=app/hfp/all_phone",
            ),
            mock.patch(
                "agent_loop_system.tools.agent.load_runtime_navigation_sources",
                return_value=runtime_source,
            ) as load_runtime,
        ):
            result = decide_reproduction_action(
                objective="AI 语音最近应用图标错误",
                source_files=[{"path": "app/sidebar.c", "content": "sidebar"}],
                trace=trace,
                defect_image_paths=[],
            )

        self.assertEqual(result, expected)
        load_runtime.assert_called_once_with(
            "VOICE_ASSISTANT_INTERACTION",
            "DISCONNECT_TIP",
            existing_paths=["app/sidebar.c"],
        )
        self.assertIn("运行时页面入口源码", captured["prompt"])
        self.assertIn("hfp_connect", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
