# Agent-loop W30/579 双平台当前整改说明

> 文档快照：2026-08-25
> 代码目录：`D:\我的\agent测试平台\Agent-loop-system`
> 整改依据：`W30-579统一探索式自动化兼容方案.md`、`Agent-loop-W30-579统一平台一次性整改计划.md`、`Agent-loop统一用例管理与W30-579双平台整改方案.md`

## 1. 当前结论

本轮已把 Agent-loop 从仅面向 W30 的工作台，整改为由同一个 Web 平台管理 W30 与 579 的统一测试平台。两套平台共用项目、用例、探索、任务、证据、报告和门禁模型，但保留各自独立的设备控制与证据采集链路。

当前状态如下：

| 范围 | 状态 | 说明 |
| --- | --- | --- |
| Web 统一入口 | 已完成 | 直接启动 `frontend/server.py`，不依赖 `desktop_qt.py` |
| 项目新增与快捷切换 | 已完成 | 顶部提供“新增项目”和“当前项目”快捷选择 |
| 用例平台入口 | 已完成 | 进入用例管理后先选择 W30 或 579，再加载对应项目与用例 |
| 统一用例管理 | 已完成 | 支持新增、编辑版本、Excel 导入/导出、复制、归档、恢复和审计 |
| W30 原执行链兼容 | 已完成 | Simulator、SuperCom、MTP 和原 W30 Case Map 继续保留 |
| W30 换机自适应 | 已完成 | 自动继承/安装运行时档案、识别唯一 SuperCom 串口及真实 MTP 存储卷 |
| 579 平台适配框架 | 已完成 | O2 的 APP Bridge 链路与 Z1640 的 PC-BLE 链路作为独立目标并存 |
| 579 实机动作 | 默认关闭 | 必须完成受控 Canary 并由授权人员显式开启 |
| W30 计算器探索式真机链路 | 已验证 | 已按“表盘 → 菜单 → 滚动查找 → 点击计算器”走通 |
| W30 计算器正式 Runner | 当前受阻 | 缺少真机隔离工作区配置；现有 `CALC_001` 仍是直接进入页面的固化映射 |
| 自动化回归 | 已通过 | `808 passed, 17 skipped, 9812 subtests passed` |

## 2. 整改后的统一执行流程

```text
选择或新增项目
  ↓
进入用例管理，先选择 W30 / 579 平台
  ↓
新增、导入或勾选测试用例
  ↓
选择本次运行平台与执行目标
  ↓
后端校验 Project + Platform + Target + Binding + Environment
  ↓
共享探索式执行核心生成或读取动作计划
  ↓
平台适配器执行动作并采集新鲜证据
  ↓
Agent 根据截图、日志和设备反馈判断下一步或最终结果
  ↓
生成任务历史、证据、报告和自动化成熟度记录
```

平台不允许自动回退。例如选择 579 后，环境或动作绑定不满足时应明确阻断，不能偷偷改用 W30 命令运行。

## 3. W30 与 579 的统一点和差异

两套平台沿用同一套探索理论和 Agent 判断方式：读取用例、获取设备状态、规划下一步、执行动作、重新观察、判断是否继续、输出结果。差异只收敛在平台适配层。

| 能力 | W30 | 579 |
| --- | --- | --- |
| 动作下发 | Socket 或 SuperCom 命令链 | O2：ADB → APP Bridge → BLE；Z1640：PC-BLE legacy L1/L2 |
| 串口定位 | 可通过既有 W30 命令链驱动设备 | COM3 严格只读，只用于观察日志 |
| 截图 | 模拟器截图或 USB MTP 截图 | O2 手表截图；Z1640 当前声明不可用 |
| 辅助反馈 | 设备 ACK、串口日志、截图 | APP Bridge 回执、COM3/O1、O2 截图 |
| 结果依据 | 新鲜截图为产品判定权威证据 | O2 新鲜截图为产品判定权威证据 |
| 动作注册 | W30 Case Map 固化命令 | 受保护的 579 动作绑定注册表 |
| 实机总门禁 | 真机会话和环境检查 | 默认关闭，需 Canary 与显式授权 |

APP Bridge ACK 或命令 ACK 只证明动作已交付，不能单独作为产品 PASS。串口日志、源码和模型输出同样只能辅助诊断，最终视觉结论必须由本轮新截图支持。

