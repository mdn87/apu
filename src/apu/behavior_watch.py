from __future__ import annotations

import json
import ntpath
import os
import re
import shutil
import subprocess
from collections import Counter, deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, TypeAlias
from uuid import uuid4

from . import __version__
from .audit import build_inventory
from .evidence import EVIDENCE_SCHEMA_VERSION, ingest_codex_trace, reconcile_evidence
from .models import canonical_json, sha256_bytes
from .state import ensure_state_home, write_json_atomic
from .work_orders import find_secret_spans

WATCHER_ID = "primary-agent-autonomy-loss"
WATCHER_ALIASES = frozenset({WATCHER_ID, "autonomy-loss"})
_MAX_TRACE_BYTES = 256 * 1024 * 1024
_MAX_DESCRIPTION_CHARS = 2_000
_RECENT_EVENT_LIMIT = 80
_SCHEMA_VERSION = 1
_MAX_SESSION_AGE_SECONDS = 10 * 60
_MAX_FUTURE_EVENT_SKEW_SECONDS = 5
_MAX_SESSION_PATHS = 1_000
_PEEK_MAX_LINES = 512
_PEEK_MAX_BYTES = 1024 * 1024
_NO_ATTRIBUTION_REASONS = frozenset(
    {
        "ambiguous_active_candidates",
        "ambiguous_session_id",
        "cwd_mismatch",
        "no_active_candidate",
        "no_exact_cwd_candidate",
        "session_not_found",
        "stale_trace",
        "trace_root_unavailable",
        "unparsable_trace",
    }
)

_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "redundant-context-request",
        re.compile(
            r"(?:already (?:available|provided|in (?:context|the repo))|"
            r"information .*already|ask(?:ed)? .*context)",
            re.IGNORECASE,
        ),
    ),
    (
        "reversible-choice-escalation",
        re.compile(
            r"(?=.*(?:approv|confirm|choose|which|permission))"
            r"(?=.*(?:reversible|filename|file name|default|minor|safe choice)).+",
            re.IGNORECASE,
        ),
    ),
    (
        "premature-stop",
        re.compile(
            r"(?:premature|stopp?ed|stop(?:s|ping)? before|did not finish|"
            r"didn't finish|left .*unfinished|without completing)",
            re.IGNORECASE,
        ),
    ),
    (
        "invented-gate",
        re.compile(
            r"(?:new prerequisite|invented gate|made .*mandatory|"
            r"blocked .*review|review .*required|must .* before continuing)",
            re.IGNORECASE,
        ),
    ),
    (
        "terminal-tool-failure",
        re.compile(
            r"(?=.*(?:tool|command|test|check).*(?:fail|error))"
            r"(?=.*(?:terminal|stopp|gave up|no fallback|without .*fallback)).+",
            re.IGNORECASE,
        ),
    ),
    (
        "delegation-not-integrated",
        re.compile(
            r"(?=.*(?:delegat|subagent|child agent))"
            r"(?=.*(?:did not integrat|didn't integrat|handed .*user|"
            r"without finishing|never finish)).+",
            re.IGNORECASE,
        ),
    ),
    (
        "user-action-transfer",
        re.compile(
            r"(?=.*(?:asked|told|left|transfer))(?=.*(?:me|user))"
            r"(?=.*(?:could .*itself|had the tools|had permission|to do it)).+",
            re.IGNORECASE,
        ),
    ),
)

_AGENT_MESSAGE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "redundant-context-request",
        re.compile(
            r"(?:please provide|can you provide|I need .* from you)", re.IGNORECASE
        ),
    ),
    (
        "reversible-choice-escalation",
        re.compile(
            r"(?:which .* (?:name|file)|would you (?:like|prefer)|approve)",
            re.IGNORECASE,
        ),
    ),
    (
        "premature-stop",
        re.compile(
            r"(?:I can(?:not|'t) continue|I(?:'m| am) blocked|unable to proceed)",
            re.IGNORECASE,
        ),
    ),
    (
        "user-action-transfer",
        re.compile(
            r"(?:you can run|you(?:'ll| will) need to|please run|do this manually)",
            re.IGNORECASE,
        ),
    ),
)

_BARRIER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "credential-barrier",
        re.compile(r"(?:credential|API key|login|authentication)", re.IGNORECASE),
    ),
    (
        "permission-barrier",
        re.compile(
            r"(?:permission denied|access denied|not authorized)", re.IGNORECASE
        ),
    ),
    (
        "destructive-side-effect",
        re.compile(
            r"(?:destructive|irreversible|delete|drop database|force.push)",
            re.IGNORECASE,
        ),
    ),
    (
        "external-side-effect",
        re.compile(
            r"(?:production|send email|publish|charge|payment|remote server)",
            re.IGNORECASE,
        ),
    ),
    (
        "material-information-gap",
        re.compile(
            r"(?:materially change|missing requirement|unknown target)", re.IGNORECASE
        ),
    ),
)

_SOURCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "approval-pressure",
        re.compile(
            r"\b(?:ask|approval|approve|confirm|permission|review)\b", re.IGNORECASE
        ),
    ),
    (
        "workflow-gate",
        re.compile(
            r"\b(?:must|required|prerequisite|before continuing|do not proceed)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "stop-pressure",
        re.compile(
            r"\b(?:stop|wait|blocked|clarif|cannot continue|can't continue)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "delegation-pressure",
        re.compile(r"\b(?:delegate|subagent|child agent|spawn_agent)\b", re.IGNORECASE),
    ),
    (
        "fallback-pressure",
        re.compile(r"\b(?:fallback|retry|failed|failure|error)\b", re.IGNORECASE),
    ),
)

_RESUME_INSTRUCTION = (
    "Resume the original task now. Use the available context and repository "
    "conventions, choose reasonable reversible defaults, try safe fallbacks for "
    "minor failures, integrate delegated results, and continue until the "
    "requested outcome is complete. Stop only for a real permission or credential "
    "barrier, a destructive or external side effect, an explicit user-requested "
    "approval point, or missing information that materially changes the result."
)


@dataclass(frozen=True)
class SessionTrace:
    path: Path
    session_id: str
    cwd: Path
    source: str | None
    originator: str | None
    cli_version: str | None
    active: bool
    non_interactive: bool
    last_event_at: str | None
    trace_sha256: str
    record_count: int
    invalid_record_count: int
    task_started_count: int
    task_completed_count: int
    event_counts: Mapping[str, int]
    tool_calls: Mapping[str, int]
    evidence_events: tuple[Mapping[str, Any], ...]
    signal_codes: tuple[str, ...]
    settings: Mapping[str, str | None]
    base_instructions_sha256: str | None
    dynamic_tools_sha256: str | None


@dataclass(frozen=True)
class SelectionProvenance:
    selector_mode: Literal[
        "exact_session_binding",
        "explicit_session_id",
        "unique_active_exact_cwd",
    ]
    candidate_count: int
    last_event_age_seconds: int | None
    confidence: Literal["exact"] = "exact"

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector_mode": self.selector_mode,
            "candidate_count": self.candidate_count,
            "last_event_age_seconds": self.last_event_age_seconds,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class SelectedSession:
    kind: Literal["selected"]
    session: SessionTrace
    provenance: SelectionProvenance


@dataclass(frozen=True)
class NoAttribution:
    kind: Literal["no_attribution"]
    reason_code: str
    provenance: SelectionProvenance

    def __post_init__(self) -> None:
        if self.reason_code not in _NO_ATTRIBUTION_REASONS:
            raise ValueError("unsupported no-attribution reason code")


SessionSelection: TypeAlias = SelectedSession | NoAttribution


class NoAttributionError(RuntimeError):
    """Raised when an operation requires an exact session binding."""

    def __init__(self, operation: str, result: NoAttribution) -> None:
        self.operation = operation
        self.result = result
        super().__init__(f"no_attribution: {result.reason_code} ({operation})")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _selection_clock(now: datetime | None) -> datetime:
    selected = datetime.now(UTC) if now is None else now
    if selected.tzinfo is None:
        raise ValueError("selection clock must include a timezone")
    return selected.astimezone(UTC)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _event_age_seconds(value: str | None, now: datetime) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    delta = (now - parsed.astimezone(UTC)).total_seconds()
    if delta < -_MAX_FUTURE_EVENT_SKEW_SECONDS:
        return None
    return max(0, int(delta))


def _windows_path(value: str) -> bool:
    return bool(re.match(r"^(?:[A-Za-z]:[\\/]|[\\/]{2})", value))


def normalized_cwd_key(value: Path | str) -> str:
    """Return a stable cwd identity, including Windows case folding."""

    raw = str(value)
    if os.name == "nt":
        raw = os.path.realpath(raw)
    if _windows_path(raw):
        return ntpath.normcase(ntpath.normpath(raw.replace("/", "\\")))
    return os.path.normcase(str(Path(raw).expanduser().resolve(strict=False)))


def require_selected_session(
    result: SessionSelection, *, operation: str
) -> SelectedSession:
    if isinstance(result, NoAttribution):
        raise NoAttributionError(operation, result)
    return result


def _provenance(
    mode: Literal[
        "exact_session_binding",
        "explicit_session_id",
        "unique_active_exact_cwd",
    ],
    *,
    candidate_count: int,
    last_event_age_seconds: int | None = None,
) -> SelectionProvenance:
    return SelectionProvenance(
        selector_mode=mode,
        candidate_count=candidate_count,
        last_event_age_seconds=last_event_age_seconds,
    )


def _safe_component(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"{field} must be one safe path component")
    return value


def codex_trace_root(
    *,
    environment: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    env = os.environ if environment is None else environment
    selected_home = Path.home() if home is None else Path(home)
    codex_home = Path(env.get("CODEX_HOME", selected_home / ".codex")).expanduser()
    return codex_home / "sessions"


def _text_sha256(value: Any) -> str | None:
    if value is None:
        return None
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _message_text(record: Mapping[str, Any]) -> str | None:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return None
    if record.get("type") == "event_msg" and payload.get("type") == "agent_message":
        message = payload.get("message")
        return message if isinstance(message, str) else None
    if (
        record.get("type") == "response_item"
        and payload.get("type") == "message"
        and payload.get("role") == "assistant"
    ):
        content = payload.get("content")
        if not isinstance(content, list):
            return None
        pieces: list[str] = []
        for item in content:
            if isinstance(item, Mapping):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    pieces.append(text)
        return "\n".join(pieces) if pieces else None
    return None


def _signals(text: str, *, agent_message: bool = False) -> tuple[str, ...]:
    patterns = _AGENT_MESSAGE_PATTERNS if agent_message else _SIGNAL_PATTERNS
    return tuple(sorted(code for code, pattern in patterns if pattern.search(text)))


def _barriers(text: str) -> tuple[str, ...]:
    return tuple(
        sorted(code for code, pattern in _BARRIER_PATTERNS if pattern.search(text))
    )


def _safe_setting(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping):
        for key in ("kind", "type", "mode", "name"):
            selected = value.get(key)
            if isinstance(selected, str) and selected:
                return selected
    return None


def _parse_session(path: Path) -> SessionTrace:
    if path.stat().st_size > _MAX_TRACE_BYTES:
        raise ValueError(f"Codex trace exceeds {_MAX_TRACE_BYTES} bytes: {path}")
    digest = sha256()
    recent: deque[dict[str, Any]] = deque(maxlen=_RECENT_EVENT_LIMIT)
    event_counts: Counter[str] = Counter()
    tool_calls: Counter[str] = Counter()
    signal_codes: set[str] = set()
    meta: dict[str, Any] = {}
    settings: dict[str, str | None] = {}
    started = 0
    completed = 0
    records = 0
    invalid = 0
    last_event_at: str | None = None
    base_instructions_sha256: str | None = None
    dynamic_tools_sha256: str | None = None

    with path.open("rb") as stream:
        for line_number, raw in enumerate(stream, start=1):
            digest.update(raw)
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                invalid += 1
                continue
            if not isinstance(record, dict):
                invalid += 1
                continue
            records += 1
            record_type = str(record.get("type") or "unknown")
            payload = record.get("payload")
            payload_type = (
                str(payload.get("type") or "") if isinstance(payload, Mapping) else ""
            )
            event_key = f"{record_type}:{payload_type or '-'}"
            event_counts[event_key] += 1
            timestamp = record.get("timestamp")
            if isinstance(timestamp, str):
                last_event_at = timestamp

            if record_type == "session_meta" and isinstance(payload, Mapping):
                meta = dict(payload)
                base_instructions_sha256 = _text_sha256(
                    payload.get("base_instructions")
                )
                dynamic_tools_sha256 = _text_sha256(payload.get("dynamic_tools"))
            elif record_type == "turn_context" and isinstance(payload, Mapping):
                settings.update(
                    {
                        "collaboration_mode": _safe_setting(
                            payload.get("collaboration_mode")
                        ),
                        "permission_profile": _safe_setting(
                            payload.get("permission_profile")
                        ),
                        "sandbox_policy": _safe_setting(payload.get("sandbox_policy")),
                        "model": _safe_setting(payload.get("model")),
                    }
                )
            elif record_type == "event_msg" and payload_type == "task_started":
                started += 1
            elif record_type == "event_msg" and payload_type == "task_complete":
                completed += 1

            if record_type == "response_item" and isinstance(payload, Mapping):
                if payload_type in {"function_call", "custom_tool_call"}:
                    name = payload.get("name")
                    if isinstance(name, str) and name:
                        tool_calls[name] += 1
                elif payload_type == "function_call_output":
                    output = payload.get("output")
                    if isinstance(output, str) and re.search(
                        r"(?:exit code: [1-9]|script failed|command failed)",
                        output,
                        re.IGNORECASE,
                    ):
                        signal_codes.add("tool-failure-observed")

            message = _message_text(record)
            message_signals = _signals(message, agent_message=True) if message else ()
            signal_codes.update(message_signals)
            recent.append(
                {
                    "line": line_number,
                    "timestamp": timestamp if isinstance(timestamp, str) else None,
                    "type": record_type,
                    "payload_type": payload_type or None,
                    "record_sha256": sha256_bytes(raw.rstrip(b"\r\n")),
                    "signals": list(message_signals),
                }
            )

    session_id = meta.get("id") or meta.get("session_id")
    cwd = meta.get("cwd")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError(f"Codex trace has no session id: {path}")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError(f"Codex trace has no absolute cwd: {path}")
    source = meta.get("source") if isinstance(meta.get("source"), str) else None
    originator = (
        meta.get("originator") if isinstance(meta.get("originator"), str) else None
    )
    source_label = f"{source or ''} {originator or ''}".lower()
    non_interactive = "exec" in source_label or "non-interactive" in source_label
    return SessionTrace(
        path=path.resolve(),
        session_id=session_id,
        cwd=Path(cwd).resolve(strict=False),
        source=source,
        originator=originator,
        cli_version=(
            meta.get("cli_version")
            if isinstance(meta.get("cli_version"), str)
            else None
        ),
        active=started > completed,
        non_interactive=non_interactive,
        last_event_at=last_event_at,
        trace_sha256=digest.hexdigest(),
        record_count=records,
        invalid_record_count=invalid,
        task_started_count=started,
        task_completed_count=completed,
        event_counts=dict(sorted(event_counts.items())),
        tool_calls=dict(sorted(tool_calls.items())),
        evidence_events=tuple(recent),
        signal_codes=tuple(sorted(signal_codes)),
        settings=dict(sorted(settings.items())),
        base_instructions_sha256=base_instructions_sha256,
        dynamic_tools_sha256=dynamic_tools_sha256,
    )


def _peek_session(path: Path) -> tuple[str | None, Path | None]:
    found_session_id: str | None = None
    found_cwd: Path | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            consumed = 0
            for _ in range(_PEEK_MAX_LINES):
                line = stream.readline()
                if not line:
                    break
                consumed += len(line.encode("utf-8", errors="replace"))
                if consumed > _PEEK_MAX_BYTES:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    not isinstance(record, Mapping)
                    or record.get("type") != "session_meta"
                ):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                session_id = payload.get("id") or payload.get("session_id")
                cwd = payload.get("cwd")
                if isinstance(session_id, str) and session_id:
                    found_session_id = session_id
                if isinstance(cwd, str) and Path(cwd).is_absolute():
                    found_cwd = Path(cwd).resolve(strict=False)
                if found_session_id is not None and found_cwd is not None:
                    return found_session_id, found_cwd
    except OSError:
        pass
    return found_session_id, found_cwd


def _available_trace_paths(root: Path) -> list[Path]:
    paths: list[tuple[int, Path]] = []
    for path in root.rglob("*.jsonl"):
        try:
            modified = path.stat().st_mtime_ns
        except OSError:
            continue
        paths.append((modified, path))
    paths.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in paths[:_MAX_SESSION_PATHS]]


