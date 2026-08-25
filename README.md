# Agent-loop-system

Agent-loop-system 是面向手表固件的自动化闭环工作台：读取缺陷或测试用例，驱动 Windows
Simulator 或真实手表，采集新鲜证据，并输出可审计的执行结果。

本仓库只保存 Agent-loop 的编排代码、测试、用例映射和稳定文档。固件源码、设备绑定、密钥
以及运行证据都有独立边界，不应混入同一个 Git 基线。

## 工作流

```text
缺陷 / 测试用例
        ↓
Agent-loop Runner
        ↓
执行目标（Simulator / Hardware）
        ↓
控制通道 + 截图通道
        ↓
result.json + 新截图 + 日志
        ↓
产品判定 / 修复迭代
```

截图是产品视觉结论的权威证据。命令 ACK、GUI tree、串口日志、源码和模型输出只用于诊断，
不能单独替代截图判定。

## 目标、profile 与 case map

| 目标 profile | 执行方式 | 普通运行依赖 | case map |
| --- | --- | --- | --- |
| `620C_W6830` | Windows Simulator | `D:\Agent-loop\workspaces\firmware\620C_W6830` | `case_map/620C_simulator_case_map` |
| `6202_W5230_SIMULATOR` | Windows Simulator | `D:\Agent-loop\workspaces\firmware\6202_W5230` | `case_map/6202_simulator_case_map` |
| `6202_W5230` | 真实手表 | `profiles/6202_W5230` 版本档案 | `case_map/6202_case_map` |
| `579_O2` | 579 O2 真机 | APP Bridge / COM3 只读 | `case_map/579_case_map` |

三套映射相互隔离，不得跨目标复制命令、坐标、页面、截图或 verdict。6204 真机源码位于
`D:\Agent-loop\workspaces\firmware\6204_W5230`；在独立构建、另行授权刷机和真机最小能力验证完成前，
它不是可执行 Runner 目标。`D:\TOPSTEP\shenju_w30` 只作上游参考，不在其中开发、构建或
打补丁。

项目、平台、执行目标与用例目录均由注册表和统一用例库解析；前端不会通过 579 的冻结
Manifest 类型来禁用人员新增或 Excel 导入。

## 统一用例管理

`case_map/*` 与 579 Manifest 是可追溯的源基线。服务首次读取项目时将其幂等同步到
`project_data/case_management.sqlite3`；人员新增、Excel 导入、编辑版本、归档、恢复和审计都写入
SQLite，不直接覆盖冻结源文件。冻结来源用例在网页中的编辑动作显示为“创建新版本”，历史版本和
源 SHA 会继续保留。

- W30 新增用例可以继续进入现有探索、候选复跑和 `PROMOTED` 固化流程。
- 579 新增/导入用例默认是“未绑定”，必须完成 579 动作注册表和环境门禁后才可运行。
- Excel 导入必须先选择适用平台并预览；冲突只能显式选择“跳过”或“创建新版本”。
- 批量运行前由后端 `execution-options` 逐条返回可运行状态、中文原因、成熟度和绑定版本。

迁移和源文件完整性审计：

```powershell
uv run python tools/migrate_case_store.py --output project_data/migration-audit.json
```

该工具检查源用例数、统一库来源身份、重复同步幂等性和迁移前后 JSON SHA。`project_data/` 是本机
业务数据，不进入 Git。Agent-loop Web 服务直接由 `frontend/server.py` 启动，不需要也不会启动
`desktop_qt.py`。

普通 6202 真机探索和固化用例不会读取固件源码。它们从不可变发布目录读取
`runtime/commands.json`、`runtime/pages.json` 和兼容性元数据，并校验发布清单、文件哈希、固件
SHA256、自动化协议版本及 Agent-loop 最低版本。源码诊断、修复、构建和档案再生成仍使用独立
固件工作区，这两种模式不能混用。

工程人员先用 `uv run python scripts/generate_hardware_runtime_assets.py --output-dir <临时目录>`
从已核对的固件工作区提取小型能力目录，再把 `--runtime-commands`、`--runtime-pages` 和
`--runtime-metadata` 一并交给
`scripts/publish_profile.py`。普通测试机器只需要随发布包取得 `profiles/`、匹配固件、SuperCom
和 Windows MTP，不需要 Git、Python SDK 或固件源码。

## 快速开始

要求：Windows、Python 3.12 和 `uv`。

```powershell
Set-Location D:\Agent-loop\system
Copy-Item .env.example .env
uv sync
uv run pytest -q
uv run python frontend/server.py --host 127.0.0.1 --port 8765
```

浏览器入口默认为 <http://127.0.0.1:8765>。`.env.example` 只描述配置字段；复制后在本机填写
`.env`。API key、token、账号标识、BLE 地址、COM 口和开发机绝对路径不得提交。

## 换机自适应与真实环境检查

W30 真机项目不再要求用户为每台电脑重复填写固定 COM 口或 MTP 存储卷名称。创建或读取项目时，
平台会从执行目标取得 `runtime_profile_id`，校验并按需安装已发布的不可变运行时档案；环境检查会
枚举当前 SuperCom AgentBridge 管道，并在恰好只有一个活动手表串口时自动选择实际 COM 口。

6202 USB/MTP 链路按真实 Windows 对象发现：

```text
SuperCom 活动串口
  → ZORA USB/PnP
  → 自动识别包含 download 的 MTP 存储卷
  → 按 System.FileName 或显示名匹配本轮 agent_capture_<seq>.bmp
```

