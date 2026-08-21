# Agent-loop

Agent-loop 是一个面向智能手表 UI 自动化、证据采集和缺陷修复闭环的 Windows 框架。
仓库只保存可协作开发的框架源码；固件源码、固件包、真实测试用例、运行记录和设备证据
通过各自的内部仓库或 Profile 发布渠道独立管理。

## 仓库内容

- `src/agent_loop_system/`：Agent 编排、Runner、设备/模拟器适配、协议和证据处理。
- `frontend/`：本地 Web UI 与 HTTP API 服务。
- `scripts/`：通用发布工具，例如不可变 Profile 与固件短入口发布器。
- `sim_tools/`：不含目标知识库数据的通用开发辅助脚本。
- `tests/`：框架单元测试；真实 Profile 数据合同测试不在本仓库中。
- `case_map/README.md`：外部 case map/Profile 的接入约定，不包含真实用例。

## 本地开发

要求 Python 3.12 和 [uv](https://docs.astral.sh/uv/)。

```powershell
uv sync
Copy-Item .env.example .env
uv run python frontend/server.py --host 127.0.0.1 --port 8765
```

浏览器访问 `http://127.0.0.1:8765`。真实 API Key、Token、设备地址、串口号和本机
绝对路径只填写在本地 `.env`，不得提交。

运行测试：

```powershell
uv run pytest
```

## 外部 Profile

框架不会在 Git 仓库中携带真实 `case_map`、命令能力知识库、UP3/PRD 或执行证据。
需要执行具体项目时，从授权的内部 Profile 发布渠道取得对应资源，放入运行根目录，或通过
环境变量指向隔离的固件/模拟器工作区。

运行根目录可以通过 `AGENT_LOOP_ROOT` 显式设置。源码模式默认解析为本仓库根目录；打包后的
EXE 模式解析为 EXE 所在目录，避免把可写数据放进 PyInstaller 临时目录。

## 仓库边界

以下内容不属于本仓库：

- 公司固件源码及其 GitLab 分支；
- UP3、PRD、DCF、FOT、模拟器 EXE 和 NAS Release 包；
- 真实 `case_map`、外部探索账本、测试历史、截图和缺陷附件；
- `.env`、API Key、ONES Token、账号信息和本机缓存；
- PyInstaller、编译器或前端构建产物。
