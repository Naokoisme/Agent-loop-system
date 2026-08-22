"""Target-scoped product-copy hints for screenshot verdicts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable


PROJECT_6202 = "6202_W5230"
TRANSLATION_PROJECT_ID = "2-赛博\\客户编号2-1（传音oraimo）"
TRANSLATION_VERSION = 30
TRANSLATION_DOCUMENT = Path(
    r"D:\TOPSTEP\topstep\翻译文档\2-赛博\客户编号2-1（传音oraimo）\Translations.json"
)


@dataclass(frozen=True, slots=True)
class VisualTranslation:
    """The only translation fields allowed into the visual-verdict path."""

    key: str
    zh_cn: str
    en_us: str


def _validated_document(payload: object, *, path: Path) -> list[object]:
    if not isinstance(payload, dict):
        raise ValueError(f"6202 翻译文档不是 JSON 对象: {path}")
    project_id = (
        f"{str(payload.get('CustomerNumber') or '').strip()}\\"
        f"{str(payload.get('TranslationNumber') or '').strip()}"
    )
    if project_id != TRANSLATION_PROJECT_ID:
        raise ValueError(
            f"6202 翻译文档项目不匹配: {project_id!r} != {TRANSLATION_PROJECT_ID!r}"
        )
    if payload.get("Version") != TRANSLATION_VERSION:
        raise ValueError(
            f"6202 翻译文档必须是 Version {TRANSLATION_VERSION}: {path}"
        )
    entries = payload.get("Entries")
    if not isinstance(entries, list):
        raise ValueError(f"6202 翻译文档缺少 Entries 数组: {path}")
    return entries


@lru_cache(maxsize=4)
def _load_translation_catalog(path_text: str) -> tuple[VisualTranslation, ...]:
    path = Path(path_text)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_entries = _validated_document(payload, path=path)

    result: list[VisualTranslation] = []
    seen_keys: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        key = str(raw.get("Key") or "").strip()
        translations = raw.get("Translations")
        if not key or not isinstance(translations, dict):
            continue
        if key in seen_keys:
            raise ValueError(f"6202 翻译文档存在重复 Key: {key}")
        seen_keys.add(key)
        zh_cn = str(translations.get("zh-CN") or "").strip()
        en_us = str(translations.get("en-US") or "").strip()
        if zh_cn and en_us:
            result.append(VisualTranslation(key=key, zh_cn=zh_cn, en_us=en_us))
    if not result:
        raise ValueError(f"6202 翻译文档没有可用的 zh-CN/en-US 对照: {path}")
    return tuple(result)


def load_translation_catalog(
    path: str | Path = TRANSLATION_DOCUMENT,
) -> tuple[VisualTranslation, ...]:
    """Load the exact 6202 v30 document and retain only three approved fields."""

    return _load_translation_catalog(str(Path(path).resolve()))


def _contains_english_product_copy(text: str, product_copy: str) -> bool:
    if not any(char.isascii() and char.isalpha() for char in product_copy):
        return False
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(product_copy)}(?![A-Za-z0-9_])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def relevant_visual_translations(
    project: str,
    checkpoint_texts: Iterable[str],
    *,
    document_path: str | Path = TRANSLATION_DOCUMENT,
) -> tuple[VisualTranslation, ...]:
    """Return only v30 entries whose English copy occurs in this checkpoint."""

    if str(project or "").strip() != PROJECT_6202:
        return ()
    text = "\n".join(str(item).strip() for item in checkpoint_texts if str(item).strip())
    if not text:
        return ()
    return tuple(
        entry
        for entry in load_translation_catalog(document_path)
        if _contains_english_product_copy(text, entry.en_us)
    )


def visual_translation_context(
    project: str,
    checkpoint_texts: Iterable[str],
    *,
    document_path: str | Path = TRANSLATION_DOCUMENT,
) -> str:
    """Format only checkpoint-relevant equivalences for the vision prompt."""

    if str(project or "").strip() != PROJECT_6202:
        return ""
    try:
        matches = relevant_visual_translations(
            project,
            checkpoint_texts,
            document_path=document_path,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return (
            "6202 Version 30 翻译文档当前无法校验。若截图与预期仅显示语言不同，"
            "不得据此判定 FAIL；无法确认语义时应返回 CANNOT_VERIFY。"
        )
    if not matches:
        return ""

    rows = "\n".join(
        f"- {entry.key}: en-US={json.dumps(entry.en_us, ensure_ascii=False)}, "
        f"zh-CN={json.dumps(entry.zh_cn, ensure_ascii=False)}"
        for entry in matches
    )
    return (
        "6202 产品文案等价关系（只包含当前预期/验证点在 Version 30 中命中的条目）：\n"
        f"{rows}\n"
        "同一 Key 的 en-US 与 zh-CN 是同一产品文案，不得仅因显示语言不同判定 FAIL；"
        "仍须继续核对项目位置、顺序、状态及其他可见内容。未列出的词语不得作模糊翻译或别名推断。"
    )