def _parse_fresh_session(
    path: Path, now: datetime
) -> tuple[SessionTrace | None, int | None]:
    try:
        session = _parse_session(path)
    except (OSError, ValueError):
        return None, None
    age = _event_age_seconds(session.last_event_at, now)
    if age is None:
        return None, None
    return session, age


def _selector_health_path(state_home: Path) -> Path:
    return Path(state_home) / "behavior" / "selector-health.json"


def _read_selector_health(state_home: Path) -> dict[str, Any]:
    path = _selector_health_path(state_home)
    default = {
        "schema_version": 1,
        "selector_mode": "strict",
        "last_successful_attribution": None,
        "ambiguity_count": 0,
        "service_heartbeat": None,
    }
    if not path.is_file():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid selector health at {path}: {error}") from error
    if not isinstance(value, Mapping) or set(value) != set(default):
        raise ValueError("selector health fields do not match contract")
    if value["schema_version"] != 1 or value["selector_mode"] != "strict":
        raise ValueError("unsupported selector health schema or mode")
    if not isinstance(value["ambiguity_count"], int) or isinstance(
        value["ambiguity_count"], bool
    ) or value["ambiguity_count"] < 0:
        raise ValueError("selector health ambiguity_count is invalid")
    for field in ("last_successful_attribution", "service_heartbeat"):
        selected = value[field]
        if (
            selected is not None
            and _event_age_seconds(selected, datetime.now(UTC)) is None
        ):
            raise ValueError(f"selector health {field} is invalid")
    return dict(value)


