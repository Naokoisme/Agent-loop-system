# Project Context

- Project root: `D:\我的\agent测试平台\Agent-loop-system`
- Purpose: Windows 本地 Agent 自动化测试平台；本次新增 PRD 到测试用例的生成、审查与同步工作台。
- Stack: Python 3.12、stdlib ThreadingHTTPServer、LangGraph/LangChain、openpyxl、原生 HTML/CSS/JavaScript、PyInstaller onedir。
- Key commands: `.\.venv\Scripts\python.exe -m pytest`; `.\.venv\Scripts\python.exe frontend/server.py`；构建复用系统 Python 3.12 的 PyInstaller，并将 `.venv\Lib\site-packages` 设为 `PYTHONPATH`。
- Safety constraints: 遵守根目录 `AGENTS.md`；不改固件隔离工作区；不刷机；现有用户改动和数据保持不动。
- QA constraint: QA Skill 只读、任务工作区外置、固定层级、S1-S36/S37、四项 P0 复核和哈希绑定。
- Active specs: `.codex/specs/requirements.md`、`spec.md`、`plan.md`、`tasks.md`。
