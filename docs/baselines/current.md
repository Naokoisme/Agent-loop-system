# 基线说明与 2026-08-14 历史快照

> 快照时间：2026-08-14 22:12:27 +08:00
> 文档状态：`HISTORICAL_SNAPSHOT / NOT_CURRENT`
> 快照时已验证基线：**否**

**此文件不是实时状态页。** 文件名为兼容既有链接而保留；下文的 branch、HEAD、文件数量、
dirty 状态和测试数字只描述 2026-08-14 的一次预基线调查，不得用于判断当前工作树。需要当前
事实时，必须重新读取 Git 状态、固件多仓状态、配置和产物哈希，不能从本页复制。

本页保留两类内容：可复用的基线定义，以及带明确时间戳的历史调查记录。它从未声明测试已经
通过或固件可以发布。

## 1. 快照时的并行边界

在快照时，Simulator 和固件代码仍有其他 Agent 在同一工作树中写入。Git branch 和 index
对同一工作树内的所有进程共享，因此当时记录了以下冻结规则：

- 不创建或切换 branch；
- 不运行 `git add`，不修改 index；
- 不 commit、不 push、不修改 remote；
- 不 reset、clean、stash 或还原其他 Agent 的文件；
- 只修改 Agent-loop 根目录的基线文档、忽略规则和编码规则；
- 等并行写入停止后，再刷新状态、文件哈希、测试结果和候选提交预览。

这些规则是历史现场记录，不表示今天仍有相同的并行任务，也不授权任何 Git 操作。

## 2. Agent-loop 历史预基线事实

| 项目 | 2026-08-14 快照事实 |
| --- | --- |
| 工作区 | `D:\Agent-loop-system` |
| branch | `main` |
| HEAD | `79066d25d95a155cefdf8a4eab3706d8a69aac13` |
| origin | `https://github.com/Naokoisme/Agent-loop-system.git` |
| HEAD 已跟踪文件 | 仅 `README.md`，共 1 个文件 |
| 实际源码状态 | `src/`、`tests/`、`frontend/`、`case_map/`、`pyproject.toml` 等尚未进入该 HEAD |
| 原 README | 混合 UTF-8/UTF-16 且包含 NUL，Git 将其当成二进制差异 |
| 原忽略规则 | 没有 `.gitignore`，本机 `.env`、运行证据和缓存均暴露为未跟踪文件 |

快照当时的结论是：所记录的远端 HEAD 不是可恢复的 Agent-loop 源码基线。该结论只适用于表中
提交，不推断当前分支或当前 HEAD。

### 已知测试观测（尚未刷新）

以下数字来自并行修改前的既有运行记录，本轮没有在活跃写入期间重跑，因此只能作为历史观测：

| 范围 | 既有结果 | 当前效力 |
| --- | --- | --- |
| BLE / 真机聚焦测试 | 57 passed，14 subtests passed | 需要在写入停止后复跑 |
| 全量测试 | 367 passed，58 failed，87 subtests passed | 允许作为源码快照的已知失败，不构成已验证基线 |

## 3. 6202 固件历史多仓快照

在 2026-08-14 快照中，固件工作区被标记为 `UNBASELINED / DEVELOPMENT_WORKTREE`，不能作为
干净、可复现的固件基线。

| 仓库 | commit | 状态 |
| --- | --- | --- |
| `D:\Agent-loop-workspace\6202_W5230` | `b2b25798fcce71fae542e129308bacc8d6536b23` | detached，dirty |
| `app` | `ae26a324f890641a8f91d3044561d8bb8ce8f919` | detached，dirty |
| `core/comm` | `2c48de5161c9873a0bc28e3a1e91335f51480f50` | detached，dirty |
| `core/gui` | `74037773697923b885a70dec5a90a21e7f17a5ca` | detached，dirty |
| `core/lvgl` | `f43e20beaf61753df6fad9adcc89f6cb01841071` | detached，clean |
| `core/platform_driver` | `218cf6cf6a2133d1169e4272dfba0a316670e521` | detached，clean |

上述 dirty 状态在快照后可能已经变化，不得当作当前状态。除 commit 外，任何新的已验证固件
基线还必须补充：