def _record_selector_health(
    state_home: Path | None,
    result: SessionSelection | None,
    *,
    now: datetime,
) -> None:
    if state_home is None:
        return
    value = _read_selector_health(state_home)
    timestamp = _format_timestamp(now)
    value["service_heartbeat"] = timestamp
    if isinstance(result, SelectedSession):
        value["last_successful_attribution"] = timestamp
    elif isinstance(result, NoAttribution) and result.reason_code in {
        "ambiguous_active_candidates",
        "ambiguous_session_id",
    }:
        value["ambiguity_count"] += 1
    ensure_state_home(state_home)
    write_json_atomic(_selector_health_path(state_home), value)


def _finish_selection(
    result: SessionSelection,
    *,
    state_home: Path | None,
    now: datetime,
) -> SessionSelection:
    _record_selector_health(state_home, result, now=now)
    return result


def select_codex_session(
    *,
    trace_root: Path | None = None,
    session_id: str | None = None,
    cwd: Path | None = None,
    state_home: Path | None = None,
    now: datetime | None = None,
) -> SessionSelection:
    clock = _selection_clock(now)
    root = (trace_root or codex_trace_root()).expanduser().resolve()
    if not root.is_dir():
        return _finish_selection(
            NoAttribution(
                kind="no_attribution",
                reason_code="trace_root_unavailable",
                provenance=_provenance(
                    "explicit_session_id"
                    if session_id is not None
                    else "unique_active_exact_cwd",
                    candidate_count=0,
                ),
            ),
            state_home=state_home,
            now=clock,
        )
    requested_cwd = (cwd or Path.cwd()).expanduser().resolve(strict=False)
    requested_key = normalized_cwd_key(requested_cwd)
    paths = _available_trace_paths(root)

    if session_id is not None:
        matched_paths: list[Path] = []
        for path in paths:
            found_id, _ = _peek_session(path)
            if found_id == session_id:
                matched_paths.append(path)
        if not matched_paths:
            return _finish_selection(
                NoAttribution(
                    kind="no_attribution",
                    reason_code="session_not_found",
                    provenance=_provenance(
                        "explicit_session_id", candidate_count=0
                    ),
                ),
                state_home=state_home,
                now=clock,
            )

        eligible: list[tuple[SessionTrace, int]] = []
        saw_mismatch = False
        saw_stale = False
        saw_unparsable = False
        for path in matched_paths:
            parsed, age = _parse_fresh_session(path, clock)
            if parsed is None or age is None:
                saw_unparsable = True
                continue
            if cwd is not None and normalized_cwd_key(parsed.cwd) != requested_key:
                saw_mismatch = True
                continue
            if age > _MAX_SESSION_AGE_SECONDS:
                saw_stale = True
                continue
            eligible.append((parsed, age))
        if len(eligible) == 1:
            parsed, age = eligible[0]
            return _finish_selection(
                SelectedSession(
                    kind="selected",
                    session=parsed,
                    provenance=_provenance(
                        "explicit_session_id",
                        candidate_count=1,
                        last_event_age_seconds=age,
                    ),
                ),
                state_home=state_home,
                now=clock,
            )
        reason = (
            "ambiguous_session_id"
            if len(eligible) > 1
            else "cwd_mismatch"
            if saw_mismatch
            else "stale_trace"
            if saw_stale
            else "unparsable_trace"
            if saw_unparsable
            else "session_not_found"
        )
        return _finish_selection(
            NoAttribution(
                kind="no_attribution",
                reason_code=reason,
                provenance=_provenance(
                    "explicit_session_id", candidate_count=len(eligible)
                ),
            ),
            state_home=state_home,
            now=clock,
        )

    exact_paths: list[Path] = []
    for path in paths:
        found_id, found_cwd = _peek_session(path)
        if (
            found_id is not None
            and found_cwd is not None
            and normalized_cwd_key(found_cwd) == requested_key
        ):
            exact_paths.append(path)
    if not exact_paths:
        return _finish_selection(
            NoAttribution(
                kind="no_attribution",
                reason_code="no_exact_cwd_candidate",
                provenance=_provenance(
                    "unique_active_exact_cwd", candidate_count=0
                ),
            ),
            state_home=state_home,
            now=clock,
        )

    active: list[tuple[SessionTrace, int]] = []
    saw_stale = False
    saw_unparsable = False
    saw_completed = False
    for path in exact_paths:
        parsed, age = _parse_fresh_session(path, clock)
        if parsed is None or age is None:
            saw_unparsable = True
            continue
        if age > _MAX_SESSION_AGE_SECONDS:
            saw_stale = True
            continue
        if parsed.active:
            active.append((parsed, age))
        else:
            saw_completed = True
    if len(active) == 1:
        parsed, age = active[0]
        return _finish_selection(
            SelectedSession(
                kind="selected",
                session=parsed,
                provenance=_provenance(
                    "unique_active_exact_cwd",
                    candidate_count=1,
                    last_event_age_seconds=age,
                ),
            ),
            state_home=state_home,
            now=clock,
        )
    reason = (
        "ambiguous_active_candidates"
        if len(active) > 1
        else "no_active_candidate"
        if saw_completed
        else "stale_trace"
        if saw_stale
        else "unparsable_trace"
        if saw_unparsable
        else "no_active_candidate"
    )
    return _finish_selection(
        NoAttribution(
            kind="no_attribution",
            reason_code=reason,
            provenance=_provenance(
                "unique_active_exact_cwd", candidate_count=len(active)
            ),
        ),
        state_home=state_home,
        now=clock,
    )