## 4. 项目、平台与执行目标

项目、平台和执行目标已经拆成三个独立概念，由注册表显式关联：

| 项目 ID | 平台 | 执行目标 | Case Map / Catalog |
| --- | --- | --- | --- |
| `620C_W6830` | W30 | `w30.620c.simulator` | `case_map/620C_simulator_case_map` |
| `6202_W5230_SIMULATOR` | W30 | `w30.6202.simulator` | `case_map/6202_simulator_case_map` |
| `6202_W5230` | W30 | `w30.6202.hardware` | `case_map/6202_case_map` |
| `579_O2` | 579 | `579.o2` | `case_map/579_case_map` |
| `579_Z1640` | 579 | `579.z1640` | `case_map/579_case_map`（按 profile 隔离） |

内置项目当前各自绑定一个平台；通过“新增项目”可登记允许的平台、执行目标和默认项。执行请求必须携带明确的 `project_id`、`platform_id`、`target_id` 和用例身份。

注册表位置：

- `config/projects.v1.json`：项目、允许平台和默认目标。
- `src/agent_loop_system/platform_data/platform_profiles.v1.json`：平台能力、目标、控制通道与截图通道。
- `src/agent_loop_system/projects/registry.py`：项目注册表读取与校验。
- `src/agent_loop_system/platforms/registry.py`：平台和目标解析。

## 5. 前端整改

### 5.1 全局项目操作

- 顶部新增“当前项目”快捷切换按钮，支持按项目名称或 ID 搜索。
- 顶部新增“新增项目”按钮，可选择允许平台、执行目标和默认项。
- 切换项目后保留当前功能页面，同时清空上一项目的用例勾选，避免跨项目误执行。

### 5.2 用例管理入口

- 点击“用例管理”后默认不直接展示用例。
- 首屏提供 W30、579 两个平台入口。
- 选择 W30 后只加载 W30 项目和用例；选择 579 后只加载 579 项目和用例。
- 进入平台用例页后仍可在页面内切换平台，切换时重新加载对应项目范围。

### 5.3 用例管理能力

- W30 和 579 均开放“新建用例”和“导入 Excel”，不再因来源或成熟度置灰。
- 支持编辑并创建新版本、复制、归档、恢复、导出和跨端迁移入口。
- 冻结来源用例不直接覆盖，编辑操作显示为“创建新版本”。
- 新建与编辑表单支持适用平台、生命周期状态、前置条件、步骤、预期结果和备注。
- 用例列表展示来源、当前版本、适用平台、成熟度、最近结果和最近运行。
- `AUTO_READY`、`NEED_REVIEW`、`MANUAL_REQUIRED`、`UNSUPPORTED` 等状态已改为“自动化就绪”“待评审”“需人工执行”“暂不支持”等中文文案。

### 5.4 运行选择

- 勾选用例后，在底部执行栏选择“运行平台”和具体“执行目标”。
- 前端调用后端 `execution-options` 获取逐条可运行状态和中文阻断原因。
- 点击运行时，后端再次校验平台映射与环境，前端状态不能绕过后端门禁。

### 5.5 环境中心

- 测试目标支持 W30/579 平台视图。
- “大模型”“ONES”“系统更新”均为可进入的独立页签。
- 环境就绪状态只应来自真实检查结果，没有检查结果时显示“尚未检查”。
- 点击“立即检查”会重置按钮状态并执行一次全新检查，完成后使用本次返回结果刷新状态和提示。
- Simulator 只有真实启动、GUI 命令和截图探测通过后才显示相应能力可用；检查结果按配置签名缓存。
- W30 真机会独立收集档案、SuperCom、USB/PnP、MTP、GUI_PING 和模型服务六项结果，单项失败
  不再遮蔽其他设备状态。
- 系统设置弹窗保留大模型、ONES 和设备配置入口，敏感信息不在页面回显。

## 6. 统一用例库

新增本机 SQLite 业务库：

```text
project_data/case_management.sqlite3
```

源 Case Map 和 579 Manifest 继续作为可追溯只读基线。服务按来源指纹幂等同步到统一库，人员新增、导入、编辑、归档和恢复不会直接修改冻结源文件。

主要数据结构：