- 根仓库和每个嵌套仓库的 dirty 文件清单；
- 实际项目与构建配置；
- Simulator 可执行文件或 UP3 等关键产物的 SHA-256；
- 资源文件及其来源哈希；
- 构建命令、工具链版本和验收结果。

## 4. 快照时的 Agent-loop 候选跟踪边界

当时的第一版源码快照只审计以下 allowlist：

```text
AGENTS.md
README.md
.gitignore
.env.example
.editorconfig
.gitattributes
pyproject.toml
uv.lock
src/
tests/
frontend/
sim_tools/
case_map/
docs/
```

`codex，Hermes，trae交接文档/` 不自动纳入；先单独检查是否包含历史状态、重复材料、私有路径或敏感信息。

以下目录和文件属于本机状态或生成物，必须保持忽略：

```text
.env
.venv/
.runtime/
.pytest_cache/
artifacts/
evidence/
history/
logs/
defects/
defects_img/
*.log
*.db*
build/
dist/
node_modules/
```

图片和 JSON 不能按扩展名全局忽略，因为 `tests/` 和 `case_map/` 可能包含必要的固定输入或期望结果。

### 历史初步只读审计

快照时并行写入尚未结束，因此下列数字只是当时的预审结果；它们现在全部视为历史数字：

| 检查项 | 预审结果 |
| --- | --- |
| allowlist 候选 | 218 个文件：1 个已跟踪、217 个未跟踪 |
| 候选总大小 | 9,805,580 bytes（约 9.35 MiB） |
| 严格 UTF-8 文本检查 | 217 个文本候选全部可解码，0 个含 NUL |
| 高置信度密钥模式 | 0 个命中 |
| 超过 5 MiB 的候选 | 0 个 |
| 嵌套 `.git` / reparse point | 0 / 0 |
| ignore 正反向探针 | `.env`、运行产物均被忽略；`.env.example`、源码和测试保持可见 |

密钥预审只匹配私钥块、OpenAI/GitHub/AWS 风格 token、Bearer 字面量、URL 内嵌凭据和高置信度 secret 字面量赋值；它不会证明任意文本绝对无敏感信息。唯一的二进制候选 `case_map/620手表全功能测试用例.xlsx` 为 925,646 bytes，未纳入文本密钥扫描，提交前需要单独确认其必要性和元数据。

当时的可移植性扫描还发现 26 个候选文件含 Windows 绝对路径字面量，其中 12 个位于运行源码
或工具中；这同样不是当前扫描结果。

## 5. 历史源码快照待办

下面的勾选状态是 2026-08-14 当时的待办记录，不是当前任务清单：

- [x] `.env` 和本机运行产物由 `.gitignore` 排除；
- [x] 提供不含真实值的 `.env.example`；
- [x] README 统一为可解码的 UTF-8，且不含 NUL；
- [x] 声明文本和二进制文件属性，但暂不强制全仓 EOL 转换；
- [x] 完成一次 allowlist、密钥模式、大文件、编码和嵌套仓库预审；
- [ ] 并行 Agent 停止写入；
- [ ] 刷新 Agent-loop 与固件多仓状态；
- [ ] 刷新并确认最终 allowlist、密钥模式、大文件和异常二进制清单；
- [ ] 运行 `git diff --check` 及约定测试；
- [ ] 生成 staged 预览并由用户确认；
- [ ] 经用户明确授权后创建本地源码快照 commit。

## 6. 可复用的已验证基线定义

源码快照不等于已验证基线。后者还需要：

- 固定且可解析的 target、firmware、watch、case-map 和 model profile；
- 根仓库与所有嵌套固件仓库的版本清单；
- 配置、资源和构建产物哈希；
- 与目标匹配的测试策略、结果及已知失败说明；
- 工作区无未说明漂移，或所有必要补丁均有独立、可恢复的 patch 记录；
- 从干净工作区完成一次可重复恢复与执行验证。

## 7. 快照时明确未执行

截至 2026-08-14 22:12:27 +08:00，该次调查没有切换 branch，没有操作 Git index，没有创建
commit，没有 push，没有修改固件代码，也没有刷机。这句话不描述之后发生的活动。