def validate_session_binding(
    trace_path: Path,
    *,
    session_id: str,
    cwd: Path,
    state_home: Path | None = None,
    now: datetime | None = None,
) -> SessionSelection:
    clock = _selection_clock(now)
    provenance = _provenance("exact_session_binding", candidate_count=0)
    selected_path = Path(trace_path).expanduser().resolve(strict=False)
    if not selected_path.is_file():
        return _finish_selection(
            NoAttribution("no_attribution", "session_not_found", provenance),
            state_home=state_home,
            now=clock,
        )
    parsed, age = _parse_fresh_session(selected_path, clock)
    if parsed is None or age is None:
        return _finish_selection(
            NoAttribution("no_attribution", "unparsable_trace", provenance),
            state_home=state_home,
            now=clock,
        )
    if parsed.session_id != session_id:
        return _finish_selection(
            NoAttribution("no_attribution", "session_not_found", provenance),
            state_home=state_home,
            now=clock,
        )
    if normalized_cwd_key(parsed.cwd) != normalized_cwd_key(cwd):
        return _finish_selection(
            NoAttribution("no_attribution", "cwd_mismatch", provenance),
            state_home=state_home,
            now=clock,
        )
    if age > _MAX_SESSION_AGE_SECONDS:
        return _finish_selection(
            NoAttribution("no_attribution", "stale_trace", provenance),
            state_home=state_home,
            now=clock,
        )
    return _finish_selection(
        SelectedSession(
            "selected",
            parsed,
            _provenance(
                "exact_session_binding",
                candidate_count=1,
                last_event_age_seconds=age,
            ),
        ),
        state_home=state_home,
        now=clock,
    )


def _effective_codex_surfaces(cwd: Path) -> tuple[dict[str, Any], ...]:
    inventory = build_inventory(
        [cwd],
        home=Path.home(),
        working_directories=[cwd],
    )
    selected_ids: set[str] = set()
    for stack in inventory.effective_stacks:
        if stack.get("provider") == "codex" and normalized_cwd_key(
            Path(str(stack.get("working_directory")))
        ) == normalized_cwd_key(cwd):
            selected_ids.update(str(item) for item in stack.get("surface_ids", ()))
    return tuple(
        {
            "surface_id": surface.id,
            "path": surface.path,
            "kind": surface.kind,
            "authority": surface.authority,
            "scope": surface.scope,
            "precedence": surface.precedence,
            "content_sha256": surface.content_sha256,
            "sensitive": surface.sensitive,
        }
        for surface in inventory.surfaces
        if surface.id in selected_ids
    )


def _build_revision() -> str:
    try:
        digest = sha256_bytes(Path(__file__).read_bytes())
    except OSError:
        return "unavailable"
    return f"sha256:{digest}"


def watcher_status(state_home: Path) -> dict[str, Any]:
    path = Path(state_home) / "behavior" / "watchers.json"
    enabled = True
    updated_at = None
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid watcher state at {path}: {error}") from error
        if value.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("unsupported watcher state schema_version")
        entry = value.get("watchers", {}).get(WATCHER_ID, {})
        if not isinstance(entry.get("enabled"), bool):
            raise ValueError("watcher enabled state must be boolean")
        enabled = entry["enabled"]
        updated_at = entry.get("updated_at")
    health = _read_selector_health(state_home)
    return {
        "watcher": WATCHER_ID,
        "enabled": enabled,
        "updated_at": updated_at,
        "provider": "codex",
        "background_service": False,
        "selector_mode": health["selector_mode"],
        "last_successful_attribution": health["last_successful_attribution"],
        "ambiguity_count": health["ambiguity_count"],
        "service_heartbeat": health["service_heartbeat"],
        "package_version": __version__,
        "build_revision": _build_revision(),
    }