- `test_cases`：当前业务版本、来源、适用平台和生命周期状态。
- `case_revisions`：不可变的历史版本。
- `case_platform_bindings`：W30/579 各自的成熟度、绑定版本和门禁状态。
- `import_batches`、`import_batch_rows`：Excel 预览、冲突策略和提交结果。
- `case_audit_events`：来源同步及所有业务变更审计。

当前源基线迁移结果：

| 项目 | 来源用例 | 统一库来源用例 | 重复同步新增/更新 |
| --- | ---: | ---: | ---: |
| `620C_W6830` | 3164 | 3164 | 0 / 0 |
| `6202_W5230_SIMULATOR` | 3164 | 3164 | 0 / 0 |
| `6202_W5230` | 3164 | 3164 | 0 / 0 |
| `579_O2` | 59 | 59 | 0 / 0 |
| `579_Z1640` | 38 | 38 | 0 / 0 |

579 O2 冻结基线的成熟度分布保持为：37 条自动化就绪、17 条待评审、5 条需人工执行、0 条暂不支持。
579 Z1640 当前包含 38 条计算器映射；由于现有 OTA 无法从表盘自动进入菜单，均以 `BLOCKED`
保存，平台会在单条和批次启动前明确阻断，不会误执行或回退到 O2/W30 链路。

## 7. Excel 导入与版本控制

- 只接受 `.xlsx`。
- 导入前必须选择适用平台。
- 先预览，再用 `batch_id`、`preview_token` 和源文件 SHA 提交。
- 编号冲突必须显式选择“跳过”或“创建新版本”，不静默覆盖。
- 默认采用原子提交；重复编号或提交前数据变化会整体阻断。
- 导出兼容原有 9 列，同时补充适用平台、状态、版本和来源字段。

迁移审计命令：

```powershell
uv run python tools/migrate_case_store.py --output project_data/migration-audit.json
```

## 8. 579 平台适配

579 没有使用 W30 的 RX 串口写指令方式，也没有把 `crossend_harness` 的桌面界面复制进来。接入方式是把 579 设备能力封装为 Agent-loop 的无界面适配器。当前包含 O2 的 APP Bridge 适配器和 Z1640 的 PC-BLE 适配器，两者具有独立 target、预检和运行门禁。

核心目录：

```text
src/agent_loop_system/platforms/platform_579/
  catalog.py           # 579 Catalog
  execution.py         # 执行网关
  gates.py             # 实机和用例门禁
  health.py            # 环境检查
  observation.py       # O1/O2 观察与证据
  policy.py            # 安全策略
  serial_readonly.py   # COM3 严格只读
  session.py           # 执行会话
  transport.py         # ADB/APP Bridge/BLE 传输
```

受保护数据：

```text
src/agent_loop_system/platform_data/579/
  bindings/action_bindings.v1.json
  catalog/automation_cases.json
  catalog/execution_manifest.json
  catalog/functional_cases.json
  state/restore_registry.v1.json
```

579 正式执行链：

```text
环境预检与自动化成熟度门禁
  → 状态准备
  → COM3/O1 开始只读观察
  → ADB → APP Bridge → BLE 下发动作
  → O2 进入截图观察并触发截图
  → 视觉/日志判定
  → teardown 与状态恢复
  → 统一历史、证据与报告
```

579 新增或导入用例默认是“未绑定”，只有动作候选、人工评审、注册表引用、计划 SHA 和环境检查全部通过后才可运行。Web 和 Agent 公共接口不接收也不回显原始 `cmd_id`、`key_id` 或 `data_hex`。

## 9. 共享探索式执行核心

新增 `src/agent_loop_system/exploration_core/`：

- `contracts.py`：统一动作、观察、证据和执行结果合同。
- `action_registry.py`：动作名称到平台绑定的受控解析。
- `runtime.py`：平台无关的探索式执行循环。
- `cli.py`：带门禁的命令行入口。

CLI 安装入口：

```powershell
uv run agent-exploration --help
```

该层负责“看到什么、下一步做什么、是否继续、证据是否充分”；W30 和 579 适配器只负责各自的动作交付和状态采集。

## 10. 后端主要接口

项目与平台：

```http
GET  /api/platforms
GET  /api/platforms/{platform_id}/capabilities
GET  /api/projects
POST /api/projects
GET  /api/projects/{project_id}
GET  /api/projects/{project_id}/execution-options
GET  /api/environments
POST /api/environments/{target_id}/check
```

