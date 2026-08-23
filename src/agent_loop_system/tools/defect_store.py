"""本地缺陷库：入库（ONES获取+附件分析+源码搜索）与读取。

入库: uv run python -m agent_loop_system.tools.defect_store --import 195096,195097
读取: load_defect(number) → dict（供 main.py --defect 使用）

defect.json 可人工编辑修正分析结果。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from agent_loop_system.tools.llm_retry import (
    LLMRetryError,
    get_llm_request_timeout,
    invoke_llm_with_retry,
)
from agent_loop_system.tools.ones import Defect, OnesClient, OnesConfig, extract_desc_image_uuids

DEFECTS_ROOT = Path(__file__).resolve().parents[3] / "defects"
DEFECTS_IMG_ROOT = Path(__file__).resolve().parents[3] / "defects_img"

_SOURCE_SUFFIXES = frozenset({".c", ".h"})
_MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024
_MAX_SOURCE_MATCHES = 12
_MAX_SOURCE_READ_CHARS = 30000  # 每个候选文件每段喂给 LLM 的内容上限
_MAX_LOCATE_ROUNDS = 3  # 第二级确认的最大轮数(大文件分段轮询)
# comm 平台分支:app/projects 下 33/33 个 Project.cmake 均 set(COMM_BASE_BRANCH "TuoBu"),
# HuaShengDa/AppleStyle 无项目引用,不参与定位
_COMM_BRANCH = "TuoBu"


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def _load_env() -> None:
    """从 .env 加载环境变量（不覆盖已存在的）。"""
    path = Path(__file__).resolve().parents[3] / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _media_kind(mime: str) -> str:
    mime = mime.casefold()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime in {"application/zip", "application/x-zip-compressed"}:
        return "archive"
    if mime.startswith("text/") or "json" in mime or "xml" in mime:
        return "log"
    return "other"


def _get_llm():
    """创建 LLM 实例，复用统一配置方式。"""
    from agent_loop_system.tools.llm_config import (
        LLM_API_KEY_SCOPE_EXPLORATION,
        create_chat_llm,
    )

    return create_chat_llm(api_key_scope=LLM_API_KEY_SCOPE_EXPLORATION)


def _llm_available() -> bool:
    from agent_loop_system.tools.llm_config import (
        LLM_API_KEY_SCOPE_EXPLORATION,
        get_llm_api_key,
    )

    api_key = get_llm_api_key(LLM_API_KEY_SCOPE_EXPLORATION)
    return bool(api_key and not api_key.startswith("暂时"))


def _llm_invoke_with_retry(llm, messages, max_retries: int = 12) -> str:
    """调用 LLM，空响应自动重试（指数退避）。返回文本或空串。"""

    def _invoke() -> str:
        resp = llm.invoke(messages)
        text = resp.content if hasattr(resp, "content") else str(resp)
        return str(text)

    try:
        return invoke_llm_with_retry(
            _invoke,
            is_valid=lambda text: bool(text.strip()),
            max_attempts=max_retries,
        )
    except LLMRetryError:
        return ""


# --------------------------------------------------------------------------- #
# 附件分析
# --------------------------------------------------------------------------- #

def _analyze_image(llm, content: bytes, mime: str, defect: Defect) -> str:
    """LLM 视觉分析图片附件。"""
    from langchain_core.messages import HumanMessage

    encoded = base64.b64encode(content).decode("ascii")
    msg = HumanMessage(content=[
        {
            "type": "text",
            "text": (
                f"你是缺陷附件分析器。分析图片中直接可见的信息。\n"
                f"缺陷标题：{defect.title}\n"
                f"缺陷描述：{defect.description}\n\n"
                "只报告直接可见的事实，不臆造根因或修复方案。输出中文摘要。"
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{encoded}"},
        },
    ])
    return _llm_invoke_with_retry(llm, [msg])


def _analyze_log(llm, content: bytes, defect: Defect) -> str:
    """LLM 文本分析日志附件。"""
    text = content.decode("utf-8", errors="replace")[:8000]
    prompt = (
        f"你是缺陷附件分析器。分析日志中的关键错误信息和异常信号。\n"
        f"缺陷标题：{defect.title}\n"
        f"缺陷描述：{defect.description}\n\n"
        "只报告日志中直接出现的事实。输出中文摘要。\n\n"
        f"日志内容：\n{text}"
    )
    return _llm_invoke_with_retry(llm, prompt)


def _extract_zip_log(content: bytes) -> str:
    """从 zip 中提取日志文本，最多 8000 字符。

    匹配：.log/.txt/.json/.xml，或文件名含 log/core/crash 的文件。
    跳过 .bin/.mp4 等二进制文件。
    """
    import io
    import zipfile

    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except Exception:
        return ""
    texts: list[str] = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.lower()
        if name.endswith((".bin", ".mp4", ".png", ".jpg", ".zip")):
            continue
        is_log = (
            name.endswith((".log", ".txt", ".json", ".xml"))
            or "log" in name
            or "core" in name
            or "crash" in name
        )
        if not is_log:
            continue
        try:
            data = zf.read(info)
            texts.append(
                f"=== {info.filename} ===\n"
                f"{data.decode('utf-8', errors='replace')}"
            )
        except Exception:
            continue
    return "\n\n".join(texts)[:8000]


def _analyze_attachments(
    attachments: list[dict],
    contents: dict[str, tuple[str, bytes]],
    defect: Defect,
) -> list[dict]:
    """分析所有附件，返回 [{name, kind, summary}]。"""
    if not _llm_available():
        return [
            {
                "name": a.get("name", ""),
                "kind": _media_kind(a.get("mime", "")),
                "summary": "[LLM未配置，未分析]",
            }
            for a in attachments
        ]
    try:
        llm = _get_llm()
    except Exception:
        return [
            {
                "name": a.get("name", ""),
                "kind": _media_kind(a.get("mime", "")),
                "summary": "[LLM初始化失败，未分析]",
            }
            for a in attachments
        ]

    results: list[dict] = []
    for att in attachments:
        uuid = att.get("uuid", "")
        name = att.get("name", "")
        mime = att.get("mime", "")
        kind = _media_kind(mime)
        downloaded = contents.get(uuid)
        if downloaded is None:
            results.append({"name": name, "kind": kind, "summary": "[下载失败，未分析]"})
            continue
        actual_mime, content = downloaded
        try:
            if kind == "image":
                summary = _analyze_image(llm, content, actual_mime, defect)
            elif kind == "log":
                summary = _analyze_log(llm, content, defect)
            elif kind == "archive":
                log_text = _extract_zip_log(content)
                if log_text:
                    summary = _analyze_log(
                        llm, log_text.encode("utf-8"), defect
                    )
                else:
                    summary = f"[archive] {name}（无法提取日志内容）"
            else:
                summary = f"[{kind}] {name}（未自动分析）"
        except Exception as exc:
            summary = f"[分析失败: {type(exc).__name__}]"
        results.append({"name": name, "kind": kind, "summary": summary})
    return results


# --------------------------------------------------------------------------- #
# 源码定位(LLM 语义理解:建议候选 → 读内容 → 确认/纠正 → 精确行号)
# --------------------------------------------------------------------------- #

def _llm_json(prompt: str) -> tuple[dict | None, str]:
    """调用 LLM 并解析 JSON,空响应自动重试(指数退避)。返回 (data, note)。

    data 为 None 时 note 是失败原因;成功时 note 为空串。
    注:该 API 实测空响应概率约 2/3,12 次重试 + 指数退避使成功率 >99%。
    """
    if not _llm_available():
        return None, "LLM未配置"
    try:
        llm = _get_llm()
    except Exception:
        return None, "LLM初始化失败"
    text = _llm_invoke_with_retry(llm, prompt, max_retries=12)
    if not text:
        return None, "LLM返回空响应(12次重试均失败)"
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None, f"LLM输出非JSON: {text[:300]!r}"
    try:
        return json.loads(text[start : end + 1]), ""
    except Exception:
        return None, f"JSON解析失败: {text[:300]!r}"


def _to_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _summaries_text(analysis: list[dict]) -> str:
    return "\n".join(
        f"- {a.get('name', '')}: {a.get('summary', '')}"
        for a in analysis if a.get("summary")
    )


def _llm_suggest_files(defect: Defect, analysis: list[dict], available_files: list[str]) -> dict:
    """第一级:从标题+附件+真实文件列表中选候选文件(1-4 个)。"""
    files_block = "\n".join(available_files[:500])
    prompt = (
        "你是嵌入式手表固件缺陷预定位助手。根据缺陷信息,从下面真实存在的源码文件列表中"
        "选出问题最可能所在的文件。\n\n"
        "依据:日志中的函数名/模块名/错误序列、图片内容、缺陷现象描述、文件名语义。\n"
        "输出 JSON: {\"files\": [\"相对路径1\", \"相对路径2\", ...]}\n"
        "- files: 必须从下面的文件列表中选,给出完整相对路径(如 app/comm/TuoBu/weather/gui_comm_weather.c)\n"
        "- 只给最可能的 1-4 个,不要凑数\n\n"
        f"缺陷标题：{defect.title}\n"
        f"缺陷描述：{defect.description}\n"
        f"附件分析：\n{_summaries_text(analysis)}\n\n"
        f"可选源码文件列表(共 {len(available_files)} 个):\n{files_block}\n\n"
        "只输出 JSON,不要解释不要多余文字。"
    )
    data, note = _llm_json(prompt)
    if data is None:
        return {"files": [], "note": note}
    files = [str(f).strip() for f in data.get("files", []) if str(f).strip()]
    return {"files": files[:_MAX_SOURCE_MATCHES]}


def _llm_confirm_locate(
    defect: Defect, analysis: list[dict], segments: list[tuple[str, str, int]]
) -> dict:
    """第二级:给定候选文件某段内容,LLM 读代码确认/纠正,输出精确位置。

    segments: [(相对路径, 段内容, 段在全文件的起始行号,1 起始)]。
    返回 {"locations": [{file, function, line_start, line_end, reason}]} 或
    {"locations": [], "note": ...}。行号按段内计数(1 起始),代码负责加偏移。
    """
    files_block = "\n".join(
        f"=== {path} ===(第 {start} 行起)\n{content}"
        for path, content, start in segments
    )
    prompt = (
        "你是嵌入式手表固件缺陷定位助手。下面是缺陷信息和候选源码文件内容(可能是文件的一部分)。"
        "请阅读代码,精确定位问题可能的位置。\n\n"
        "输出 JSON: {\"locations\": [{\"file\": \"...\", \"function\": \"...\", "
        "\"line_start\": N, \"line_end\": M, \"reason\": \"...\"}]}\n"
        "- file: 必须是下面候选文件路径之一(=== 后面的路径)\n"
        "- function: 相关函数名,读过代码后给出,没有可省略\n"
        "- line_start/line_end: 按本段内容计数(1 起始),代码会加文件偏移\n"
        "- reason: 中文简述定位依据(代码逻辑/现象)\n"
        "- 0-3 个位置;若看完内容认为都不相关,输出 {\"locations\": []}\n\n"
        f"缺陷标题：{defect.title}\n"
        f"缺陷描述：{defect.description}\n"
        f"附件分析：\n{_summaries_text(analysis)}\n\n"
        f"候选源码文件内容:\n{files_block}\n\n"
        "只输出 JSON,不要解释不要多余文字。"
    )
    data, note = _llm_json(prompt)
    if data is None:
        return {"locations": [], "note": note}
    locations = []
    for loc in data.get("locations", []):
        if not isinstance(loc, dict) or not str(loc.get("file", "")).strip():
            continue
        locations.append({
            "file": str(loc["file"]).strip(),
            "function": str(loc.get("function", "")).strip(),
            "line_start": _to_int(loc.get("line_start")),
            "line_end": _to_int(loc.get("line_end")),
            "reason": str(loc.get("reason", "")).strip(),
        })
    return {"locations": locations[:_MAX_SOURCE_MATCHES]}


def _locate_function(lines: list[str], func: str) -> int:
    """找函数定义行:name( 之后分号先于花括号 → 调用/声明,跳过;兜底任意出现。"""
    for i, line in enumerate(lines):
        idx = line.find(f"{func}(")
        if idx == -1:
            continue
        tail = line[idx + len(func) + 1 :]
        if ";" in tail.split("{")[0]:
            continue  # 调用或声明
        return i
    for i, line in enumerate(lines):
        if func in line:
            return i
    return 0


def _locate_source(defect: Defect, analysis: list[dict]) -> dict:
    """LLM 语义定位:扫描文件列表 → LLM从真实文件中选 → 读内容 → LLM确认 → 按行号截 snippet。

    代码只负责:扫描文件树、读文件内容、按行号截 snippet。选哪些文件、定位哪行由 LLM 判定。
    """
    from agent_loop_system.tools.workspace import WorkspaceConflictError, resolve_source_root

    try:
        root = resolve_source_root()
    except WorkspaceConflictError as exc:
        return {"matches": [], "error": str(exc)}

    # 1. 扫描源码文件,排除非启用平台的 comm 目录
    all_files: list[str] = []
    path_map: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in _SOURCE_SUFFIXES:
            continue
        rel = path.relative_to(root).as_posix()
        if "/app/comm/" in rel and f"/app/comm/{_COMM_BRANCH}/" not in rel:
            continue
        all_files.append(rel)
        path_map[rel] = path

    if not all_files:
        return {"matches": [], "error": "源码树为空"}

    # 2. LLM 从真实文件列表中选候选文件
    suggested = _llm_suggest_files(defect, analysis, all_files)
    names = suggested.get("files", [])
    if not names:
        return {"matches": [], "note": suggested.get("note", "LLM未给出候选文件")}

    # 3. 读候选文件完整内容
    file_texts: list[tuple[str, str]] = []
    for name in names:
        path = path_map.get(name)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_texts.append((name, text))
    if not file_texts:
        return {"matches": [], "note": "候选文件读取失败"}

    # 大文件分段:每段 ≤ _MAX_SOURCE_READ_CHARS,带全文件起始行号
    segments: list[tuple[str, str, int]] = []
    for path, full in file_texts:
        pos, start_line = 0, 1
        while pos < len(full):
            seg = full[pos : pos + _MAX_SOURCE_READ_CHARS]
            segments.append((path, seg, start_line))
            pos += len(seg)
            start_line = full.count("\n", 0, pos) + 1

    # 第二级:按段轮询确认(同一轮给所有文件的第 N 段),命中即停
    by_round: dict[int, list[tuple[str, str, int]]] = {}
    for i, seg in enumerate(segments):
        by_round.setdefault(i, []).append(seg)
    locations: list[dict] = []
    last_note = "LLM判定候选文件均不相关"
    for round_i in sorted(by_round):
        confirmed = _llm_confirm_locate(defect, analysis, by_round[round_i])
        locations = confirmed.get("locations", [])
        if locations:
            break
        last_note = confirmed.get("note", "LLM判定候选文件均不相关")
    if not locations:
        return {"matches": [], "note": last_note}

    # 行号偏移(段内行号 + 段起始行 - 1),按行号读 snippet(纯搬运)
    seg_start = {s[0]: s[2] for s in by_round[round_i]}
    full_by_path = dict(file_texts)
    matches: list[dict] = []
    for loc in locations[:_MAX_SOURCE_MATCHES]:
        path = loc.get("file", "")
        full = full_by_path.get(path)
        if full is None:
            continue
        offset = seg_start.get(path, 1) - 1
        ls = (loc.get("line_start") or 1) + offset
        le = (loc.get("line_end") or ls) + offset
        lines = full.splitlines()
        if ls < 1:
            ls = 1
        if le < ls:
            le = ls
        start, end = max(0, ls - 1), min(len(lines), le)
        matches.append({
            "path": path,
            "line_start": ls,
            "line_end": le,
            "function": loc.get("function", ""),
            "reason": loc.get("reason", ""),
            "snippet": "\n".join(lines[start:end])[:2000],
        })

    return {"candidates": locations, "matches": matches}


# --------------------------------------------------------------------------- #
# 入库 & 读取
# --------------------------------------------------------------------------- #

def _import_one(client: OnesClient, defect: Defect, *, force: bool) -> tuple[str, str]:
    """入库单个缺陷。返回 (状态, 说明)；状态 ∈ {ok, skipped, failed}。

    流程：获取缺陷 → 下载附件 → LLM 分析 → 源码搜索 → 存 defect.json。
    附件内容只驻留内存，不落盘。
    """
    number = defect.number
    if not number:
        return "failed", "缺陷无编号"
    defect_path = DEFECTS_ROOT / number / "defect.json"
    if defect_path.is_file() and not force:
        return "skipped", "已存在 defect.json"
    print(f"[import] 处理缺陷 {number}...")
    try:
        print(f"[import] 标题: {defect.title}")

        # 下载附件
        attachments = client.fetch_attachments(defect.uuid)
        print(f"[import] 发现 {len(attachments)} 个附件")
        contents: dict[str, tuple[str, bytes]] = {}
        for att in attachments:
            uuid = att.get("uuid", "")
            name = att.get("name", "")
            try:
                mime, content = client.download_attachment(uuid)
                contents[uuid] = (mime, content)
                print(f"[import]   下载: {name} ({len(content)} bytes)")
            except Exception as exc:
                print(f"[import]   下载失败 {name}: {exc}")

        # 下载描述富文本内嵌图片（与"动态"附件不同源）
        desc_imgs = extract_desc_image_uuids(defect.desc_rich)
        if desc_imgs:
            print(f"[import] 描述内嵌图片 {len(desc_imgs)} 个")
            for i, (img_uuid, img_mime) in enumerate(desc_imgs):
                try:
                    mime, content = client.download_attachment(img_uuid)
                    contents[img_uuid] = (mime, content)
                    ext = (img_mime.split("/")[-1].split(";")[0] or "png").lower()
                    attachments.append({
                        "uuid": img_uuid,
                        "name": f"desc_image_{i}.{ext}",
                        "mime": img_mime,
                    })
                    # 落盘到 defects_img 供修复阶段 Agent 结合 summary 看原图
                    img_dir = DEFECTS_IMG_ROOT / number
                    img_dir.mkdir(parents=True, exist_ok=True)
                    (img_dir / f"desc_image_{i}.{ext}").write_bytes(content)
                    print(f"[import]   下载描述图片 {i} ({len(content)} bytes)")
                except Exception as exc:
                    print(f"[import]   描述图片 {i} 下载失败: {exc}")

        # 分析附件
        print("[import] 分析附件...")
        analysis = _analyze_attachments(attachments, contents, defect)

        # LLM 语义定位源码
        print("[import] LLM 定位相关源码...")
        source = _locate_source(defect, analysis)
        print(f"[import]   定位到 {len(source.get('matches', []))} 处代码")

        # 存储（只留分析结果，不含原始文件）
        defect_dir = DEFECTS_ROOT / number
        defect_dir.mkdir(parents=True, exist_ok=True)
        defect_data = {
            "number": defect.number,
            "title": defect.title,
            "description": defect.description,
            "status": defect.status_name,
            "attachments": analysis,
            "source_analysis": source,
        }
        defect_path.write_text(
            json.dumps(defect_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[import] 已存储: {defect_path}")
        return "ok", ""
    except Exception as exc:
        return "failed", str(exc)


def import_defects(numbers: list[str], *, force: bool = False) -> int:
    """批量入库：获取缺陷 → 下载附件 → 分析 → 源码搜索 → 存储 → 删原始文件。"""
    _load_env()
    try:
        client = OnesClient(OnesConfig.from_env())
    except Exception as exc:
        print(f"[import] ONES配置失败: {exc}")
        return 1

    for number in numbers:
        try:
            defect = client.get_defect(number)
        except Exception as exc:
            print(f"[import] 获取缺陷 {number} 失败: {exc}")
            continue
        if defect is None:
            print(f"[import] 未找到缺陷 {number}")
            continue
        status, reason = _import_one(client, defect, force=force)
        if status == "failed":
            print(f"[import] 缺陷 {number} 入库失败: {reason}")

    print(f"\n[import] 完成，共处理 {len(numbers)} 个缺陷")
    return 0


def import_all(*, force: bool = False, include_completed: bool = False, limit: int = 0) -> int:
    """全量入库缺陷：默认仅开放，include_completed 含已关闭。limit>0 限制数量。"""
    _load_env()
    try:
        client = OnesClient(OnesConfig.from_env())
    except Exception as exc:
        print(f"[import-all] ONES配置失败: {exc}")
        return 1

    scope = "开放+已关闭" if include_completed else "开放"
    print(f"[import-all] 获取{scope}缺陷列表...")
    try:
        defects = client.list_defects(include_completed=include_completed)
    except Exception as exc:
        print(f"[import-all] 获取缺陷列表失败: {exc}")
        return 1
    if not defects:
        print(f"[import-all] 无{scope}缺陷")
        return 0
    if limit and limit > 0:
        defects = defects[:limit]
        print(f"[import-all] 共 {len(defects)} 个{scope}缺陷（限制 {limit}）")
    else:
        print(f"[import-all] 共 {len(defects)} 个{scope}缺陷")

    ok = skipped = failed = 0
    failed_list: list[str] = []
    for defect in defects:
        status, reason = _import_one(client, defect, force=force)
        if status == "ok":
            ok += 1
        elif status == "skipped":
            skipped += 1
        else:
            failed += 1
            failed_list.append(f"{defect.number}({reason})")
            print(f"[import-all] 缺陷 {defect.number} 失败: {reason}")

    print(f"\n[import-all] 完成: 成功 {ok}，跳过 {skipped}，失败 {failed}")
    if failed_list:
        print("[import-all] 失败列表（可 --import 重试）:")
        for item in failed_list:
            print(f"  {item}")
    return 1 if failed else 0


def load_defect(number: str) -> dict | None:
    """从本地缺陷库读取缺陷（含附件分析和源码搜索结果）。"""
    path = DEFECTS_ROOT / number / "defect.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_objective(defect: dict) -> str:
    """把缺陷数据组装为 Agent 的 objective。"""
    parts = [defect["title"], "", defect.get("description", "")]
    attachments = defect.get("attachments", [])
    if attachments:
        parts.append("\n附件分析:")
        for a in attachments:
            parts.append(f"  [{a['kind']}] {a['name']}: {a['summary']}")
    source = defect.get("source_analysis", {})
    matches = source.get("matches", [])
    if matches:
        parts.append("\n相关源码位置（完整源码由 source_files 单独提供，仅供参考）:")
        for m in matches[:5]:
            location = f"  {m['path']}#L{m['line_start']}-L{m['line_end']}"
            if m.get("reason"):
                location += f": {m['reason']}"
            parts.append(location)
    return "\n".join(parts).strip()


def build_judge_criteria(defect: dict) -> str:
    """只汇总判定所需的缺陷描述和附件事实，不混入源码定位推测。"""
    parts = [defect["title"]]
    description = str(defect.get("description", "")).strip()
    if description and description != "[image]":
        parts.extend(["", description])
    attachments = defect.get("attachments", [])
    summaries = [
        f"- {item.get('name', '')}: {item.get('summary', '')}"
        for item in attachments
        if item.get("summary")
    ]
    if summaries:
        parts.extend(["", "附件文字摘要（仅作提示；与缺陷原图冲突时以原图为准）：", *summaries])
    return "\n".join(parts).strip()


def list_defect_images(number: str) -> list[Path]:
    """返回本地缺陷原图；顺序稳定，供复现和视觉判定共同使用。"""
    directory = DEFECTS_IMG_ROOT / number
    if not directory.is_dir():
        return []
    return [
        path
        for path in sorted(directory.iterdir())
        if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    ]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="defect_store")
    parser.add_argument(
        "--import",
        dest="import_numbers",
        help="逗号分隔的 ONES 缺陷编号，批量入库",
    )
    parser.add_argument(
        "--import-all",
        action="store_true",
        help="全量入库所有开放缺陷（已有 defect.json 的跳过）",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="已有 defect.json 也重新入库",
    )
    parser.add_argument(
        "--include-completed",
        action="store_true",
        help="含已关闭缺陷（默认仅开放）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="限制拉取数量（0=不限制）",
    )
    args = parser.parse_args(argv)
    if args.import_all:
        return import_all(
            force=args.force,
            include_completed=args.include_completed,
            limit=args.limit,
        )
    if not args.import_numbers:
        parser.print_help()
        return 1
    numbers = [n.strip() for n in args.import_numbers.split(",") if n.strip()]
    return import_defects(numbers, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