def configure_watcher(
    state_home: Path,
    *,
    enabled: bool,
    updated_at: str | None = None,
) -> dict[str, Any]:
    timestamp = updated_at or _timestamp()
    try:
        health_clock = _selection_clock(datetime.fromisoformat(timestamp))
    except ValueError as error:
        raise ValueError("watcher updated_at must be an RFC3339 timestamp") from error
    value = {
        "schema_version": _SCHEMA_VERSION,
        "watchers": {WATCHER_ID: {"enabled": enabled, "updated_at": timestamp}},
    }
    ensure_state_home(state_home)
    write_json_atomic(Path(state_home) / "behavior" / "watchers.json", value)
    _record_selector_health(
        state_home,
        None,
        now=health_clock,
    )
    return watcher_status(state_home)


def mark_incident(
    state_home: Path,
    description: str,
    *,
    trace_root: Path | None = None,
    session_id: str | None = None,
    cwd: Path | None = None,
    recorded_at: str | None = None,
    evidence_schema_version: int = EVIDENCE_SCHEMA_VERSION,
) -> tuple[Path, dict[str, Any]]:
    note = description.strip()
    if not note:
        raise ValueError("incident description is required")
    if len(note) > _MAX_DESCRIPTION_CHARS:
        raise ValueError("incident description is too long")
    if find_secret_spans(note):
        raise ValueError("incident description contains credential-shaped material")
    if not watcher_status(state_home)["enabled"]:
        raise ValueError(f"watcher is disabled: {WATCHER_ID}")

    selection = require_selected_session(
        select_codex_session(
            trace_root=trace_root,
            session_id=session_id,
            cwd=cwd,
            state_home=state_home,
        ),
        operation="mark incident",
    )
    session = selection.session
    evidence_path, normalized_events, source_boundary = ingest_codex_trace(
        state_home,
        session.path,
        attribution=selection.provenance.to_dict(),
        schema_version=evidence_schema_version,
    )
    evidence_reconciliation = reconcile_evidence(normalized_events)
    timestamp = recorded_at or _timestamp()
    incident_id = f"incident-{uuid4().hex}"
    surfaces = _effective_codex_surfaces(session.cwd)
    description_signals = _signals(note)
    barriers = _barriers(note)
    events = session.evidence_events
    line_start = events[0]["line"] if events else None
    line_end = events[-1]["line"] if events else None
    evidence_refs = [
        item["event_id"]
        for item in normalized_events
        if line_start is not None
        and line_end is not None
        and item["source"]["line"] is not None
        and line_start <= item["source"]["line"] <= line_end
    ]
    evidence_signals = set(evidence_reconciliation["reason_codes"]) & {
        "repeated-identical-tool-failure",
        "repeated-permission-denial",
    }
    artifact = {
        "schema_version": _SCHEMA_VERSION,
        "incident_id": incident_id,
        "watcher": WATCHER_ID,
        "recorded_at": timestamp,
        "description": note,
        "description_sha256": sha256_bytes(note.encode("utf-8")),
        "claim": {
            "source": "operator-attestation",
            "verification_status": "asserted",
        },
        "attribution": {
            "kind": selection.kind,
            **selection.provenance.to_dict(),
        },
        "session": {
            "session_id": session.session_id,
            "trace_path": str(session.path),
            "trace_sha256": session.trace_sha256,
            "cwd": str(session.cwd),
            "source": session.source,
            "originator": session.originator,
            "cli_version": session.cli_version,
            "active": session.active,
            "non_interactive": session.non_interactive,
            "last_event_at": session.last_event_at,
        },
        "nearby_evidence": {
            "line_start": line_start,
            "line_end": line_end,
            "record_count": len(events),
            "event_counts": dict(session.event_counts),
            "tool_calls": dict(session.tool_calls),
            "task_started_count": session.task_started_count,
            "task_completed_count": session.task_completed_count,
            "invalid_record_count": session.invalid_record_count,
            "events": [dict(item) for item in events],
        },
        "observed_signals": sorted(
            set(description_signals) | set(session.signal_codes) | evidence_signals
        ),
        "possible_barriers": list(barriers),
        "runtime_context": {
            "settings": dict(session.settings),
            "base_instructions_sha256": session.base_instructions_sha256,
            "dynamic_tools_sha256": session.dynamic_tools_sha256,
        },
        "surface_refs": [dict(item) for item in surfaces],
        "evidence_plane": {
            "schema_version": source_boundary["schema_version"],
            "provider": "codex",
            "session_id": session.session_id,
            "verification_status": "observed",
            "event_refs": evidence_refs,
            "evidence_path": str(evidence_path) if evidence_path is not None else None,
            "source_boundary": {
                "snapshot_bytes": source_boundary["snapshot_bytes"],
                "snapshot_sha256": source_boundary["snapshot_sha256"],
            },
            "reconciliation": evidence_reconciliation,
        },
        "privacy": (
            "Nearby message bodies, reasoning, tool input/output, and environment "
            "content are not persisted; only hashes, types, counts, safe labels, "
            "result metadata, Git object IDs, and detector codes are stored."
        ),
    }
    ensure_state_home(state_home)
    path = Path(state_home) / "behavior" / "incidents" / f"{incident_id}.json"
    write_json_atomic(path, artifact)
    write_json_atomic(
        Path(state_home) / "behavior" / "latest-incident.json",
        {"schema_version": _SCHEMA_VERSION, "incident_id": incident_id},
    )
    return path, artifact


