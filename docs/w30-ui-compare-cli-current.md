# W30 UI 比对 CLI 接入现状

更新时间：2026-08-18（Asia/Shanghai）

## 当前结论

Agent-loop 已具备调用独立 W30 UI 比对工具的最小适配器：

- 适配器：`src/agent_loop_system/tools/w30_ui_compare.py`
- 独立工具：`ui_compare_cli.exe`
- 当前模式：仅离线 `pixel`
- 视觉判定所有者：`ui_compare_cli`
- 当前状态：`ADAPTER_IMPLEMENTED / UNIT_VERIFIED / REAL_PIXEL_E2E_VERIFIED`

这不是一个新的 Agent-loop 硬件目标。当前没有注册 W30 Runner、会话、截图 Provider、case map 或设备控制流程。

## 责任边界

`ui_compare_cli` 负责：

- 执行 W30 C++ 像素比对规则；
- 计算分数；
- 输出视觉 PASS/FAIL；
- 记录阈值、输入图片身份和自动判定来源。

Agent-loop 适配器只负责：

- 启动独立进程并设置超时；
- 解析版本化 JSON；
- 检查工具身份、模式和执行状态；
- 复核实测图/参考图的绝对路径、SHA-256 和字节数；
- 检查 PASS/FAIL、分数和 `pixel_result` 内部一致；
- 确认 `decision_source=automatic_w30_engine`；
- 确认 `manual_review_applied=false`；
- 原样保存工具结果和标准错误诊断。

适配器不会重新计算图片相似度、套用另一个阈值或覆盖视觉结论。

## 调用方法

可通过 `--tool` 显式指定 EXE：

```powershell
cd D:\Agent-loop\system
uv run python -m agent_loop_system.tools.w30_ui_compare compare `
  --tool D:\UI_AUTO_TEST_TOOLS-xushuo\ui_check_tools\build-cli-qt683\Release\ui_compare_cli.exe `
  --actual D:\evidence\actual.bmp `
  --reference D:\references\expected.png `
  --name HEART_001 `
  --timeout-seconds 30 `
  --output D:\evidence\w30_ui_comparison.json
```

也可配置非秘密路径变量，省略 `--tool`：

```powershell
$env:W30_UI_COMPARE_CLI_PATH = "D:\UI_AUTO_TEST_TOOLS-xushuo\ui_check_tools\build-cli-qt683\Release\ui_compare_cli.exe"
```

Python 调用方可以直接使用：

```python
from agent_loop_system.tools.w30_ui_compare import compare_w30_ui

comparison = compare_w30_ui(
    "D:/evidence/actual.bmp",
    "D:/references/expected.png",
    tool_path="D:/tools/ui_compare_cli.exe",
    case_name="HEART_001",
)

visual_passed = comparison.passed
tool_payload = comparison.tool_result
```

`comparison.passed` 只是对工具原始字段的只读访问，不是 Agent-loop 的二次判定。

## 输出与退出码

适配器 JSON 将工具原始 JSON 保存在 `tool_result` 下：

```json
{
  "schema_version": 1,
  "adapter": {
    "name": "agent_loop_w30_ui_compare",
    "comparison_profile": "W30",
    "mode": "pixel",
    "verdict_owner": "ui_compare_cli"
  },
  "execution_status": "completed",
  "tool_exit_code": 0,
  "diagnostics": [],
  "tool_result": {
    "result": {
      "status": "PASS",
      "passed": true,
      "score": 1.0
    }
  }
}
```

| 退出码 | 含义 |
|---:|---|
| `0` | 适配器和工具执行完成；视觉结果可能是 PASS 或 FAIL |
| `2` | Agent-loop 侧工具路径、图片路径或超时参数无效 |
| `3` | 工具无法启动、进程超时或工具以非零状态结束 |
| `4` | 工具 JSON 不符合约定或图片证据身份不一致 |
| `5` | Agent-loop 适配器结果文件写入失败 |

调用方必须读取 `tool_result.result.passed`。不得把进程退出码 `0` 解释为视觉 PASS。

## 已完成验证

- 新增单元测试：`tests/test_w30_ui_compare.py`。
- 单元测试结果：`6 passed, 4 subtests passed`。
- 使用真实 `Qt 6.8.3 / MSVC 2022 x64` 构建的 `ui_compare_cli.exe` 完成两次离线调用：
  - 相同图片：`PASS`、分数 `1.0`、工具/适配器退出码均为 `0`；
  - 明显不同图片：`FAIL`、分数 `0.0`、工具/适配器退出码仍均为 `0`。
- 使用真实工具传入无法解码的文件：工具退出码 `2`，适配器返回 `tool_exit_nonzero` 和退出码 `3`，且没有伪造视觉 FAIL。
- 两次输出均确认 `verdict_owner=ui_compare_cli`、`mode=pixel`、`manual_review_applied=false`。

## 蓝湖参考图当前状态

已从蓝湖项目 `传音W30-410X502` 建立首轮 5 张静态参考图和独立清单：

```text
D:\UI_AUTO_TEST_TOOLS-xushuo\photo_cmp_tools\references\w30\reference_manifest.json
```

当前页面为 `血氧-无数据`、`压力-无数据`、`天气-未获取`、`勿扰模式-关`、`常亮设置`。每张原图均为 `410 x 502` PNG；manifest 记录画板 ID、版本 ID、画板更新时间、本地文件大小和 SHA-256。

Agent-loop 调用方后续可以从 manifest 选择一个 `reference_id`，校验文件身份后把对应本地路径传给适配器的 `--reference`。manifest 不是 case map，不含实测截图或视觉结论；当前也没有据此注册 W30 Runner。

## 当前明确不做

- 不接 `ai` 或 `both`，不处理 `AiAnalyzer` 凭据；
- 不构建或启动完整 Qt GUI；
- 不连接 W30、6202 或 6204 设备；
- 不复用 6202/6204 的命令、截图、坐标、case map、会话或测试证据；
- 不将 W30 注册成 Agent-loop 可执行测试目标；
- 不在 Agent-loop 中复制 W30 像素阈值或判定算法。

## 后续进入 Runner 的门槛

只有在 W30 截图来源、参考图清单、独立会话/串口所有权以及候选 case map 分别验证后，才考虑新增隔离的 W30 regression profile。当前 CLI 适配器本身不能证明这些前置条件已经满足。