统一用例：

```http
GET    /api/cases
GET    /api/cases/{case_id}
POST   /api/cases
POST   /api/cases/{case_id}/revisions
GET    /api/cases/{case_id}/revisions
POST   /api/cases/{case_id}/clone
DELETE /api/cases/{case_id}
POST   /api/cases/{case_id}/restore
GET    /api/cases/{case_id}/audit
```

导入与平台绑定：

```http
POST /api/cases/import/preview
POST /api/cases/import/commit
GET  /api/cases/import/{batch_id}
GET  /api/cases/import/{batch_id}/errors
GET  /api/cases/{case_id}/bindings
POST /api/cases/{case_id}/bindings/{platform_id}/candidate
POST /api/cases/{case_id}/bindings/{platform_id}/review
POST /api/cases/{case_id}/bindings/{platform_id}/promote
POST /api/cases/{case_id}/bindings/{platform_id}/rollback
POST /api/cases/execution-options
```

执行与任务：

```http
POST /api/tests/run
POST /api/tests/run-batch
GET  /api/tests/jobs
GET  /api/tests/jobs/{job_id}
POST /api/tests/jobs/{job_id}/cancel
POST /api/tests/jobs/{job_id}/resume
```

旧 `/api/cases/create`、`/api/cases/update`、`/api/excel/preview` 和 `/api/excel/confirm` 暂时保留兼容，新页面使用统一用例库接口。

## 11. 环境、依赖与打包

运行要求：Windows、Python 3.12、`uv`。

新增或明确的关键依赖包括：

- `pyserial`：串口能力。
- `Pillow`、`numpy`：截图和图像处理。
- `openpyxl`：Excel 导入导出。
- `bleak` 与 Windows WinRT 包：BLE/RFCOMM 能力。
- `pydantic`、LangGraph、LangChain OpenAI：合同、编排和模型调用。

关键环境变量见 `.env.example`。其中：

- W30 真机默认示例为 `COM7`。运行时会枚举 SuperCom AgentBridge；恰好一个活动串口时自动采用
  实际端口，因此换电脑后无需因为 COM 编号变化修改项目。零个或多个活动串口会明确阻断，避免误连。
- 579 使用 `PLATFORM_579_COM_PORT=COM3`，该端口在代码中保持只读。
- `PLATFORM_579_ENABLED=false`、`PLATFORM_579_DEVICE_ACTIONS_ENABLED=false` 默认关闭 579 实机动作。
- API Key、Token、BLE 地址、COM 口和本机绝对路径不得提交 Git。

兼容 Wheel：

```powershell
.\.venv\Scripts\python.exe tools\build_compat_wheel.py --output-dir .runtime\package-check --allow-dirty
.\.venv\Scripts\python.exe tools\verify_compat_wheel.py .runtime\package-check\agent_loop_system-0.4.0-py3-none-any.whl
```

正式发布必须在干净提交上去掉 `--allow-dirty`。校验器会检查平台注册表、579 Catalog、动作绑定、状态恢复数据和无界面网关，同时拒绝桌面 UI 文件及跨仓绝对依赖。

## 12. 启动与使用

首次准备：

```powershell
Set-Location 'D:\我的\agent测试平台\Agent-loop-system'
Copy-Item .env.example .env
uv sync
```

启动 Web 平台：

```powershell
uv run python frontend/server.py --host 127.0.0.1 --port 8765
```

浏览器访问：<http://127.0.0.1:8765>

本项目不需要启动 `desktop_qt.py`。579 原桌面 Qt/Tk 工作台不是 Agent-loop 的入口，也不应复制到本仓库。

建议操作顺序：

1. 在顶部选择已有项目或新增项目。
2. 进入“环境中心”，对目标执行真实环境检查。
3. 进入“用例管理”，先选择 W30 或 579。
4. 选择平台对应项目，新建、导入或勾选用例。
5. 在执行栏选择运行平台和执行目标。
6. 查看后端返回的可运行数量及阻断原因。
7. 提交任务后，在自动化执行和测试报告中查看结果与证据。

## 13. 当前验证结果

全量回归命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

2026-08-25 当前结果：

```text
764 passed, 16 skipped, 9702 subtests passed in 44.58s
```

