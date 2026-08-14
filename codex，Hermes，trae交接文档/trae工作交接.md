# Trae Agent 工作交接

> 交接对象：下一任接手 W30 Agent 自闭环系统（后端 LangGraph 闭环）的 Agent。
> 前端由 codex 独立负责，本交接不涉及前端实现，但第 5 节给出前后端契约冲突需协同处理。
> 唯一信息来源假设：以下内容为新 Agent 的全部上下文。

---

## 1. 最终任务目标

构建面向 **W30 智能手表嵌入式固件** 的 Agent 自闭环修复系统（基于 LangGraph 编排），实现：
**Agent 读 ONES 缺陷 → 定位源码 → 生成 patch + 测试命令 → 构建固件 → Simulator 测试 → LLM 判 PASS/FAIL → 失败重试 → 落盘修改依据**。

### 三条铁律（贯穿全代码，不可违背）

1. **目前仅在 Simulator 上验证**，不接真机、不接 GitLab 仓库。
2. **Agent 结合测试命令输出（终端 JSON）判定 PASS/FAIL**，截图只给人看。
3. **无需人工验证**，但**修改依据必须保留**（patch diff + reason + 截图对比）。

### 预期成果

- 输入：ONES 缺陷编号（如 `--defect 196482`）
- 输出：自动修复并验证 → `verdict: PASS/FAIL` + `history/{defect_id}/{timestamp}.json` 完整证据链

---

## 2. 当前进展

### P1–P5 闭环已完成（可运行）

- **P1 构建工具**：`tools/build.py` 调真实构建链路（`D:\TOPSTEP\shenju_w30`）
- **P2 测试工具**：`tools/simulator.py`（SimulatorSession 封装）、`tools/test.py`（judge_with_llm 判定）、`tools/case_map.py`（hlq_quick_cmd 协议用例执行）
- **P3 图骨架**：`graph.py` 7 节点（validate→baseline→agent→apply→build→test→record）+ 条件重试边
- **P4 Agent+Patcher**：`tools/agent.py`（LLM 生成 Patch）、`tools/patcher.py`（before 精确匹配 + 写入）
- **P5 证据归档**：`history/{defect_id}/{timestamp}.json` 含 patch/test_result/before.bmp/after.bmp

### ONES 缺陷库已集成

- `tools/ones.py`：最小 ONES 客户端（list_defects / get_defect / 附件下载）
- `tools/defect_store.py`：入库流水线 = ONES 拉取 → 附件 LLM 分析（图片/日志/zip）→ 两阶段 LLM 源码定位 → 落 `defects/{number}/defect.json`
- CLI：`uv run python -m agent_loop_system.tools.defect_store --import 196482` 或 `--import-all --force`

### 最近一次核心改动（本次会话完成，尚未端到端验证）

将测试命令来源从「静态 case_map 用例」转为「Agent LLM 自主生成」，并修正假阳性判定：