这兼容了不同电脑上的 COM 编号变化、`storage` 与 `ZORA MTP Storage Volume` 等卷标差异，以及
资源管理器隐藏 `.bmp` 扩展名的情况。程序不会伪造档案或在多个活动串口之间猜测：未安装 USB/MTP
驱动、SuperCom 未运行、连接零台或多台设备、固件未暴露 `download` 时，环境中心会保留其余检查
结果并给出具体阻断项。

2026-08-25 已在 6202 真机验证：环境检查 6/6 通过；通过 COM6 的 SuperCom 共享管道完成
`dal_usb close → SCREENSHOT_CAPTURE_FILE → dal_usb open → MTP 下载`，取得 410×502、
618,518-byte BMP，固件回执校验通过。换机仍需安装项目依赖、SuperCom AgentBridge 和正常的
Windows USB/MTP 驱动；把包含本修复的提交或发布包部署到新电脑后，无需再手工绑定固定卷标或 COM
编号。

## 验证层级

以下事实彼此独立，不能互相代替：

1. **传输完成**：命令已接收、队列已通过栅栏或文件已传输，只证明通道工作。
2. **证据完整**：每个视觉检查点都有一张本轮新截图，且 `result.json` 的证据合同完整。
3. **产品 verdict**：PASS、FAIL、CANNOT_VERIFY 或 ERROR；视觉结论只由截图支持。
4. **映射成熟度**：只有精确的 `mapping_status=PROMOTED` 才表示步骤已固化。产品 FAIL 也可在
   路径与证据完整时晋升；`mapping_status` 与 verdict 必须分开记录。
5. **基线状态**：构建成功不等于刷机成功，刷机成功也不等于产品行为通过；源码快照更不等于
   已验证基线。

case map 的唯一数据合同和动态统计见 [case_map 数据合同](case_map/README.md)。

W30/579 统一平台的项目创建、快捷切换、运行平台选择与 579 安全门禁见
[W30-579 统一平台使用说明](W30-579统一平台使用说明.md)。

## 仓库与基线边界

- `D:\Agent-loop\system` 保存编排器、Runner、前端、测试、case map 和文档。
- `D:\Agent-loop\workspaces` 只保存固件、配套工具和待人工审查的独立工作区；路径配置可相对 `system` 书写。
- 固件工作区是独立 Git 仓库，可能包含 `app`、`core/comm`、`core/gui`、`core/lvgl` 等嵌套仓库。
- `artifacts/`、`evidence/`、`history/`、`logs/` 等目录是本机运行输出，不进入源码基线。
- 可复现固件清单必须记录根仓库和嵌套仓库的提交、dirty 状态、项目配置及关键产物哈希。

本项目区分源码快照和已验证基线。源码快照只保证仓库可恢复；已验证基线还必须固定目标、固件
多仓版本、配置、资源、产物哈希和测试结果，并说明所有工作区漂移。历史预基线记录与通用验收
条件见 [基线说明与 2026-08-14 历史快照](docs/baselines/current.md)，该页面不是实时 Git 状态。

## 真机安全边界

- 6202 真机批次在每条用例启动前由 Runner 受控重启设备、恢复 GUI/USB、发送
  `TEST_SESSION:START`，并确认 `DIAL + popup=null`；不要求外部预先持有测试会话，
  普通用例结束也不发送 `STOP`。
- MTP 是 6202 默认截图链路；BLE 已通过真实完整 BMP 功能验证，但当前性能不足，不切换默认值。
- 构建、刷机和真机操作是分别授权的活动；构建许可不包含刷机许可。
- 不自动修改 Git remote，不自动 push，不在其他 Agent 活跃写入期间操作共享 branch 或 index。

## 后续优化安排

- BLE 截图性能：2026-08-18 真机单图约 172 秒，期间出现 644 次
  `lld mem alloc buf fail type 0x2`。图片完整、校验通过，关闭 USB 后也能恢复 USB；当前没有明确可落地的
  修复方案，因此接受该限制并暂不处理。只有后续明确要求缩短 BLE 截图时间，或该日志开始影响稳定性时，
  才重新调查；在此之前继续以 MTP 为默认截图方式。

## 文档入口

- [W30/579 双平台当前整改说明](README-W30-579-当前整改说明.md)：本轮全部改动、当前验证结果、真机计算器证据和待处理项总览。
- [case map 数据合同](case_map/README.md)：映射成熟度、外部探索账本与 Runner 选择规则。
- [6202 Simulator 当前链路](docs/6202-simulator-current.md)：Simulator 配置与已验证事实快照。
- [6202 真机截图概览](docs/6202-hardware-screenshot-current.md)：默认链路、已验证边界和开放阻塞。
- [6202 MTP 截图操作指南](docs/6202-mtp-screenshot-tool.md)：通过 SuperCom 共享会话取得 MTP 证据。
- [6202 Windows BLE 实验快照](docs/6202-watch-ble-current.md)：绑定实验与 BLE 截图 POC 边界。
- [6204 真机迁移现状](docs/6204-hardware-migration-current.md)：尚未启用 Runner 的 6204 源码快照。
- [基线说明与历史快照](docs/baselines/current.md)：基线术语、验收条件和非实时历史记录。
- [公开仓库快照边界](docs/repository-snapshot.md)：三类本地目录的纳入范围、排除项和工作区清单。

`codex，Hermes，trae交接文档/trae工作交接.md` 仅为历史交接材料，不是现行操作手册。