覆盖范围包括前端页面与静态资源、项目/平台路由、579 动作注册表、探索 CLI、平台运行时、统一用例仓库、Excel 导入、Wheel 构建审计、双平台集成逻辑、运行时档案自动安装、SuperCom 端口自动发现、独立环境检查及 Windows MTP 卷标/文件名兼容。

同日 6202 真机验证结果：

| 检查 | 结果 |
| --- | --- |
| 真机运行时档案 | `v1.2.0-dev.3` / 固件 `v2.1.8.0`，通过 |
| SuperCom | 自动使用当前唯一活动的 COM6 共享管道，通过 |
| Windows USB/PnP | 唯一 ZORA WPD 实例在线，通过 |
| Windows MTP | `ZORA → ZORA MTP Storage Volume → download` 可浏览，通过 |
| UART/GUI | `GUI_PING` 返回匹配序号的 `processed`，通过 |
| 模型服务 | `gpt-5.6-sol` 实际连通，通过 |
| 新鲜截图 | 410×502、618,518 bytes、固件回执校验通过 |

## 14. W30 计算器真机验证

本轮在已连接的 W30 真机上验证了用户要求的探索式入口流程：

1. 回到并唤醒表盘。
2. 通过实体按键进入菜单列表。
3. 获取菜单截图并判断当前屏是否出现“计算器”。
4. 第一屏未找到后继续上滑，每次重新获取状态并判断。
5. 第 3 次上滑后在屏幕顶部找到计算器。
6. 点击运行时发现的坐标 `(200, 30)`。
7. 获取结果截图，确认已进入计算器页面。

动作回执均为 `accepted / processed`，最终探索记录 `ok: true`。关键证据：

- 表盘：`evidence/w30_calc_menu_exploration/20260824T161420/menu-swipe-00.bmp`
- 菜单首屏：`evidence/w30_calc_correct_menu_entry/20260824T161635/menu-first-screen.bmp`
- 第 3 次滚动后找到计算器：`evidence/w30_calc_correct_menu_scroll/20260824T161719/menu-swipe-03.bmp`
- 点击后计算器页面：`evidence/w30_calc_exploratory_click/20260824T162024/calculator-after-menu-click.bmp`
- 动作摘要：`evidence/w30_calc_exploratory_click/20260824T162024/summary.json`

该结果证明“逐屏截图判断并继续寻找”的真机动作链能够走通，但尚不能等同于正式 Runner 已完成整改，原因见下一节。

## 15. 当前已知阻塞与下一步

### 已解决：W30 真机档案与端口手动绑定

- W30 真机目标现在显式拥有 `runtime_profile_id=6202_W5230`；逻辑项目复用目标档案，不按项目名
  伪造能力数据。
- 项目创建和执行前会从发布包、本机档案库或兼容发行目录寻找经过清单与哈希校验的档案，并原子
  安装到托管 `.runtime/profiles`。找不到真实档案时仍然阻断，不会生成假档案。
- SuperCom 端口由当前活动 AgentBridge 管道自动发现，旧电脑保存的 COM7 不会覆盖新电脑实际的
  COM6；多设备歧义时保持失败关闭。

### P0：把计算器入口改为正式探索循环

当前 `case_map/6202_case_map/计算器.json` 中 `CALC_001` 的固化动作仍是：

```text
:ENTER_PAGE:CALCULATOR,0
```

正式探索模式应改为：表盘 → 菜单 → 截图判断 → 未找到则滚动 → 再截图判断 → 找到后点击 → 结果截图。直接进入页面的命令可保留为调试或已固化快速复跑能力，但不能替代探索式转换流程。

### 已解决：MTP 发现和文件匹配兼容

程序原先把中间存储卷写死为 `storage`，并只比较 Shell 显示名；实际设备在当前 Windows 中暴露
`ZORA MTP Storage Volume`，BMP 扩展名只存在于 `System.FileName`，因此被误报为 MTP 未就绪或
截图不存在。现在已改为：

1. 优先匹配历史标准卷名；未命中时选择唯一包含 `download` 的存储卷。
2. 截图同时按显示名和 `System.FileName` 匹配，仍严格限定本次请求序号。
3. 真实截图仍遵循 `关闭 USB → 固件生成文件 → 恢复 USB/PnP/MTP → 下载并校验 BMP`。