| 文件                                                                               | 改动                                                                                                                                                       |
| ---------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [tools/agent.py](file:///d:\Agent-loop-system\src\agent_loop_system\tools\agent.py) | `Patch` 模型新增 `test_commands: list[str]`；prompt 加入 `_COMMAND_REFERENCE` 命令清单 + 第 7 条要求（先 ENTER_PAGE → 注入数据 → GUI_TREE 采集）   |
| [state.py](file:///d:\Agent-loop-system\src\agent_loop_system\state.py)             | 新增`agent_test_commands` 字段                                                                                                                           |
| [graph.py](file:///d:\Agent-loop-system\src\agent_loop_system\graph.py#L164-L189)   | `test_node` 优先用 `agent_test_commands`，无则回退 case_map；`agent_node` 回填 `agent_test_commands`                                               |
| [tools/test.py](file:///d:\Agent-loop-system\src\agent_loop_system\tools\test.py)   | `judge_with_llm` 新增 `CANNOT_VERIFY` 判定（缺触发数据时返回，避免假阳性）；`--defect` 模式用 `defect_criteria`（标题+描述）判定而非 expected_text |
| [main.py](file:///d:\Agent-loop-system\src\agent_loop_system\main.py#L81)           | `--defect` 模式下 `judge_criteria = title + description`                                                                                               |

---

## 3. 关键上下文

### 项目布局

```
d:\Agent-loop-system\
├── src\agent_loop_system\        # 后端主代码
│   ├── main.py                   # CLI 入口
│   ├── graph.py                  # LangGraph 7 节点编排
│   ├── state.py                  # LoopState (TypedDict)
│   ├── reporting.py              # progress.json / result.json 落盘
│   └── tools\
│       ├── agent.py              # LLM 生成 Patch + test_commands
│       ├── patcher.py            # 应用 patch
│       ├── build.py              # 真实构建
│       ├── simulator.py          # SimulatorSession + BMP 截图后处理
│       ├── test.py               # judge_with_llm + Verdict
│       ├── case_map.py           # hlq 用例执行
│       ├── ones.py               # ONES API 客户端
│       └── defect_store.py       # 缺陷入库 + 源码定位
├── defects\{number}\defect.json  # 本地缺陷库（可人工编辑修正分析）
├── evidence\{task_id}\           # 每缺陷仅 before.bmp + after.bmp
├── history\{defect_id}\{ts}\     # 每次修复完整证据
├── case_map\*.json               # 40 个测试用例映射表（Excel 转换）
├── .env                          # OPENAI_API_KEY / OPENAI_BASE_URL / W30_SOURCE_ROOT / ONES_AUTH_TOKEN
└── frontend\                     # 由 codex 负责，本交接不涉及
```

### 关键环境变量（.env）

- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` — LLM（第三方代理 `api.onefaka.com`）
- `W30_SOURCE_ROOT` = `D:\TOPSTEP\shenju_w30`（620C 项目源码根）
- `ONES_AUTH_TOKEN` — ONES API 鉴权
- `SIM_EXE` — 模拟器可执行路径

### 关键决定与假设

1. **判定依据 = 终端 JSON 输出**，不是截图。截图仅供人工复查。
2. **截图后处理规则**：BMP 像素行序倒序写入 = 右旋 180° + 水平翻转（等价垂直翻转），见 `simulator.py:_capture_window_bmp`。
3. **evidence 目录硬约束**：每个 `{task_id}/` 仅两个文件 `before.bmp`（基线，首轮生成后不变）+ `after.bmp`（每次修复覆盖）；构建失败时 `after.bmp` 用 `before.bmp` 占位；重试前清理 `after_attempt_*.bmp`。
4. **源码定位只看 `app/comm/TuoBu/` 分支**，过滤 HuaShengDa/AppleStyle（33 个 Project.cmake 均设 `COMM_BASE_BRANCH="TuoBu"`）。
5. **`--defect` 模式判定依据 = 缺陷标题 + 描述**；功能测试模式才用用例 `expected_text`。
6. **defect.json 可人工编辑**修正 LLM 分析错误（如源码定位、附件摘要）。
7. **不引入 SHA256 防漂移**（单线程本地场景属过度设计，已移除）。
8. **不合并 `hlq_quick_cmd_handler.c`（命令分发）与 `gui_comm_quick_cmd.c`（GUI 业务）**，会导致层倒置 + 线程安全问题。

---

## 4. 关键发现与踩坑

### LLM 相关

- **第三方代理 `api.onefaka.com` 空响应率约 67%**：必须 12 次重试 + 指数退避（1/2/4/.../60s 上限），成功率 >99%。见 `defect_store._llm_invoke_with_retry`。
- **源码定位不能让 LLM 盲猜文件名**：早期让 LLM 直接产出文件路径，命中率极低（如 `screen_timeout` 命中血氧模块）。正确做法 = 两阶段：①LLM 从真实文件列表中选候选 ②读文件内容（30K 分段轮询大文件）③LLM 确认精确行号。见 `defect_store._locate_source`。
- **LLM 会混淆函数调用与定义**：`_locate_function` 用 `name(` 后是否先 `;` 后 `{` 区分，调用/声明跳过。
- **大文件需分段**：如 `gui_comm_activity.c` 1718 行，单次喂不下，按 `_MAX_SOURCE_READ_CHARS=30000` 分段并带全文件起始行号偏移。

### 测试判定相关（重要）

- **假阳性根因**：早期测试用例只 ENTER_PAGE 不注入数据，LLM 看到终端 JSON 无异常就判 PASS。典型案例：天气 `broken clouds` 显示 unknown，但用例 `WTHR_001` 没注入天气数据，缺陷现象根本没被触发。
- **已验证无效方案**：仅靠 case_map 静态用例无法覆盖具体缺陷的触发条件 → 必须由 Agent 针对缺陷生成测试命令（含数据注入）。
- **新增 `CANNOT_VERIFY`**：终端 JSON 缺触发数据时返回，区别于 PASS/FAIL，避免误判。

### defect.json 源码分析 snippet 错误

- `source_analysis.matches[].snippet` 早期是按行号机械截取，常错位（指向 getter 而非真实归一化逻辑 `weather_normalize_big_code`）。
- 修复：snippet 改为 LLM 确认行号后按行号截取；prompt 强调"源码位置仅供参考，Agent 需通读自主判断"。

### 构建链路

- 重建 `main.exe` 后若资源版本不匹配（assets 停在旧 SVN 版本）会进 `SYSTEM_ERROR_TIP`。需 revert 旧导出 + update 到匹配版本 + 同步 fs_dir 资源 + 重新 config/构建。
- `FACTORY_RESET` 命令虽通，但执行器无重启/重连语义，~70 条"恢复出厂后进入 XX"用例暂无法 able 化。

---

## 5. 未完成事项（按优先级）

### P0 · 端到端验证新流程（最高优先）

**用缺陷 #196482 跑一次完整闭环**，全程跟踪输入输出。该缺陷已入库：

- 标题：`broken clouds 天气显示 unknown`
- 源码已定位到 `app/comm/TuoBu/weather/gui_comm_weather.c` 的 `weather_normalize_big_code`（L684-737）
- 预期 Agent 应生成：`ENTER_PAGE:WEATHER_HOME` → `WEATHER_SET:...,broken_clouds_code,...` → `GUI_TREE:1`
- 验证点：
  1. Agent 生成的 test_commands 是否含 `WEATHER_SET` 注入 broken clouds
  2. 终端 JSON 是否采集到界面 weather 字段
  3. 判定是否为 PASS（修复后 unknown 消失）或 CANNOT_VERIFY（数据未注入）
- 命令：`uv run python -m agent_loop_system --defect 196482 --task-id 196482`

### P1 · 前后端契约对齐（需与 codex 协同）

**当前矛盾**：前端 `frontend/app.js` + `server.py` 强制 `test_case` + `source_file` 三字段必填并校验在 case_map able 集合内；但新后端已改为 Agent 自主生成测试命令，case_map 仅回退。前端选的 test_case 在 `--defect` 模式下不参与判定，纯属冗余。

**建议方案（KISS）**：

- 前端：表单只留"启动 Agent 修复"按钮，移除 sheet/test-case/source-file 下拉
- `server.py`：`/api/run` 只校验 `defect`；`JobManager.start` 去掉 test_case/source_file 必填；subprocess 不传 `--test-case`/`--source-file`（或 `--source-file` 传所有 matches 作为参考）
- history 记录：`test_case`/`source_file` 改可选，渲染显示"Agent 自主生成"

**注意**：前端归 codex，本项需通过交接文档与 codex 协调，不要直接改 frontend/ 代码（避免冲突）。

### P2 · defects 全量拉取

- 已拉取 130/294 条（另一个 Agent 在执行 `defects全量拉取计划.md`）
- 老数据（70 条）因源码定位逻辑过时已删除，需用 `--import-all --force` 重新处理
- 命令：`uv run python -m agent_loop_system.tools.defect_store --import-all --force`

### P3 · simulator.py 小 bug

- `POWER_LOW_NOTIFY` 路径有 `NameError` 未修（低电弹窗链路 25 条用例受影响，需固件修）
- 见 memory「遗留」项

### P4 · 命令补全

- `SIM_CHARGE` / `FACTORY_RESET` 已补并实测通过
- 充电与低电.json：21/60 able 化；控制中心/工厂/全局部分 note 待更新

---

## 6. 建议接手路径

### 第一步：验证环境可运行（5 分钟）

```powershell
cd d:\Agent-loop-system
uv run python -c "from agent_loop_system.graph import build_graph; build_graph(); print('graph ok')"
```

确认 `.env` 存在且含 `OPENAI_API_KEY` / `W30_SOURCE_ROOT` / `ONES_AUTH_TOKEN`。

### 第二步：跑 #196482 端到端（P0）

```powershell
uv run python -m agent_loop_system --defect 196482 --task-id 196482
```

全程观察 stdout，重点看：

1. `agent_node` 输出的 `test_commands` 是否合理（是否注入了 broken clouds 天气数据）
2. `test_node` 的 `terminal_json` 是否含 weather 字段
3. 最终 `verdict` 与 `reason`

若假阳性仍存在 → 检查 Agent prompt（`agent.py` 第 7 条要求）是否被 LLM 忽略，可能需在 prompt 中强制 `WEATHER_SET` 的 code/type 参数说明。

### 第三步：修复发现的问题

- 若 Agent 不生成测试命令 → 检查 `Patch.test_commands` 是否在 LLM 结构化输出中返回
- 若 `CANNOT_VERIFY` 频繁 → Agent 未注入触发数据，加强 prompt
- 若源码定位错 → 直接编辑 `defects/196482/defect.json` 修正 `source_analysis.matches`

### 第四步：协同 codex 处理 P1

将本交接第 5 节 P1 内容同步给 codex，约定：

- 后端 `server.py` 由 trae 改（属后端）
- 前端 `app.js` 由 codex 改
- 或整体前端含 server.py 都归 codex，trae 只改 `main.py`/`graph.py` 接受可选参数

### 第五步：推进 P2 缺陷全量拉取

确认另一个 Agent 进度后，用 `--import-all --force` 重跑。

### 关键文件读取顺序（恢复状态用）

1. 本交接文档
2. `src/agent_loop_system/graph.py`（看 7 节点流程）
3. `src/agent_loop_system/tools/agent.py`（看 Patch 模型 + prompt）
4. `src/agent_loop_system/tools/test.py`（看 judge_with_llm 三态判定）
5. `src/agent_loop_system/tools/defect_store.py`（看两阶段源码定位）
6. `defects/196482/defect.json`（看真实缺陷数据结构）

### 不要做的事

- 不要重新引入 SHA256 防漂移
- 不要合并 `hlq_quick_cmd_handler.c` 和 `gui_comm_quick_cmd.c`
- 不要为"未来可能用到"的功能预留接口（YAGNI）
- 不要在 case_map 之外另造测试用例体系，Agent 生成命令已是主路径
- 不要改 `frontend/`（codex 负责）