def _load_leaf(state_home: Path, kind: str, artifact_id: str | None) -> dict[str, Any]:
    root = Path(state_home) / "behavior"
    singular = {"diagnoses": "diagnosis"}.get(kind, kind.removesuffix("s"))
    selected_id = artifact_id
    if selected_id is None:
        pointer = root / f"latest-{singular}.json"
        if not pointer.is_file():
            raise ValueError(f"no {singular} has been recorded")
        value = json.loads(pointer.read_text(encoding="utf-8"))
        selected_id = value.get(f"{singular}_id")
    selected_id = _safe_component(str(selected_id), f"{singular}_id")
    path = root / kind / f"{selected_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid {singular} artifact at {path}: {error}") from error
    if not isinstance(value, dict) or value.get(f"{singular}_id") != selected_id:
        raise ValueError(f"invalid {singular} identity at {path}")
    return value


def load_incident(state_home: Path, incident_id: str | None = None) -> dict[str, Any]:
    return _load_leaf(state_home, "incidents", incident_id)


def _surface_sources(incident: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for surface in incident.get("surface_refs", ()):
        if not isinstance(surface, Mapping):
            continue
        path = Path(str(surface.get("path", "")))
        if not path.is_file() or surface.get("sensitive"):
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if sha256_bytes(content) != surface.get("content_sha256"):
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        matched: dict[str, list[int]] = {}
        for line_number, line in enumerate(text.splitlines(), start=1):
            for code, pattern in _SOURCE_PATTERNS:
                if pattern.search(line):
                    matched.setdefault(code, []).append(line_number)
        if not matched:
            continue
        source_codes = sorted(matched)
        precedence = surface.get("precedence")
        precedence_score = precedence if isinstance(precedence, int) else 0
        authority_score = 10 if surface.get("authority") == "repository" else 0
        score = min(
            100,
            25 + 8 * len(source_codes) + 2 * precedence_score + authority_score,
        )
        sources.append(
            {
                "kind": "instruction-surface",
                "path": str(path),
                "surface_id": surface.get("surface_id"),
                "content_sha256": surface.get("content_sha256"),
                "reason_codes": source_codes,
                "line_numbers": sorted(
                    {number for numbers in matched.values() for number in numbers}
                ),
                "score": score,
            }
        )
    return sources


def diagnose_incident(
    state_home: Path,
    *,
    incident_id: str | None = None,
    diagnosed_at: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    incident = load_incident(state_home, incident_id)
    signals = tuple(sorted(set(incident.get("observed_signals", ()))))
    barriers = tuple(sorted(set(incident.get("possible_barriers", ()))))
    sources = _surface_sources(incident)
    evidence = incident.get("nearby_evidence", {})
    settings = incident.get("runtime_context", {}).get("settings", {})
    if "delegation-not-integrated" in signals and evidence.get("tool_calls", {}).get(
        "spawn_agent", 0
    ):
        sources.append(
            {
                "kind": "runtime-behavior",
                "reason_codes": ["delegation-observed"],
                "score": 70,
            }
        )
    if "tool-failure-observed" in signals:
        sources.append(
            {
                "kind": "runtime-behavior",
                "reason_codes": ["tool-failure-without-proven-fallback"],
                "score": 60,
            }
        )
    if "repeated-identical-tool-failure" in signals:
        sources.append(
            {
                "kind": "runtime-behavior",
                "reason_codes": ["repeated-identical-tool-failure"],
                "score": 75,
            }
        )
    if "repeated-permission-denial" in signals:
        sources.append(
            {
                "kind": "harness-setting",
                "setting": "permission-flow",
                "value": "repeated-denial",
                "reason_codes": ["repeated-permission-denial"],
                "score": 70,
            }
        )
    collaboration_mode = settings.get("collaboration_mode")
    if collaboration_mode and "plan" in collaboration_mode.lower():
        sources.append(
            {
                "kind": "harness-setting",
                "setting": "collaboration_mode",
                "value": collaboration_mode,
                "reason_codes": ["plan-mode-question-pressure"],
                "score": 55,
            }
        )
    if not sources:
        sources.append(
            {
                "kind": "model-or-unobserved-runtime-pressure",
                "reason_codes": ["no-matching-active-surface"],
                "score": 20,
            }
        )
    ranked = sorted(
        sources,
        key=lambda item: (-int(item["score"]), canonical_json(item)),
    )
    for rank, source in enumerate(ranked, start=1):
        source["rank"] = rank
    status = (
        "possible-legitimate-barrier"
        if barriers
        else "likely-autonomy-loss"
        if signals
        else "insufficient-evidence"
    )
    diagnosis_id = f"diagnosis-{uuid4().hex}"
    artifact = {
        "schema_version": _SCHEMA_VERSION,
        "diagnosis_id": diagnosis_id,
        "incident_id": incident["incident_id"],
        "watcher": WATCHER_ID,
        "diagnosed_at": diagnosed_at or _timestamp(),
        "status": status,
        "observed_signals": list(signals),
        "possible_barriers": list(barriers),
        "likely_sources": ranked,
        "evaluation": {
            "method": "deterministic",
            "semantic_evaluator_used": False,
            "verification_status": "observed",
            "reason": "deterministic signals were sufficient"
            if signals
            else "no signal matched",
        },
        "recommended_intervention": {
            "type": "resume-instruction",
            "template_id": "primary-agent-autonomy-resume-v1",
            "prompt_sha256": sha256_bytes(_RESUME_INSTRUCTION.encode("utf-8")),
            "durable_policy_mutation": False,
        },
    }
    ensure_state_home(state_home)
    path = Path(state_home) / "behavior" / "diagnoses" / f"{diagnosis_id}.json"
    write_json_atomic(path, artifact)
    write_json_atomic(
        Path(state_home) / "behavior" / "latest-diagnosis.json",
        {"schema_version": _SCHEMA_VERSION, "diagnosis_id": diagnosis_id},
    )
    return path, artifact


def load_diagnosis(state_home: Path, diagnosis_id: str | None = None) -> dict[str, Any]:
    return _load_leaf(state_home, "diagnoses", diagnosis_id)


def _completed_without_pressure(stdout: str) -> tuple[bool, tuple[str, ...]]:
    completed = False
    signals: set[str] = set()
    for line in stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, Mapping):
            continue
        if record.get("type") in {"turn.completed", "thread.completed"}:
            completed = True
        item = record.get("item")
        if isinstance(item, Mapping) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                signals.update(_signals(text, agent_message=True))
    return completed and not signals, tuple(sorted(signals))


def intervene(
    state_home: Path,
    *,
    diagnosis_id: str | None = None,
    dry_run: bool = False,
    force_execute: bool = False,
    timeout_seconds: int = 900,
    executable: str | None = None,
    created_at: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    if timeout_seconds < 1:
        raise ValueError("intervention timeout must be positive")
    diagnosis = load_diagnosis(state_home, diagnosis_id)
    if diagnosis["status"] == "possible-legitimate-barrier":
        raise ValueError("intervention refused because a legitimate barrier may exist")
    recommendation = diagnosis.get("recommended_intervention")
    if (
        not isinstance(recommendation, Mapping)
        or recommendation.get("durable_policy_mutation") is not False
    ):
        raise ValueError(
            "intervention refused because durable_policy_mutation is not false"
        )
    incident = load_incident(state_home, diagnosis["incident_id"])
    attribution = incident.get("attribution")
    if not isinstance(attribution, Mapping) or attribution.get("kind") != "selected":
        raise ValueError("intervention incident has no selected attribution")
    stored_session = incident["session"]
    session_id = _safe_component(str(stored_session["session_id"]), "session_id")
    stored_cwd = Path(str(stored_session["cwd"])).resolve(strict=False)
    binding = require_selected_session(
        validate_session_binding(
            Path(str(stored_session["trace_path"])),
            session_id=session_id,
            cwd=stored_cwd,
            state_home=state_home,
        ),
        operation="intervene",
    )
    session = binding.session
    cwd = session.cwd
    codex = executable or shutil.which("codex")
    if not codex:
        raise ValueError("Codex CLI is unavailable")
    non_interactive = session.non_interactive
    if non_interactive:
        command = [codex, "exec", "resume", "--json", session_id, _RESUME_INSTRUCTION]
    else:
        command = [codex, "resume", session_id, _RESUME_INSTRUCTION]

    should_execute = not dry_run and (non_interactive or force_execute)
    returncode: int | None = None
    result_signals: tuple[str, ...] = ()
    if should_execute:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=non_interactive,
            text=True,
            timeout=timeout_seconds,
        )
        returncode = completed.returncode
        if returncode != 0:
            status = "failed"
        elif non_interactive:
            finished, result_signals = _completed_without_pressure(completed.stdout)
            status = "completed" if finished else "resumed"
        else:
            status = "resumed"
    else:
        status = "planned" if dry_run else "continuation-ready"

    intervention_id = f"intervention-{uuid4().hex}"
    artifact = {
        "schema_version": _SCHEMA_VERSION,
        "intervention_id": intervention_id,
        "diagnosis_id": diagnosis["diagnosis_id"],
        "incident_id": incident["incident_id"],
        "created_at": created_at or _timestamp(),
        "type": "resume-instruction",
        "status": status,
        "session_id": session_id,
        "command": command,
        "executed": should_execute,
        "returncode": returncode,
        "result_signals": list(result_signals),
        "verification_status": "observed" if should_execute else "unverifiable",
        "prompt_template_id": "primary-agent-autonomy-resume-v1",
        "prompt_sha256": sha256_bytes(_RESUME_INSTRUCTION.encode("utf-8")),
        "durable_policy_mutation": False,
    }
    ensure_state_home(state_home)
    path = Path(state_home) / "behavior" / "interventions" / f"{intervention_id}.json"
    write_json_atomic(path, artifact)
    write_json_atomic(
        Path(state_home) / "behavior" / "latest-intervention.json",
        {"schema_version": _SCHEMA_VERSION, "intervention_id": intervention_id},
    )
    return path, artifact


def intervention_prompt() -> str:
    return _RESUME_INSTRUCTION


def record_intervention_result(
    state_home: Path,
    result: str,
    *,
    intervention_id: str | None = None,
    recorded_at: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    if result not in {"completed", "blocked", "failed"}:
        raise ValueError("intervention result must be completed, blocked, or failed")
    intervention = _load_leaf(state_home, "interventions", intervention_id)
    result_id = f"intervention-result-{uuid4().hex}"
    artifact = {
        "schema_version": _SCHEMA_VERSION,
        "intervention_result_id": result_id,
        "intervention_id": intervention["intervention_id"],
        "diagnosis_id": intervention["diagnosis_id"],
        "incident_id": intervention["incident_id"],
        "recorded_at": recorded_at or _timestamp(),
        "result": result,
        "source": "operator-attestation",
        "verification_status": "asserted",
        "durable_policy_mutation": False,
    }
    path = Path(state_home) / "behavior" / "intervention-results" / f"{result_id}.json"
    write_json_atomic(path, artifact)
    write_json_atomic(
        Path(state_home) / "behavior" / "latest-intervention-result.json",
        {"schema_version": _SCHEMA_VERSION, "intervention_result_id": result_id},
    )
    return path, artifact
