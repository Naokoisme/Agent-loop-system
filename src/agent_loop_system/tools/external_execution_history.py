"""Strict reader for target-local external case exploration ledgers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path


EXTERNAL_EXECUTION_HISTORY_FIELDS = frozenset({
    "case_id",
    "sheet",
    "target",
    "last_verified",
    "evidence_root",
    "evidence_paths",
})
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SCALAR_FIELDS = (
    "case_id",
    "sheet",
    "target",
    "last_verified",
    "evidence_root",
)


class _DuplicateJsonField(ValueError):
    """Raised while decoding a JSON object that repeats one member name."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(field)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for field, item in pairs:
        if field in value:
            raise _DuplicateJsonField(field)
        value[field] = item
    return value


@dataclass(frozen=True, slots=True)
class ExternalExecutionRecord:
    """One validated external exploration fact, in ledger field order."""

    case_id: str
    sheet: str
    target: str
    last_verified: str
    evidence_root: str
    evidence_paths: tuple[str, ...]


class ExternalExecutionHistoryError(ValueError):
    """Stable validation failure with a machine-readable location and code."""

    code: str
    path: Path
    line_number: int | None
    case_id: str | None
    message: str

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: Path,
        line_number: int | None = None,
        case_id: str | None = None,
    ) -> None:
        self.code = code
        self.path = path
        self.line_number = line_number
        self.case_id = case_id
        self.message = message
        location = f"{path}:{line_number}" if line_number is not None else str(path)
        super().__init__(f"{location}: {message}")


def _error(
    code: str,
    message: str,
    *,
    path: Path,
    line_number: int | None = None,
    case_id: str | None = None,
) -> ExternalExecutionHistoryError:
    return ExternalExecutionHistoryError(
        code,
        message,
        path=path,
        line_number=line_number,
        case_id=case_id,
    )


def read_external_execution_history(
    path: str | Path,
    *,
    expected_target: str | None = None,
) -> list[ExternalExecutionRecord]:
    """Read and strictly validate one JSONL ledger in file order."""

    ledger_path = Path(path)
    if not ledger_path.is_file():
        raise _error(
            "external_ledger_missing",
            "缺少 external_execution_history.jsonl",
            path=ledger_path,
        )
    try:
        lines = ledger_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise _error(
            "external_ledger_read_error",
            f"外部探索账本读取失败: {exc}",
            path=ledger_path,
        ) from exc

    records: list[ExternalExecutionRecord] = []
    seen_case_ids: set[str] = set()
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line, object_pairs_hook=_unique_object)
        except _DuplicateJsonField as exc:
            raise _error(
                "external_ledger_schema_mismatch",
                f"外部探索记录包含重复字段 {exc.field!r}",
                path=ledger_path,
                line_number=line_number,
            ) from exc
        except json.JSONDecodeError as exc:
            raise _error(
                "invalid_external_ledger_json",
                f"外部探索账本不是合法 JSON: {exc.msg}",
                path=ledger_path,
                line_number=line_number,
            ) from exc
        if not isinstance(raw, dict):
            raise _error(
                "external_ledger_schema_mismatch",
                "外部探索记录必须是 JSON 对象",
                path=ledger_path,
                line_number=line_number,
            )

        raw_case_id = raw.get("case_id")
        known_case_id = (
            raw_case_id
            if isinstance(raw_case_id, str) and raw_case_id.strip()
            else None
        )
        actual_fields = set(raw)
        if actual_fields != EXTERNAL_EXECUTION_HISTORY_FIELDS:
            missing = sorted(EXTERNAL_EXECUTION_HISTORY_FIELDS - actual_fields)
            extra = sorted(actual_fields - EXTERNAL_EXECUTION_HISTORY_FIELDS)
            details = []
            if missing:
                details.append(f"缺少字段 {missing}")
            if extra:
                details.append(f"多余字段 {extra}")
            raise _error(
                "external_ledger_schema_mismatch",
                "外部探索账本字段必须精确匹配合同；" + "；".join(details),
                path=ledger_path,
                line_number=line_number,
                case_id=known_case_id,
            )

        for field in _SCALAR_FIELDS:
            value = raw[field]
            if not isinstance(value, str) or not value.strip():
                code = (
                    "external_ledger_case_id_missing"
                    if field == "case_id"
                    else "external_ledger_fact_missing"
                )
                raise _error(
                    code,
                    f"账本字段 {field} 必须是非空字符串",
                    path=ledger_path,
                    line_number=line_number,
                    case_id=known_case_id,
                )

        case_id = raw["case_id"]
        last_verified = raw["last_verified"]
        try:
            valid_date = bool(_DATE_PATTERN.fullmatch(last_verified))
            if valid_date:
                date.fromisoformat(last_verified)
        except ValueError:
            valid_date = False
        if not valid_date:
            raise _error(
                "external_ledger_date_invalid",
                "账本字段 last_verified 必须是有效的 YYYY-MM-DD 日期",
                path=ledger_path,
                line_number=line_number,
                case_id=case_id,
            )

        evidence_paths = raw["evidence_paths"]
        if (
            not isinstance(evidence_paths, list)
            or not evidence_paths
            or not all(
                isinstance(item, str) and bool(item.strip())
                for item in evidence_paths
            )
        ):
            raise _error(
                "external_ledger_evidence_missing",
                "evidence_paths 必须是包含非空字符串的非空数组",
                path=ledger_path,
                line_number=line_number,
                case_id=case_id,
            )

        if expected_target is not None and raw["target"] != expected_target:
            raise _error(
                "external_ledger_target_mismatch",
                f"target 必须是 {expected_target}",
                path=ledger_path,
                line_number=line_number,
                case_id=case_id,
            )
        if case_id in seen_case_ids:
            raise _error(
                "duplicate_external_ledger_case_id",
                "外部探索账本中的 case_id 必须唯一",
                path=ledger_path,
                line_number=line_number,
                case_id=case_id,
            )

        seen_case_ids.add(case_id)
        records.append(ExternalExecutionRecord(
            case_id=case_id,
            sheet=raw["sheet"],
            target=raw["target"],
            last_verified=last_verified,
            evidence_root=raw["evidence_root"],
            evidence_paths=tuple(evidence_paths),
        ))
    return records


__all__ = [
    "EXTERNAL_EXECUTION_HISTORY_FIELDS",
    "ExternalExecutionHistoryError",
    "ExternalExecutionRecord",
    "read_external_execution_history",
]