USB 重新枚举可能影响前台页面的产品行为仍由 Runner 的状态重置和动作后页面校验负责，不能用旧图
或跳过页面检查规避。

### P1：完成 579 受控 Canary

579 工程链和全 fake 离线链已完成，但实机业务动作仍默认关闭。后续需要在受控设备上逐项验证 ADB、APP Bridge、BLE、COM3/O1、O2、状态恢复和证据合同，通过后再由授权人员开启总门禁。

### 已解决：环境中心状态以深度检查为准

环境列表只读取带配置签名的缓存，不因注册表存在而主动宣称就绪；“立即检查”才触发真实探测。
W30 真机六项检查会完整返回，Simulator 真实启动失败时命令和截图明确显示未检查或失败。579 继续
按 APP Bridge、BLE、COM3/O1、O2 与实机动作门禁返回平台专属状态。

## 16. 当前代码改动清单

本轮工作区主要变更如下：

| 位置 | 改动 |
| --- | --- |
| `.env.example` | 补充 W30 真机、6202 模拟器和 579 O2 配置 |
| `src/agent_loop_system/tools/llm_config.py` | 移除源码内置 API Key，敏感凭据仅从环境变量读取 |
| `pyproject.toml`、`uv.lock` | 补齐串口、图像、Excel、BLE/WinRT 依赖和 CLI 入口 |
| `frontend/index.html` | 新增全局项目切换、新增项目和设置入口 |
| `frontend/app.js` | 双平台入口、用例管理、项目切换、运行路由、中文状态、环境中心和报告交互 |
| `frontend/server.py` | 项目/平台 API、统一用例 API、导入、绑定、任务、环境和报告后端 |
| `frontend/styles.css` | 平台入口、项目对话框、用例管理和环境中心样式 |
| `config/projects.v1.json` | 四个内置项目及目标注册 |
| `src/agent_loop_system/projects/` | 项目注册与校验 |
| `src/agent_loop_system/platforms/` | 平台合同、注册表和 579 适配器 |
| `src/agent_loop_system/platform_data/` | 平台 profile、579 Catalog、绑定和恢复数据 |
| `src/agent_loop_system/exploration_core/` | 统一探索合同、动作注册表、运行时和 CLI |
| `src/agent_loop_system/case_management/` | SQLite 统一用例仓库和版本审计 |
| `src/agent_loop_system/reproduction.py` | 执行复现流程的双平台兼容 |
| `src/agent_loop_system/tools/agent.py` | Agent 工具侧的平台解析与门禁 |
| `src/agent_loop_system/tools/case_map.py` | 多项目 Case Map 与统一映射读取 |
| `src/agent_loop_system/tools/hardware_runtime_profile.py` | 已验证真机档案的自动查找、校验和原子安装 |
| `src/agent_loop_system/tools/hardware_preflight.py` | 六项独立真机检查、SuperCom 端口自动发现与真实 MTP 路径展示 |
| `src/agent_loop_system/tools/mtp_screenshot.py` | MTP 卷标自适应与 Windows 隐藏扩展名兼容 |
| `src/agent_loop_system/tools/simulator.py` | Simulator 会话使用显式进程环境，避免跨项目配置污染 |
| `case_map/579_case_map/` | 579 冻结来源映射 |
| `case_map/6202_case_map/` | 6202 真机映射 |
| `case_map/6202_simulator_case_map/` | 6202 模拟器映射 |
| `case_map/620C_simulator_case_map/` | 620C 模拟器映射 |
| `tools/` | 579 资产导入、用例迁移、Wheel 构建和审计工具 |
| `tests/` | 双平台、统一用例、Excel、CLI、注册表、前端和打包回归 |

## 17. 相关文档

- [项目主 README](README.md)
- [W30/579 统一平台使用说明](W30-579统一平台使用说明.md)
- [统一用例管理与双平台整改实施报告](../Agent-loop统一用例管理与W30-579双平台整改实施报告.md)
- [W30/579 统一探索式自动化兼容方案](../W30-579统一探索式自动化兼容方案.md)
- [Agent-loop 双平台一次性整改计划](../Agent-loop-W30-579统一平台一次性整改计划.md)

本文件是当前工作区的整改总览；架构原则、使用方式、验证结果和待办状态发生变化时，应同步更新本文件，避免方案、代码和实际设备状态不一致。
