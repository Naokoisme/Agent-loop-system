"""统一运行时根目录与资源路径解析。

为源码模式与 PyInstaller frozen (EXE) 模式提供唯一权威的目录约定：
- 源码模式：使用项目工作区根目录（或显式 --root / AGENT_LOOP_ROOT 环境变量）
- EXE 模式：使用 EXE 所在目录（Path(sys.executable).resolve().parent）
- 严禁把可写数据（case_map, history, evidence, .runtime）解析到 sys._MEIPASS 临时目录。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def is_frozen() -> bool:
    """判断当前进程是否运行在 PyInstaller 打包的 EXE 模式中。"""
    return bool(getattr(sys, "frozen", False))


def resolve_app_root(explicit_root: Path | str | None = None) -> Path:
    """确定当前应用程序的权威根目录。

    优先级：
    1. 显式参数 explicit_root
    2. 环境变量 AGENT_LOOP_ROOT
    3. Frozen 模式：sys.executable 所在目录（便携发布包根目录）
    4. 源码模式：向上寻找包含 case_map 或 pyproject.toml 的工作区根目录
    """
    if explicit_root is not None and str(explicit_root).strip():
        root = Path(explicit_root).resolve()
    elif os.environ.get("AGENT_LOOP_ROOT", "").strip():
        root = Path(os.environ["AGENT_LOOP_ROOT"]).resolve()
    elif is_frozen():
        root = Path(sys.executable).resolve().parent
    else:
        candidate = Path(__file__).resolve().parents[2]
        if (candidate / "case_map").is_dir() or (candidate / "pyproject.toml").is_file():
            root = candidate
        else:
            cwd = Path.cwd().resolve()
            if (cwd / "case_map").is_dir():
                root = cwd
            else:
                root = candidate

    os.environ["AGENT_LOOP_ROOT"] = str(root)
    return root


def load_app_env(
    env_path: Path | None = None,
    *,
    app_root: Path | None = None,
) -> Path | None:
    """从 .env 文件加载环境变量到 os.environ（不覆盖已存在的）。"""
    if env_path is not None:
        path = Path(env_path).resolve()
    elif app_root is not None:
        path = Path(app_root).resolve() / ".env"
    else:
        root = resolve_app_root()
        path = root / ".env"
        if not path.is_file():
            cwd_env = Path.cwd() / ".env"
            if cwd_env.is_file():
                path = cwd_env

    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
    return path


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """应用程序运行期全部资源与数据路径集合。"""

    root: Path

    @property
    def frontend(self) -> Path:
        return self.root / "frontend"

    @property
    def case_map(self) -> Path:
        return self.root / "case_map"

    @property
    def templates(self) -> Path:
        return self.root / "templates"

    @property
    def history(self) -> Path:
        return self.root / "history"

    @property
    def test_history(self) -> Path:
        return self.root / "history" / "tests"

    @property
    def evidence(self) -> Path:
        return self.root / "evidence"

    @property
    def defects(self) -> Path:
        return self.root / "defects"

    @property
    def defect_images(self) -> Path:
        return self.root / "defects_img"

    @property
    def runtime_jobs(self) -> Path:
        return self.root / ".runtime" / "jobs"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @classmethod
    def from_root(cls, root: Path | str | None = None) -> RuntimePaths:
        resolved = resolve_app_root(root)
        return cls(root=resolved)
