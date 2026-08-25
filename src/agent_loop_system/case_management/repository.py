from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


CASE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
WORKFLOW_STATES = {"DRAFT", "REVIEWING", "ACTIVE", "ARCHIVED"}
IMPORT_STRATEGIES = {"SKIP", "NEW_REVISION"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest().upper()


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return copy.deepcopy(default)
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return copy.deepcopy(default)


class CaseManagementRepository:
    """SQLite-backed business cases layered over immutable source catalogs.

    Source case-map/Manifest files remain untouched by Web CRUD.  They are
    synchronized as traceable baseline revisions; user edits create managed
    revisions in this store.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_info (
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS test_cases (
                    project_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    sheet TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    priority TEXT NOT NULL DEFAULT 'P1',
                    workflow_state TEXT NOT NULL DEFAULT 'ACTIVE',
                    applicable_platforms_json TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_locked INTEGER NOT NULL DEFAULT 0,
                    has_managed_override INTEGER NOT NULL DEFAULT 0,
                    source_ref_json TEXT NOT NULL DEFAULT '{}',
                    source_sha256 TEXT NOT NULL DEFAULT '',
                    source_snapshot_json TEXT NOT NULL DEFAULT '{}',
                    current_revision INTEGER NOT NULL,
                    current_content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, case_id)
                );
                CREATE INDEX IF NOT EXISTS idx_test_cases_project_sheet
                    ON test_cases(project_id, sheet, case_id);
                CREATE INDEX IF NOT EXISTS idx_test_cases_workflow
                    ON test_cases(project_id, workflow_state);
                CREATE TABLE IF NOT EXISTS case_revisions (
                    project_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    content_json TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    change_type TEXT NOT NULL,
                    change_summary TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT 'local-user',
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, case_id, revision),
                    FOREIGN KEY (project_id, case_id)
                        REFERENCES test_cases(project_id, case_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS case_platform_bindings (
                    project_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    target_id TEXT NOT NULL DEFAULT '',
                    binding_version INTEGER NOT NULL DEFAULT 1,
                    automation_maturity TEXT NOT NULL DEFAULT 'UNMAPPED',
                    runnable INTEGER NOT NULL DEFAULT 0,
                    blocker TEXT NOT NULL DEFAULT '',
                    binding_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (project_id, case_id, platform_id, target_id),
                    FOREIGN KEY (project_id, case_id)
                        REFERENCES test_cases(project_id, case_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS import_batches (
                    batch_id TEXT PRIMARY KEY,
                    preview_token TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_filename TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    conflict_strategy TEXT NOT NULL,
                    status TEXT NOT NULL,
                    total_rows INTEGER NOT NULL,
                    created_cases INTEGER NOT NULL DEFAULT 0,
                    new_revisions INTEGER NOT NULL DEFAULT 0,
                    skipped_rows INTEGER NOT NULL DEFAULT 0,
                    failed_rows INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    committed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS import_batch_rows (
                    batch_id TEXT NOT NULL,
                    row_number INTEGER NOT NULL,
                    case_id TEXT NOT NULL,
                    sheet TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    case_json TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (batch_id, row_number),
                    FOREIGN KEY (batch_id) REFERENCES import_batches(batch_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS case_audit_events (
                    event_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    case_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_sync_state (
                    project_id TEXT PRIMARY KEY,
                    source_fingerprint TEXT NOT NULL,
                    source_case_count INTEGER NOT NULL DEFAULT 0,
                    synced_at TEXT NOT NULL
                );
                """
            )
            row = connection.execute("SELECT version FROM schema_info LIMIT 1").fetchone()
            if row is None:
                connection.execute("INSERT INTO schema_info(version) VALUES (?)", (self.SCHEMA_VERSION,))
            elif int(row["version"]) != self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"CASE_STORE_SCHEMA_UNSUPPORTED: {row['version']} != {self.SCHEMA_VERSION}"
                )

    @staticmethod
    def _validate_sheet(value: Any) -> str:
        sheet = str(value or "").strip()
        if not sheet or "/" in sheet or "\\" in sheet or sheet in {".", ".."}:
            raise ValueError("CASE_SHEET_INVALID: 模块名称不合法")
        return sheet

    @staticmethod
    def _validate_case_id(value: Any) -> str:
        case_id = str(value or "").strip()
        if not CASE_ID.fullmatch(case_id) or case_id in {".", ".."}:
            raise ValueError("CASE_ID_INVALID: 用例编号只能包含字母、数字、点、下划线和连字符")
        return case_id

    @staticmethod
    def _platforms(project: dict[str, Any], raw: Any) -> list[str]:
        allowed = [str(value) for value in project.get("allowed_platforms", [])]
        values = raw if isinstance(raw, list) else allowed
        result = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        if not result or any(value not in allowed for value in result):
            raise ValueError("PLATFORM_NOT_APPLICABLE: 适用平台必须属于项目允许的平台")
        return result

    def normalize_case(
        self,
        project: dict[str, Any],
        raw: dict[str, Any],
        *,
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("case 必须是对象")
        base = copy.deepcopy(existing or {})
        base.update(copy.deepcopy(raw))
        case_id = self._validate_case_id(base.get("case_id"))
        sheet = self._validate_sheet(base.get("sheet") or base.get("file_sheet"))
        workflow_state = str(base.get("workflow_state") or "ACTIVE").strip().upper()
        if workflow_state not in WORKFLOW_STATES:
            raise ValueError("CASE_WORKFLOW_STATE_INVALID")
        steps = str(base.get("steps_text") or "").strip()
        expected = str(base.get("expected_text") or "").strip()
        if not steps or not expected:
            raise ValueError("CASE_CONTENT_REQUIRED: 操作步骤和预期结果为必填项")
        applicable = self._platforms(project, base.get("applicable_platforms"))
        points = base.get("verification_points")
        if not isinstance(points, list):
            points = [expected] if expected else []
        raw_mapping_status = str(base.get("mapping_status") or "")
        raw_maturity = base.get("automation_maturity")
        if not raw_maturity:
            raw_maturity = raw_mapping_status if raw_mapping_status in {"PROMOTED", "AUTO_READY"} else "UNMAPPED"
        return {
            **base,
            "case_id": case_id,
            "sheet": sheet,
            "file_sheet": sheet,
            "title": str(base.get("title") or expected.splitlines()[0] or case_id).strip(),
            "priority": str(base.get("priority") or "P1").strip().upper(),
            "precondition_text": str(base.get("precondition_text") or "").strip(),
            "steps_text": steps,
            "expected_text": expected,
            "verification_points": [str(value).strip() for value in points if str(value).strip()],
            "setup": list(base.get("setup")) if isinstance(base.get("setup"), list) else [],
            "actions": list(base.get("actions")) if isinstance(base.get("actions"), list) else [],
            "collect": list(base.get("collect")) if isinstance(base.get("collect"), list) else [],
            "unable": bool(base.get("unable", False)),
            # 兼容旧 W30 语义：只有精确的 PROMOTED 才是正式固化，
            # 小写或带空格的历史脏值不能被自动“修正”为已固化。
            "mapping_status": raw_mapping_status,
            "automation_maturity": str(raw_maturity).strip().upper(),
            "note": str(base.get("note") or "").strip(),
            "blockers": [str(value) for value in base.get("blockers", []) if str(value).strip()]
            if isinstance(base.get("blockers"), list) else [],
            "platform_automation": copy.deepcopy(base.get("platform_automation", {}))
            if isinstance(base.get("platform_automation"), dict) else {},
            "source_ref": copy.deepcopy(base.get("source_ref", {}))
            if isinstance(base.get("source_ref"), dict) else {},
            "execution_ref": copy.deepcopy(base.get("execution_ref", {}))
            if isinstance(base.get("execution_ref"), dict) else {},
            "applicable_platforms": applicable,
            "workflow_state": workflow_state,
        }

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        project_id: str,
        case_id: str,
        event_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO case_audit_events VALUES (?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, project_id, case_id, event_type, _json(details or {}), _now()),
        )

    def _insert_binding_rows(
        self,
        connection: sqlite3.Connection,
        project: dict[str, Any],
        case: dict[str, Any],
    ) -> None:
        project_id = str(project["project_id"])
        case_id = str(case["case_id"])
        applicable = set(case["applicable_platforms"])
        raw_bindings = case.get("platform_automation") or {}
        for platform_id in project.get("allowed_platforms", []):
            if platform_id not in applicable:
                continue
            binding = copy.deepcopy(raw_bindings.get(platform_id, {}))
            if not isinstance(binding, dict):
                binding = {}
            maturity = str(
                binding.get("maturity")
                or (case.get("automation_maturity") if platform_id == project.get("default_platform") else "")
                or (case.get("mapping_status") if platform_id == "w30" else "")
                or "UNMAPPED"
            ).upper()
            if platform_id == "w30" and case.get("mapping_status") == "PROMOTED":
                maturity = "PROMOTED"
                binding["maturity"] = "PROMOTED"
                binding["runnable"] = True
            if binding:
                runnable = bool(binding.get("runnable"))
            elif platform_id == "w30":
                runnable = not bool(case.get("unable"))
            else:
                runnable = False
            blocker = str(binding.get("blocker") or "")
            if not runnable and not blocker:
                blocker = "CASE_PLATFORM_MAPPING_MISSING" if platform_id == "579" else str(case.get("note") or "CASE_PLATFORM_MAPPING_MISSING")
            if blocker == "CASE_PLATFORM_MAPPING_MISSING":
                binding.setdefault("blocker_label", "尚未完成平台自动化绑定")
            targets = [
                str(value) for value in project.get("allowed_targets", [])
                if str(value).split(".", 1)[0] == platform_id
                or (platform_id == "w30" and str(value).startswith("w30."))
            ]
            target_id = targets[0] if targets else ""
            existing_binding = connection.execute(
                """SELECT binding_json FROM case_platform_bindings
                   WHERE project_id=? AND case_id=? AND platform_id=? AND target_id=?""",
                (project_id, case_id, platform_id, target_id),
            ).fetchone()
            if existing_binding and _loads(existing_binding["binding_json"], {}).get("managed_override") is True:
                continue
            connection.execute(
                """
                INSERT INTO case_platform_bindings(
                    project_id, case_id, platform_id, target_id, binding_version,
                    automation_maturity, runnable, blocker, binding_json, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, case_id, platform_id, target_id) DO UPDATE SET
                    automation_maturity=excluded.automation_maturity,
                    runnable=excluded.runnable,
                    blocker=excluded.blocker,
                    binding_json=excluded.binding_json,
                    updated_at=excluded.updated_at
                """,
                (
                    project_id, case_id, platform_id, target_id, maturity,
                    int(runnable), blocker, _json(binding), _now(),
                ),
            )

    def transition_binding(
        self,
        project: dict[str, Any],
        case_id: str,
        platform_id: str,
        action: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Advance a public binding workflow without accepting raw device commands."""

        case_id = self._validate_case_id(case_id)
        platform_id = str(platform_id or "").strip()
        action = str(action or "").strip().lower()
        payload = copy.deepcopy(payload or {})
        if platform_id not in project.get("allowed_platforms", []):
            raise ValueError("PLATFORM_NOT_APPLICABLE")
        forbidden = {"raw", "raw_command", "command", "commands", "params", "payload"}
        if any(str(key).lower() in forbidden for key in payload):
            raise ValueError("RAW_ACTION_FORBIDDEN")
        allowed_candidate = {
            "binding_ref", "plan_sha256", "action_ids", "setup_action_ids",
            "teardown_action_ids", "assertion_ids", "evidence_requirements", "note",
        }
        with self._lock, self._connect() as connection:
            case_row = connection.execute(
                "SELECT * FROM test_cases WHERE project_id=? AND case_id=?",
                (project["project_id"], case_id),
            ).fetchone()
            if case_row is None:
                raise ValueError(f"CASE_NOT_FOUND: {case_id}")
            case = self._row_case(connection, case_row)
            if platform_id not in case.get("applicable_platforms", []):
                raise ValueError("PLATFORM_NOT_APPLICABLE")
            row = connection.execute(
                """SELECT * FROM case_platform_bindings
                   WHERE project_id=? AND case_id=? AND platform_id=?
                   ORDER BY target_id LIMIT 1""",
                (project["project_id"], case_id, platform_id),
            ).fetchone()
            if row is None:
                raise ValueError("BINDING_NOT_READY")
            details = _loads(row["binding_json"], {})
            version = int(row["binding_version"]) + 1
            maturity = str(row["automation_maturity"])
            runnable = False
            blocker = "BINDING_NOT_READY"
            if action == "candidate":
                unknown = sorted(set(payload) - allowed_candidate)
                if unknown:
                    raise ValueError("BINDING_FIELD_FORBIDDEN: " + ", ".join(unknown))
                binding_ref = str(payload.get("binding_ref") or "").strip()
                plan_sha256 = str(payload.get("plan_sha256") or "").strip().upper()
                if platform_id == "579" and (not binding_ref or not re.fullmatch(r"[A-Fa-f0-9]{64}", plan_sha256)):
                    raise ValueError("CAPABILITY_MISSING: 579 候选必须引用已登记 binding_ref 与 plan_sha256")
                details = {
                    "managed_override": True,
                    "candidate": {key: value for key, value in payload.items() if key in allowed_candidate},
                    "review_status": "PENDING",
                }
                maturity = "NEED_REVIEW"
            elif action == "review":
                if not isinstance(details.get("candidate"), dict):
                    raise ValueError("BINDING_NOT_READY: 尚未提交绑定候选")
                approved = payload.get("approved") is True
                details["managed_override"] = True
                details["review_status"] = "APPROVED" if approved else "REJECTED"
                details["review_note"] = str(payload.get("review_note") or "")
                maturity = "NEED_REVIEW"
            elif action == "promote":
                if details.get("review_status") != "APPROVED":
                    raise ValueError("BINDING_NOT_READY: 绑定候选尚未评审通过")
                details["managed_override"] = True
                details["promoted_at"] = _now()
                maturity = "AUTO_READY" if platform_id == "579" else "PROMOTED"
                runnable = True
                blocker = ""
            elif action == "rollback":
                details = {"managed_override": True, "review_status": "ROLLED_BACK"}
                maturity = "UNMAPPED"
                blocker = "CASE_PLATFORM_MAPPING_MISSING"
            else:
                raise ValueError("BINDING_ACTION_INVALID")
            connection.execute(
                """UPDATE case_platform_bindings
                   SET binding_version=?, automation_maturity=?, runnable=?, blocker=?,
                       binding_json=?, updated_at=?
                   WHERE project_id=? AND case_id=? AND platform_id=? AND target_id=?""",
                (
                    version, maturity, int(runnable), blocker, _json(details), _now(),
                    project["project_id"], case_id, platform_id, row["target_id"],
                ),
            )
            self._audit(connection, str(project["project_id"]), case_id, f"BINDING_{action.upper()}", {
                "platform_id": platform_id, "binding_version": version, "maturity": maturity,
            })
            return {
                "case_id": case_id,
                "platform_id": platform_id,
                "target_id": row["target_id"],
                "binding_version": version,
                "automation_maturity": maturity,
                "runnable": runnable,
                "blocker": blocker,
                "review_status": details.get("review_status", ""),
            }

    def _insert_case(
        self,
        connection: sqlite3.Connection,
        project: dict[str, Any],
        case: dict[str, Any],
        *,
        source_type: str,
        source_locked: bool,
        source_ref: dict[str, Any] | None,
        source_sha256: str,
        change_type: str,
        change_summary: str = "",
    ) -> dict[str, Any]:
        project_id = str(project["project_id"])
        now = _now()
        content_json = _json(case)
        connection.execute(
            """
            INSERT INTO test_cases(
                project_id, case_id, sheet, title, priority, workflow_state,
                applicable_platforms_json, source_type, source_locked,
                has_managed_override, source_ref_json, source_sha256,
                source_snapshot_json, current_revision, current_content_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                project_id, case["case_id"], case["sheet"], case["title"], case["priority"],
                case["workflow_state"], _json(case["applicable_platforms"]), source_type,
                int(source_locked), _json(source_ref or case.get("source_ref") or {}),
                source_sha256, content_json if source_locked else "{}", content_json, now, now,
            ),
        )
        connection.execute(
            "INSERT INTO case_revisions VALUES (?, ?, 1, ?, ?, ?, ?, 'local-user', ?)",
            (
                project_id, case["case_id"], content_json, _digest(case), change_type,
                change_summary, now,
            ),
        )
        self._insert_binding_rows(connection, project, case)
        self._audit(connection, project_id, case["case_id"], change_type, {"revision": 1})
        return {"case_id": case["case_id"], "revision": 1}

    def create_case(
        self,
        project: dict[str, Any],
        raw: dict[str, Any],
        *,
        source_type: str = "MANUAL",
        change_type: str = "CREATE",
        change_summary: str = "",
    ) -> dict[str, Any]:
        case = self.normalize_case(project, raw)
        with self._lock, self._connect() as connection:
            try:
                return self._insert_case(
                    connection, project, case, source_type=source_type,
                    source_locked=False, source_ref=None, source_sha256="",
                    change_type=change_type, change_summary=change_summary,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"CASE_ID_CONFLICT: {case['case_id']}") from exc

    def _row_case(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        case = _loads(row["current_content_json"], {})
        bindings: dict[str, dict[str, Any]] = {}
        for binding in connection.execute(
            "SELECT * FROM case_platform_bindings WHERE project_id=? AND case_id=?",
            (row["project_id"], row["case_id"]),
        ):
            details = _loads(binding["binding_json"], {})
            details.update({
                "maturity": binding["automation_maturity"],
                "runnable": bool(binding["runnable"]),
                "blocker": binding["blocker"],
                "target_id": binding["target_id"],
                "binding_version": int(binding["binding_version"]),
            })
            bindings[str(binding["platform_id"])] = details
        case.update({
            "case_id": row["case_id"],
            "sheet": row["sheet"],
            "file_sheet": row["sheet"],
            "title": row["title"],
            "priority": row["priority"],
            "workflow_state": row["workflow_state"],
            "applicable_platforms": _loads(row["applicable_platforms_json"], []),
            "source_type": row["source_type"],
            "source_locked": bool(row["source_locked"]),
            "has_managed_override": bool(row["has_managed_override"]),
            "is_frozen_source": bool(row["source_locked"]),
            "source_ref": _loads(row["source_ref_json"], case.get("source_ref", {})),
            "source_sha256": row["source_sha256"],
            "current_revision": int(row["current_revision"]),
            "platform_automation": bindings,
        })
        default_platform = case["applicable_platforms"][0] if case["applicable_platforms"] else ""
        if default_platform in bindings:
            case["automation_maturity"] = bindings[default_platform]["maturity"]
        return case

    def list_cases(self, project_id: str, *, include_archived: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM test_cases WHERE project_id=?"
        params: list[Any] = [project_id]
        if not include_archived:
            query += " AND workflow_state != 'ARCHIVED'"
        query += " ORDER BY sheet, case_id"
        with self._lock, self._connect() as connection:
            return [self._row_case(connection, row) for row in connection.execute(query, params)]

    def get_case(self, project_id: str, case_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM test_cases WHERE project_id=? AND case_id=?",
                (project_id, case_id),
            ).fetchone()
            return self._row_case(connection, row) if row else None

    def create_revision(
        self,
        project: dict[str, Any],
        case_id: str,
        changes: dict[str, Any],
        *,
        change_type: str = "EDIT",
        change_summary: str = "",
    ) -> dict[str, Any]:
        case_id = self._validate_case_id(case_id)
        if changes.get("case_id") and str(changes["case_id"]).strip() != case_id:
            raise ValueError("CASE_ID_IMMUTABLE: 用例编号不可直接修改，请复制为新用例")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM test_cases WHERE project_id=? AND case_id=?",
                (project["project_id"], case_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"CASE_NOT_FOUND: {case_id}")
            current = self._row_case(connection, row)
            candidate = self.normalize_case(project, {**current, **copy.deepcopy(changes), "case_id": case_id})
            revision = int(row["current_revision"]) + 1
            now = _now()
            content_json = _json(candidate)
            connection.execute(
                """
                UPDATE test_cases SET sheet=?, title=?, priority=?, workflow_state=?,
                    applicable_platforms_json=?, has_managed_override=1,
                    current_revision=?, current_content_json=?, updated_at=?
                WHERE project_id=? AND case_id=?
                """,
                (
                    candidate["sheet"], candidate["title"], candidate["priority"],
                    candidate["workflow_state"], _json(candidate["applicable_platforms"]),
                    revision, content_json, now, project["project_id"], case_id,
                ),
            )
            connection.execute(
                "INSERT INTO case_revisions VALUES (?, ?, ?, ?, ?, ?, ?, 'local-user', ?)",
                (
                    project["project_id"], case_id, revision, content_json, _digest(candidate),
                    change_type, change_summary, now,
                ),
            )
            self._insert_binding_rows(connection, project, candidate)
            self._audit(connection, project["project_id"], case_id, change_type, {"revision": revision})
            return {"case_id": case_id, "revision": revision}

    def revisions(self, project_id: str, case_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT revision, content_json, content_sha256, change_type, change_summary,
                          created_by, created_at
                   FROM case_revisions WHERE project_id=? AND case_id=?
                   ORDER BY revision DESC""",
                (project_id, case_id),
            ).fetchall()
            return [
                {**dict(row), "content": _loads(row["content_json"], {})}
                for row in rows
            ]

    def set_workflow_state(self, project: dict[str, Any], case_id: str, state: str) -> dict[str, Any]:
        state = state.upper()
        if state not in {"ACTIVE", "ARCHIVED"}:
            raise ValueError("CASE_WORKFLOW_STATE_INVALID")
        return self.create_revision(
            project, case_id, {"workflow_state": state},
            change_type="ARCHIVE" if state == "ARCHIVED" else "RESTORE",
        )

    def clone_case(self, project: dict[str, Any], case_id: str, new_case_id: str) -> dict[str, Any]:
        source = self.get_case(str(project["project_id"]), case_id)
        if source is None:
            raise ValueError(f"CASE_NOT_FOUND: {case_id}")
        payload = {
            key: copy.deepcopy(value)
            for key, value in source.items()
            if key not in {
                "current_revision", "source_locked", "has_managed_override", "is_frozen_source",
                "source_sha256", "source_type", "source_ref", "platform_automation",
            }
        }
        payload.update({
            "case_id": new_case_id,
            "workflow_state": "DRAFT",
            "mapping_status": "",
            "automation_maturity": "UNMAPPED",
            "setup": [], "actions": [], "collect": [],
        })
        return self.create_case(project, payload, source_type="CLONE", change_type="CLONE", change_summary=f"复制自 {case_id}")

    def sync_source_cases(
        self,
        project: dict[str, Any],
        cases: Iterable[dict[str, Any]],
        *,
        source_fingerprint: str = "",
    ) -> dict[str, int]:
        source_type = "MANIFEST_579" if (project.get("case_catalog") or {}).get("type") == "manifest_579" else "W30_CASE_MAP"
        created = updated = unchanged = conflicts = source_case_count = 0
        project_id = str(project["project_id"])
        with self._lock, self._connect() as connection:
            for raw in cases:
                source_case_count += 1
                case = self.normalize_case(project, raw)
                source_ref = copy.deepcopy(case.get("source_ref") or {})
                source_ref.update({
                    "file": str(raw.get("_source_file") or ""),
                    "file_sha256": str(raw.get("_source_file_sha256") or ""),
                })
                source_sha = str(source_ref.get("source_sha256") or source_ref.get("file_sha256") or _digest(case)).upper()
                row = connection.execute(
                    "SELECT * FROM test_cases WHERE project_id=? AND case_id=?",
                    (project_id, case["case_id"]),
                ).fetchone()
                if row is None:
                    self._insert_case(
                        connection, project, case, source_type=source_type,
                        source_locked=True, source_ref=source_ref, source_sha256=source_sha,
                        change_type="SOURCE_IMPORT", change_summary="导入冻结/Case Map 基线",
                    )
                    created += 1
                    continue
                if row["source_type"] not in {"MANIFEST_579", "W30_CASE_MAP"}:
                    conflicts += 1
                    self._audit(connection, project_id, case["case_id"], "SOURCE_ID_CONFLICT", {"source_type": source_type})
                    continue
                if bool(row["has_managed_override"]):
                    connection.execute(
                        "UPDATE test_cases SET source_ref_json=?, source_sha256=?, source_snapshot_json=? WHERE project_id=? AND case_id=?",
                        (_json(source_ref), source_sha, _json(case), project_id, case["case_id"]),
                    )
                    unchanged += 1
                    continue
                current = _loads(row["current_content_json"], {})
                if _digest(current) == _digest(case):
                    unchanged += 1
                    self._insert_binding_rows(connection, project, case)
                    continue
                revision = int(row["current_revision"]) + 1
                now = _now()
                connection.execute(
                    """UPDATE test_cases SET sheet=?, title=?, priority=?, workflow_state=?,
                       applicable_platforms_json=?, source_ref_json=?, source_sha256=?,
                       source_snapshot_json=?, current_revision=?, current_content_json=?, updated_at=?
                       WHERE project_id=? AND case_id=?""",
                    (
                        case["sheet"], case["title"], case["priority"], case["workflow_state"],
                        _json(case["applicable_platforms"]), _json(source_ref), source_sha,
                        _json(case), revision, _json(case), now, project_id, case["case_id"],
                    ),
                )
                connection.execute(
                    "INSERT INTO case_revisions VALUES (?, ?, ?, ?, ?, 'SOURCE_SYNC', '', 'system', ?)",
                    (project_id, case["case_id"], revision, _json(case), _digest(case), now),
                )
                self._insert_binding_rows(connection, project, case)
                updated += 1
            self._audit(connection, project_id, "", "SOURCE_SYNC", {
                "created": created, "updated": updated, "unchanged": unchanged, "conflicts": conflicts,
            })
            if source_fingerprint:
                connection.execute(
                    """INSERT INTO source_sync_state(
                           project_id, source_fingerprint, source_case_count, synced_at
                       ) VALUES (?, ?, ?, ?)
                       ON CONFLICT(project_id) DO UPDATE SET
                           source_fingerprint=excluded.source_fingerprint,
                           source_case_count=excluded.source_case_count,
                           synced_at=excluded.synced_at""",
                    (project_id, source_fingerprint, source_case_count, _now()),
                )
        return {"created": created, "updated": updated, "unchanged": unchanged, "conflicts": conflicts}

    def source_sync_matches(self, project_id: str, source_fingerprint: str) -> bool:
        """Return whether the immutable source catalog is already synchronized."""

        if not source_fingerprint:
            return False
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT source_fingerprint FROM source_sync_state WHERE project_id=?",
                (str(project_id),),
            ).fetchone()
        return bool(row and str(row["source_fingerprint"]) == source_fingerprint)

    def preview_import(
        self,
        project: dict[str, Any],
        cases: list[dict[str, Any]],
        *,
        source_filename: str,
        source_sha256: str,
        conflict_strategy: str = "SKIP",
    ) -> dict[str, Any]:
        strategy = str(conflict_strategy or "SKIP").upper()
        if strategy not in IMPORT_STRATEGIES:
            raise ValueError("IMPORT_STRATEGY_INVALID")
        normalized = [self.normalize_case(project, case) for case in cases]
        case_ids = [case["case_id"] for case in normalized]
        duplicates = sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
        if duplicates:
            raise ValueError("IMPORT_DUPLICATE_CASE_ID: " + ", ".join(duplicates))
        batch_id = f"import-{uuid.uuid4().hex}"
        token = uuid.uuid4().hex
        new_count = existing_count = 0
        with self._lock, self._connect() as connection:
            connection.execute(
                """INSERT INTO import_batches(
                    batch_id, preview_token, project_id, source_type, source_filename,
                    source_sha256, conflict_strategy, status, total_rows, created_at
                ) VALUES (?, ?, ?, 'EXCEL', ?, ?, ?, 'PREVIEW', ?, ?)""",
                (
                    batch_id, token, project["project_id"], source_filename,
                    source_sha256, strategy, len(normalized), _now(),
                ),
            )
            for index, case in enumerate(normalized, start=1):
                exists = connection.execute(
                    "SELECT 1 FROM test_cases WHERE project_id=? AND case_id=?",
                    (project["project_id"], case["case_id"]),
                ).fetchone() is not None
                disposition = "NEW_REVISION" if exists and strategy == "NEW_REVISION" else "SKIP" if exists else "CREATE"
                new_count += int(not exists)
                existing_count += int(exists)
                connection.execute(
                    "INSERT INTO import_batch_rows VALUES (?, ?, ?, ?, ?, ?, '')",
                    (batch_id, index, case["case_id"], case["sheet"], disposition, _json(case)),
                )
        return {
            "batch_id": batch_id,
            "preview_token": token,
            "source_sha256": source_sha256,
            "new_count": new_count,
            "existing_count": existing_count,
            "total_parsed": len(normalized),
            "modules": sorted({case["sheet"] for case in normalized}),
            "new_cases": [
                {"case_id": case["case_id"], "sheet": case["sheet"], "expected_text": case["expected_text"]}
                for case in normalized if not self.get_case(str(project["project_id"]), case["case_id"])
            ],
        }

    def commit_import(
        self,
        project: dict[str, Any],
        *,
        batch_id: str,
        preview_token: str,
        source_sha256: str,
    ) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            batch = connection.execute(
                "SELECT * FROM import_batches WHERE batch_id=? AND project_id=?",
                (batch_id, project["project_id"]),
            ).fetchone()
            if batch is None or batch["preview_token"] != preview_token:
                raise ValueError("IMPORT_PREVIEW_EXPIRED")
            if batch["status"] != "PREVIEW":
                raise ValueError("IMPORT_ALREADY_COMMITTED")
            if str(batch["source_sha256"]).upper() != str(source_sha256).upper():
                raise ValueError("IMPORT_SOURCE_CHANGED")
            created = revisions = skipped = failed = 0
            modules: set[str] = set()
            rows = connection.execute(
                "SELECT * FROM import_batch_rows WHERE batch_id=? ORDER BY row_number",
                (batch_id,),
            ).fetchall()
            # 提交前一次性校验数据库状态；任一并发冲突都在任何写入前终止，
            # 保证当前默认严格模式不会留下半批数据。
            for row in rows:
                exists = connection.execute(
                    "SELECT 1 FROM test_cases WHERE project_id=? AND case_id=?",
                    (project["project_id"], row["case_id"]),
                ).fetchone() is not None
                if row["disposition"] == "CREATE" and exists:
                    raise ValueError(f"CASE_REVISION_CONFLICT: {row['case_id']}")
                if row["disposition"] == "NEW_REVISION" and not exists:
                    raise ValueError(f"CASE_REVISION_CONFLICT: {row['case_id']}")
            for row in rows:
                case = _loads(row["case_json"], {})
                if row["disposition"] == "CREATE":
                    self._insert_case(
                        connection, project, case, source_type="EXCEL",
                        source_locked=False, source_ref={"import_batch_id": batch_id},
                        source_sha256=source_sha256, change_type="IMPORT_CREATE",
                    )
                    created += 1
                elif row["disposition"] == "NEW_REVISION":
                    existing = connection.execute(
                        "SELECT * FROM test_cases WHERE project_id=? AND case_id=?",
                        (project["project_id"], case["case_id"]),
                    ).fetchone()
                    current = self._row_case(connection, existing)
                    candidate = self.normalize_case(project, {**current, **case})
                    revision = int(existing["current_revision"]) + 1
                    now = _now()
                    connection.execute(
                        """UPDATE test_cases SET sheet=?, title=?, priority=?, workflow_state=?,
                           applicable_platforms_json=?, has_managed_override=1,
                           current_revision=?, current_content_json=?, updated_at=?
                           WHERE project_id=? AND case_id=?""",
                        (
                            candidate["sheet"], candidate["title"], candidate["priority"], candidate["workflow_state"],
                            _json(candidate["applicable_platforms"]), revision, _json(candidate), now,
                            project["project_id"], candidate["case_id"],
                        ),
                    )
                    connection.execute(
                        "INSERT INTO case_revisions VALUES (?, ?, ?, ?, ?, 'IMPORT_REVISION', ?, 'local-user', ?)",
                        (
                            project["project_id"], candidate["case_id"], revision, _json(candidate),
                            _digest(candidate), f"Excel 导入批次 {batch_id}", now,
                        ),
                    )
                    self._insert_binding_rows(connection, project, candidate)
                    revisions += 1
                else:
                    skipped += 1
                modules.add(str(case.get("sheet") or ""))
            connection.execute(
                """UPDATE import_batches SET status='COMMITTED', created_cases=?,
                   new_revisions=?, skipped_rows=?, failed_rows=?, committed_at=? WHERE batch_id=?""",
                (created, revisions, skipped, failed, _now(), batch_id),
            )
            self._audit(connection, project["project_id"], "", "IMPORT_COMMIT", {
                "batch_id": batch_id, "created": created, "revisions": revisions,
                "skipped": skipped, "failed": failed,
            })
            return {
                "batch_id": batch_id,
                "imported_count": created,
                "overwritten_count": revisions,
                "new_revision_count": revisions,
                "skipped_count": skipped,
                "failed_count": failed,
                "modules_updated": sorted(value for value in modules if value),
            }

    def import_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM import_batches WHERE batch_id=?", (batch_id,)).fetchone()
            return dict(row) if row else None

    def import_errors(self, batch_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT row_number, case_id, sheet, disposition, error
                   FROM import_batch_rows
                   WHERE batch_id=? AND (disposition='FAILED' OR error != '')
                   ORDER BY row_number""",
                (batch_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def audit_events(self, project_id: str, case_id: str = "") -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            if case_id:
                rows = connection.execute(
                    "SELECT * FROM case_audit_events WHERE project_id=? AND case_id=? ORDER BY created_at DESC",
                    (project_id, case_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM case_audit_events WHERE project_id=? ORDER BY created_at DESC",
                    (project_id,),
                ).fetchall()
            return [{**dict(row), "details": _loads(row["details_json"], {})} for row in rows]
