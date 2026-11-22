"""Classify completed native turns locally. Only references and decision codes leave the parser."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from .behavior_watch import _claude_text, _message_text, normalized_cwd_key
from .codex_results import codex_result_status

# This pilot recognizes ordinary test, build and lint commands with literal arguments.
_COMMAND = re.compile(
    r"(?:(?:python(?:3(?:\.\d+)?)?|py(?: -3(?:\.\d+)?)?) -m (?:pytest|unittest)|"
    r"pytest|ruff check|(?:npm|pnpm|yarn) (?:test|build|run (?:build|test))|"
    r"cargo (?:test|check|build)|go (?:test|build)|dotnet (?:test|build))"
    r"(?: [\w./:=+-]+)*",
    re.IGNORECASE,
)
_ASK = re.compile(
    r"^(?:[-*] |\d+\. )?(?:(?:please|now) |(?:can|could|would) you (?:please )?|"
    r"you (?:can|should|need to) )?(?:run|execute|rerun|re-run)\s+",
    re.IGNORECASE,
)
_IMPERATIVE = re.compile(
    r"(?:^(?:[-*] |\d+\. )?(?:(?:can|could|would) you |I (?:want|need) you to )?|"
    r"\band (?:then )?)(?:please )?(run|execute|rerun|test|verify|validate|check|build)\b",
    re.IGNORECASE,
)
_INSTRUCTION_REQUEST = re.compile(
    r"\b(?:how (?:to|do|can|should)|walk me through|explain|example|sample command|"
    r"what command|which command|show me|instructions for|write (?:a |the )?(?:guide|instructions))\b",
    re.IGNORECASE,
)
_STOP = re.compile(r"\b(?:stop|pause|wait|ask (?:me|first))\b", re.IGNORECASE)
_DIRTY = re.compile(
    r"\b(?:dirty|uncommitted|untracked|already modified)\b", re.IGNORECASE
)
_CLEAN = re.compile(
    r"\b(?:(?:board|files?|tree|workspace|repository) (?:is|are|was|were) clean|"
    r"no (?:uncommitted |untracked )?(?:changes|edits)|(?:is|was) not dirty)\b",
    re.IGNORECASE,
)
_PROHIBITION = re.compile(
    r"\b(?:do not|don't|never) (?:run|execute|proceed|change|edit)\b", re.IGNORECASE
)
_HUMAN = re.compile(
    r"\b(?:physically|physical (?:observation|device|button|hardware)|hardware button|unplug|plug in|"
    r"(?:you|please) (?:must |need to )?(?:sign in|log in)|(?:missing|your|requires?) credentials?|"
    r"credentials? (?:are |is )?(?:required|missing|unavailable)|"
    r"captcha|permission denied|access denied|not authorized|on your phone|human observation|"
    r"manually (?:observe|inspect)|(?:cannot|can't|couldn't|unable to) (?:access|authenticate|connect)|"
    r"(?:requires?|needs?) (?:explicit |your |user )?(?:approval|permission)|"
    r"(?:is|would be) (?:destructive|irreversible))\b",
    re.IGNORECASE,
)
_UNCERTAIN = re.compile(
    r"\b(?:if (?:you|your|it)|on (?:your|the remote)|in (?:your|the remote)|"
    r"(?:can't|cannot|couldn't|unable to) (?:run|execute|verify)|not installed|command not found|"
    r"missing dependenc(?:y|ies))\b",
    re.IGNORECASE,
)
_SCOPE_CHANGE = re.compile(
    r"(?:^|[\s;&|()])(?:cd|chdir|pushd|popd|set-location|push-location|ssh|sudo)\b|"
    r"(?:^|\s)(?:-C|--(?:cwd|directory|prefix|work-tree|git-dir|manifest-path))(?:\s|=)"
)
_EXTERNAL_ARG = re.compile(
    r"(?:^|[=\s:])(?:/|[A-Za-z]:[/\\])|(?:^|[=/\\\s])\.\.(?:[/\\\s]|$)"
)
_WORKSPACE_READ = re.compile(
    r"(?:git (?:status|rev-parse|diff|ls-files)|pwd|ls|dir|"
    r"get-childitem|get-location|rg --files)(?: [\w./=+-]+)*",
    re.IGNORECASE,
)
_QUOTE_INTRO = re.compile(
    r"\b(?:example|quotation|instructions?|documentation|(?:user|guide|README) says)"
    r"[^.!?]{0,40}:\s*$",
    re.IGNORECASE,
)
_KEEP_GOING = re.compile(r"\b(?:not|never|don't) (?:stop|wait|pause)\b", re.IGNORECASE)
# A soak: a long unattended run held as a gate (a soak, a burn-in run, an N-hour run).
_SOAK_RUN = (
    r"(?:soak\w*|burn[- ]in (?:runs?|tests?|periods?|windows?)|"
    r"(?:\d+|one|two|three|twenty-four|forty-eight)[- ]?(?:hours?|hrs?|h|days?|nights?)"
    r"[- ](?:runs?|soaks?|tests?|windows?|periods?|clocks?|burn[- ]in)|"
    r"overnight (?:runs?|tests?|windows?)|stability (?:runs?|tests?|windows?|periods?))"
)
_SOAK = re.compile(
    rf"\b(?:{_SOAK_RUN}|(?:monitor|watch|observe)\w* (?:it |this |that )?for "
    r"(?:a |one |another |\d+ )?(?:days?|nights?|weeks?|hours?)|"
    r"run(?:s|ning)? (?:overnight|for (?:a |one |another |\d+ )?(?:days?|nights?|hours?)))\b",
    re.IGNORECASE,
)
# The sentence says there is no soak, it was stopped, or it is not a gate.
_NOT_A_GATE = re.compile(
    r"\b(?:no|without|skip\w*|drop\w*|cancel\w*|stop\w*)"
    rf"(?: (?:the|a|any|this|that|its))?(?: \S+)? {_SOAK_RUN}|"
    r"\b(?:not|never|\w+n[’']t) (?:doing|running|need\w*|using|holding|waiting|"
    rf"gating|blocking)\b[^.!?]{{0,24}}?{_SOAK_RUN}|"
    rf"{_SOAK_RUN}(?: \w+)? (?:(?:is|was|are|were) not|(?:isn|wasn|aren|weren)[’']t)\b",
    re.IGNORECASE,
)
_NOT = r"(?<!not )(?<!n't )(?<!n’t )(?<!never )"
# Work held back, deferred or put to the operator because of that run.
_HOLD = re.compile(
    r"\b(?:(?:not|never|\w+n[’']t|(?:have|has|did|will) not|not going to)"
    r"(?: yet| been| be)* (?:deploy|push|merg|land|releas|ship|roll)\w*|"
    rf"{_NOT}(?:hold|holds|holding|held)(?: (?:off|back))?(?: on)? (?:the |this |that |my |it )?"
    r"(?:off|back|it|deploy\w*|push\w*|merg\w*|releas\w*|landing|rollout|ship\w*)|"
    rf"on hold|{_NOT}(?:defer|postpon)\w* (?:the |this |that |my )?"
    r"(?:deploy\w*|push\w*|merg\w*|releas\w*|landing|rollout)|"
    rf"{_NOT}wait(?:s|ed|ing)? (?:for|until|on|out)|"
    rf"(?:until|once|after|when) [^.!?]{{0,40}}?{_SOAK_RUN}[^.!?]{{0,20}}?"
    r"(?:finishes|completes|passes|ends|clears|is (?:done|clean|over|complete)|"
    r"has (?:finished|completed|passed|ended))|"
    r"(?:restart|reset|interrupt|invalidate|break|void|spoil)s? (?:the |that |this |its |a )?"
    r"[^.!?]{0,30}?(?:soak|burn[- ]in|run|clock|window)s?\b|"
    r"let (?:it|this|that) (?:soak|bake|run)|"
    r"(?:before|prior to) (?:deploying|pushing|merging|landing|releasing|shipping|"
    r"rolling out|the (?:deploy|merge|release|push))|"
    r"(?:want me to|should I|shall I)(?: go ahead and)? "
    r"(?:deploy|push|merge|land|release|ship|roll))",
    re.IGNORECASE,
)
_NO_SHIP = re.compile(
    r"\b(?:(?:do not|don[’']t|never|no) (?:deploy|push|merg|land|releas|ship)\w*|"
    r"hold (?:off|back))\b",
    re.IGNORECASE,
)
# A soak can suit a shipped product; the review is about products still being built.
_SHIPPED = re.compile(
    r"\b(?:in production|to production|production (?:traffic|users|release|rollout|"
    r"environment|servers?|deploy\w*)|customers?|end users|paying users|"
    r"general availability)\b",
    re.IGNORECASE,
)
_ID = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}")
_SHELL_TOOLS = {
    "bash",
    "shell",
    "shell_command",
    "exec_command",
    "functions.exec_command",
}


def _sentences(text):
    fenced = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("~~~", "```")):
            fenced = not fenced
        elif not fenced and not line.startswith((">", '"', "'", "“")):
            yield from re.split(r"(?<=[.!?])\s+", line)


def _own_sentences(text):
    """The agent's own sentences: no fences, quotes or the line after a quote intro."""
    quoted_next = False
    for sentence in _sentences(text):
        if quoted_next:
            quoted_next = False
            continue
        if _QUOTE_INTRO.search(sentence):
            quoted_next = True
            continue
        yield sentence


def _requested(text):
    for sentence in _own_sentences(text):
        ask = _ASK.match(sentence)
        if ask is None:
            continue
        tail = sentence[ask.end() :]
        if tail.startswith("`"):
            command, end, _ = tail[1:].partition("`")
            if not end:
                continue
        else:
            command = re.split(
                r"[,;]|\s+(?:and (?:tell|send|share|paste|report)|then)\b",
                tail,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].rstrip(".?!")
        command = command.strip()
        if len(command) <= 240 and _COMMAND.fullmatch(command):
            return command
    return None


def _held(text):
    """The sentences holding work for a soak, with the sentence next to it that holds."""
    sentences = list(_own_sentences(text))
    for index, sentence in enumerate(sentences):
        if not _SOAK.search(sentence) or _NOT_A_GATE.search(sentence):
            continue
        nearby = range(max(0, index - 1), min(len(sentences), index + 2))
        held = [i for i in nearby if i == index or _HOLD.search(sentences[i])]
        if any(_HOLD.search(sentences[i]) for i in held):
            words = " ".join(sentences[i] for i in held)
            return words if len(words) <= 240 else words[:239].rstrip() + "…"
    return None


def _final_answer(provider, verified_raw):
    try:
        row = json.loads(verified_raw)
    except (UnicodeError, ValueError):
        return None
    if not isinstance(row, dict):
        return None
    if provider == "codex":
        data = row.get("payload")
        if (
            row.get("type") != "event_msg"
            or not isinstance(data, dict)
            or data.get("type") != "task_complete"
        ):
            return None
        answer = data.get("last_agent_message")
    elif provider == "claude-code":
        msg = row.get("message")
        if (
            row.get("type") != "assistant"
            or not isinstance(msg, dict)
            or msg.get("role") != "assistant"
            or msg.get("stop_reason") != "end_turn"
        ):
            return None
        answer = _claude_text(row)
    else:
        return None
    return answer if isinstance(answer, str) else None


def review_command(provider: str, verified_raw: bytes) -> str | None:
    """Extract the check on explicit local lookup, after the caller verifies these bytes."""
    answer = _final_answer(provider, verified_raw)
    return None if answer is None else _requested(answer)


def review_hold(provider: str, verified_raw: bytes) -> str | None:
    """Extract the soak hold on explicit local lookup, after the caller verifies these bytes."""
    answer = _final_answer(provider, verified_raw)
    return None if answer is None else _held(answer)


def _authorized(request, command):
    if _INSTRUCTION_REQUEST.search(request):
        return False
    explicit = _requested(request)
    if explicit is not None:
        return _check_key(explicit) == _check_key(command)
    qualifiers = re.findall(
        r"\b(?:integration|unit|staging|production|e2e|end-to-end)\b",
        request,
        re.IGNORECASE,
    )
    if any(word.lower() not in command.lower() for word in qualifiers):
        return False
    if re.search(r"\b(?:pytest|unittest|test)\b", command, re.IGNORECASE):
        category = r"\b(?:tests?|testing|suites?|pytest|unittest)\b"
    elif re.search(r"\bbuild\b", command, re.IGNORECASE):
        category = r"\bbuild\b"
    else:
        category = r"\b(?:checks?|lint|ruff)\b"
    for sentence in _sentences(request):
        for directive in _IMPERATIVE.finditer(sentence):
            scope = sentence[directive.start() :]
            if re.search(category, scope, re.IGNORECASE) or re.match(
                r"(?:please )?(?:verify|validate|test|check) (?:the |this |these |my )?"
                r"(?:implementation|changes?|fix|project)\b",
                scope,
                re.IGNORECASE,
            ):
                return True
    return False


def _check_key(command):
    normalized = re.sub(
        r"^(?:python(?:3(?:\.\d+)?)?|py(?: -3(?:\.\d+)?)?) -m pytest\b",
        "pytest",
        command.lower().strip(),
    )
    return " ".join(
        token for token in normalized.split() if token not in {"-q", "--quiet"}
    )


def _identifier(value):
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


@dataclass
class Turn:
    turn_id: str | None = None
    user: str = ""
    scope: bool = False
    restricted: bool = False
    shell_ok: bool = False
    # Any shell command that succeeded here, wherever it pointed: the agent could act.
    ran_shell: bool = False
    board_dirty: bool | None = None
    final: str = ""
    requests: dict[str, str] = field(default_factory=dict)
    calls: set[str] = field(default_factory=set)
    attempted: set[str] = field(default_factory=set)
    issued: set[str] = field(default_factory=set)

    def shell(self, command):
        if not self.scope or not self.user or not isinstance(command, str):
            return False
        if (
            _SCOPE_CHANGE.search(command)
            or _SCOPE_CHANGE.search(command.lower())
            or _EXTERNAL_ARG.search(command)
        ):
            return False
        for part in re.split(r"(?:&&|\|\||;|\r?\n)", command):
            if _COMMAND.fullmatch(part.strip()):
                self.attempted.add(_check_key(part))
        return bool(
            _COMMAND.fullmatch(command.strip())
            or _WORKSPACE_READ.fullmatch(command.strip())
        )

    def outcome(self, command, success, stdout):
        if success is None:
            return
        self.shell_ok |= success
        if _COMMAND.fullmatch(command.strip()):
            self.issued.add(_check_key(command))
        if (
            success
            and isinstance(stdout, str)
            and len(stdout) <= 64 * 1024
            and command.strip() in ("git status --porcelain", "git status --short")
        ):
            if not stdout.strip():
                self.board_dirty = False
            elif re.search(
                r"^[ MADRCU?!]{2} \.dias/(?:chips\.json|status\.json)?\s*$",
                stdout,
                re.MULTILINE,
            ):
                self.board_dirty = True


def _shell_request(turn, call_id, name, args, matches_cwd):
    turn.board_dirty = None
    if (
        not isinstance(name, str)
        or name.lower() not in _SHELL_TOOLS
        or not _identifier(call_id)
    ):
        return
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return
    if not isinstance(args, dict):
        return
    workdir = args.get("workdir", args.get("cwd"))
    if workdir is not None and not matches_cwd(workdir):
        return
    command = args.get("cmd") or args.get("command")
    if turn.scope and turn.user and isinstance(command, str):
        turn.calls.add(call_id)
    if turn.shell(command):
        turn.requests[call_id] = command


def _shell_result(turn, call_id, success, stdout):
    if not _identifier(call_id):
        return
    if call_id in turn.calls:
        turn.calls.discard(call_id)
        turn.ran_shell |= success is True
    if call_id in turn.requests:
        turn.outcome(turn.requests.pop(call_id), success, stdout)


def _native_command(argv):
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(x, str) for x in argv)
    ):
        return None
    executable = re.split(r"[/\\]", argv[0])[-1].lower().removesuffix(".exe")
    if executable in {"bash", "sh", "zsh", "powershell", "pwsh", "cmd"}:
        if len(argv) >= 3 and argv[-2].lower() in {"-c", "-lc", "-command", "/c"}:
            return argv[-1]
        return None
    return " ".join(argv)


def _codex(row, turn, matches_cwd, session):
    kind, data = row.get("type"), row.get("payload")
    if kind == "session_meta":
        if (
            not isinstance(data, dict)
            or (data.get("id") or data.get("session_id")) != session
            or not matches_cwd(data.get("cwd"))
        ):
            raise ValueError("review source contains another session")
        return Turn(), None
    if not isinstance(data, dict):
        return turn, None
    event = data.get("type")
    turn_id = _identifier(data.get("turn_id"))
    if kind == "event_msg" and event == "task_started":
        return Turn(turn_id=turn_id), None
    if kind == "event_msg" and event == "turn_aborted":
        return Turn(), None
    if kind == "turn_context":
        scope = matches_cwd(data.get("cwd"))
        if not scope or (turn_id and turn_id != turn.turn_id):
            turn = Turn(turn_id=turn_id)
        turn.scope = scope
        policy = data.get("sandbox_policy")
        if isinstance(policy, dict):
            turn.restricted = policy.get("type") not in (
                "workspace-write",
                "danger-full-access",
            )
        return turn, None

    if kind == "event_msg" and event == "item_completed":
        item = data.get("item")
        if (
            not isinstance(item, dict)
            or data.get("thread_id") != session
            or turn_id != turn.turn_id
            or not turn_id
        ):
            return turn, None
        if item.get("type") == "UserMessage" and isinstance(item.get("content"), list):
            user = "\n".join(
                b["text"]
                for b in item["content"]
                if isinstance(b, dict)
                and b.get("type") == "text"
                and isinstance(b.get("text"), str)
            )
            turn = Turn(
                turn_id=turn.turn_id,
                user=user,
                scope=turn.scope,
                restricted=turn.restricted,
            )
        elif (
            item.get("type") == "CommandExecution"
            and item.get("source") in ("agent", "unified_exec_startup")
            and matches_cwd(item.get("cwd"))
        ):
            turn.board_dirty = None
            command = _native_command(item.get("command"))
            if (
                command
                and turn.scope
                and turn.user
                and item.get("status") == "completed"
                and type(item.get("exit_code")) is int
                and item["exit_code"] == 0
            ):
                turn.ran_shell = True
            if (
                turn.shell(command)
                and type(item.get("exit_code")) is int
                and item.get("status") in ("completed", "failed")
            ):
                turn.outcome(
                    command,
                    item["status"] == "completed" and item["exit_code"] == 0,
                    item.get("aggregated_output"),
                )
        elif item.get("type") == "FileChange":
            turn.board_dirty = None
    elif (
        kind == "event_msg"
        and event == "user_message"
        and isinstance(data.get("message"), str)
    ):
        turn = Turn(
            turn_id=turn.turn_id,
            user=data["message"],
            scope=turn.scope,
            restricted=turn.restricted,
        )
    elif kind == "response_item" and event == "function_call":
        _shell_request(
            turn,
            data.get("call_id"),
            data.get("name"),
            data.get("arguments"),
            matches_cwd,
        )
    elif kind == "response_item" and event == "function_call_output":
        output = data.get("output")
        status, code = codex_result_status(output)
        # Legacy wrappers put the exit code on their first line, before stdout.
        if (
            isinstance(output, str)
            and not output.lstrip().startswith(("{", "["))
            and not re.match(
                r"\A\s*Exit code:\s*-?\d+[ \t]*(?:$|\r?\n)", output, re.IGNORECASE
            )
        ):
            code = None
        stdout = None
        if isinstance(output, str) and len(output) <= 64 * 1024:
            _, header, body = output.replace("\r\n", "\n").partition("\nOutput:\n")
            if header:
                stdout = body
        _shell_result(
            turn,
            data.get("call_id"),
            None if code is None else status == "completed" and code == 0,
            stdout,
        )
    elif kind == "response_item" and event == "custom_tool_call":
        turn.board_dirty = None
    elif data.get("phase") == "final_answer":
        turn.final = _message_text(row) or turn.final
    if kind == "event_msg" and event == "task_complete":
        if not turn.turn_id or turn_id != turn.turn_id:
            return Turn(), None
        answer = data.get("last_agent_message")
        if answer is None:
            answer = turn.final
        return turn, answer if isinstance(answer, str) else ""
    return turn, None


def _claude_prompt(row):
    if (
        row.get("type") != "user"
        or row.get("isMeta")
        or row.get("isCompactSummary")
        or row.get("isVisibleInTranscriptOnly")
    ):
        return False
    msg = row.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return False
    blocks = msg.get("content")
    if isinstance(blocks, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks
    ):
        return False
    text = _claude_text(row)
    return bool(text) and not text.lstrip().startswith(
        (
            "<local-command-",
            "<command-",
            "<system-reminder>",
            "<task-notification>",
            "<bash-",
        )
    )


def _claude(row, turn, matches_cwd, session):
    if row.get("sessionId") != session or row.get("isSidechain"):
        return turn, None
    if row.get("isCompactSummary") or row.get("subtype") == "compact_boundary":
        return Turn(), None
    if "cwd" in row and not matches_cwd(row["cwd"]):
        return Turn(), None
    msg = row.get("message")
    role = row.get("type")
    if role in ("assistant", "user") and not matches_cwd(row.get("cwd")):
        return Turn(), None
    uuid = _identifier(row.get("uuid"))
    if not isinstance(msg, dict) or role != msg.get("role"):
        return turn, None
    content = msg.get("content")
    blocks = content if isinstance(content, list) else []
    text = _claude_text(row) or ""
    if _claude_prompt(row):
        turn = Turn(turn_id=uuid, user=text, scope=True)
    if (
        row.get("permissionMode") == "plan"
        or row.get("toolDenialKind") == "permission-rule"
    ):
        turn.restricted = True
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if role == "assistant" and block.get("type") == "tool_use":
            _shell_request(
                turn,
                block.get("id"),
                block.get("name"),
                block.get("input"),
                matches_cwd,
            )
        elif role == "user" and block.get("type") == "tool_result":
            _shell_result(
                turn,
                block.get("tool_use_id"),
                None
                if row.get("toolDenialKind")
                else block.get("is_error", False) is False,
                block.get("content"),
            )
    return turn, text if text and role == "assistant" and msg.get(
        "stop_reason"
    ) == "end_turn" else None


@dataclass(frozen=True, slots=True)
class _Link:
    parent: str | None
    start: int
    end: int
    prompt: bool
    stop: bool


class _ClaudeHistory:
    """Claude can append sibling tool calls and messages. Follow only the completion's ancestors."""

    def __init__(self, prefix):
        self.prefix = prefix
        self.links = {}
        self.last = None

    def observe(self, row, start, end, complete, matches_cwd, session):
        uuid = _identifier(row.get("uuid"))
        if not uuid:
            return Turn(), None
        if uuid in self.links:
            previous = self.links[uuid]
            if (
                previous
                and self.prefix[previous.start : previous.end] != self.prefix[start:end]
            ):
                self.links[uuid] = None
            return Turn(), None
        # Older transcripts without parent links contain one ordered conversation.
        parent = _identifier(row.get("parentUuid", self.last))
        link = _Link(
            parent,
            start,
            end,
            _claude_prompt(row),
            complete
            or bool(row.get("isCompactSummary"))
            or row.get("subtype") == "compact_boundary",
        )
        self.links[uuid], self.last = link, uuid
        if not complete:
            return Turn(), None
        chain = [link]
        while not link.prompt:
            link = self.links.get(link.parent)
            if link is None or link.stop or link.start >= chain[-1].start:
                return Turn(), None
            chain.append(link)
        turn = Turn()
        for link in reversed(chain):
            turn, answer = _claude(
                json.loads(self.prefix[link.start : link.end]),
                turn,
                matches_cwd,
                session,
            )
        return turn, answer


def _decision(turn, answer, command):
    request = " ".join(_sentences(turn.user))
    answer = " ".join(_sentences(answer))
    if _PROHIBITION.search(request):
        return "suppressed", "explicit-stop"
    for sentence in _sentences(turn.user):
        if not _STOP.search(sentence) or _KEEP_GOING.search(sentence):
            continue
        if _DIRTY.search(sentence) and re.search(
            r"\bboard\b|\.dias[/\\]", sentence, re.IGNORECASE
        ):
            if turn.board_dirty is True or (
                _DIRTY.search(answer) and not _CLEAN.search(answer)
            ):
                return "suppressed", "explicit-stop"
            if turn.board_dirty is False:
                continue
            return "unknown", "authority-or-capability-uncertain"
        if re.search(r"\b(?:if|when|unless)\b", sentence, re.IGNORECASE) and not (
            _DIRTY.search(sentence) and _DIRTY.search(answer)
        ):
            return "unknown", "authority-or-capability-uncertain"
        return "suppressed", "explicit-stop"
    if _HUMAN.search(request) or _HUMAN.search(answer):
        return "suppressed", "human-or-permission-barrier"
    if _check_key(command) in turn.issued:
        return "suppressed", "check-already-issued"
    if (
        _check_key(command) in turn.attempted
        or turn.restricted
        or not turn.shell_ok
        or not _authorized(turn.user, command)
        or _EXTERNAL_ARG.search(command)
        or _SCOPE_CHANGE.search(command)
        or _UNCERTAIN.search(answer)
    ):
        return "unknown", "authority-or-capability-uncertain"
    return "candidate", "authorized-executable-check"


def _soak_decision(turn, answer):
    """A soak the operator did not ask for, on work the agent could have landed itself."""
    asked = list(_sentences(turn.user))
    request = " ".join(asked)
    answer = " ".join(_sentences(answer))
    if _PROHIBITION.search(request) or _NO_SHIP.search(request):
        return "suppressed", "explicit-stop"
    if any(_SOAK.search(s) and not _NOT_A_GATE.search(s) for s in asked):
        return "suppressed", "operator-requested-soak"
    for sentence in asked:
        if _STOP.search(sentence) and not _KEEP_GOING.search(sentence):
            if re.search(r"\b(?:if|when|unless)\b", sentence, re.IGNORECASE):
                return "unknown", "authority-or-capability-uncertain"
            return "suppressed", "explicit-stop"
    if _HUMAN.search(request) or _HUMAN.search(answer):
        return "suppressed", "human-or-permission-barrier"
    if _SHIPPED.search(request) or _SHIPPED.search(answer):
        return "unknown", "shipped-product-uncertain"
    if turn.restricted or not turn.ran_shell:
        return "unknown", "authority-or-capability-uncertain"
    return "candidate", "unrequested-soak-hold"


def inspect_turns(candidate, events, verification, cutoff, now):
    """Review the same bounded native bytes whose completion records passed replay."""
    path = candidate.source_path
    marker_type = (
        "task.completed" if candidate.provider == "codex" else "turn.completed"
    )
    markers = {
        e["source"]["line"]: e
        for e in events
        if e["event_type"] == marker_type
        and e["source"]["path"] == str(path)
        and verification[e["event_id"]]["status"] == "verified"
    }
    if not markers:
        return [], 0
    source = max((e["source"] for e in events), key=lambda s: s["snapshot_bytes"])
    boundary = source["snapshot_bytes"]
    if boundary > candidate.source_bytes:
        raise ValueError("review source exceeds selected bytes")
    with path.open("rb") as stream:
        prefix = stream.read(boundary)
    if (
        len(prefix) != boundary
        or sha256(prefix).hexdigest() != source["snapshot_sha256"]
    ):
        raise ValueError("review source changed")
    cwd = normalized_cwd_key(candidate.cwd)

    @lru_cache(maxsize=64)
    def absolute_match(value):
        return Path(value).is_absolute() and normalized_cwd_key(value) == cwd

    def matches_cwd(value):
        return isinstance(value, str) and absolute_match(value)

    turn, offset = Turn(), 0
    context_digest = sha256()
    completed, results = set(), []
    claude = _ClaudeHistory(prefix) if candidate.provider == "claude-code" else None
    for line, raw in enumerate(BytesIO(prefix), 1):
        start, offset = offset, offset + len(raw)
        context_digest.update(raw)
        try:
            row = json.loads(raw)
        except (ValueError, UnicodeError):
            turn = Turn()
            if claude:
                claude = _ClaudeHistory(prefix)
            continue
        if not isinstance(row, dict):
            turn = Turn()
            continue
        if claude is None:
            turn, answer = _codex(row, turn, matches_cwd, candidate.session_id)
        else:
            turn, answer = claude.observe(
                row, start, offset, line in markers, matches_cwd, candidate.session_id
            )
        if answer is None:
            continue
        finished, turn = turn, Turn()
        marker = markers.get(line)
        if (
            not answer
            or not finished.user
            or not finished.scope
            or not finished.turn_id
            or finished.turn_id in completed
            or not marker
        ):
            continue
        stamp = marker["observed_at"]
        try:
            observed = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
            if (
                observed is None
                or observed.utcoffset() is None
                or not cutoff <= observed <= now
            ):
                continue
        except ValueError:
            continue
        record = sha256(raw.rstrip(b"\r\n")).hexdigest()
        if record != marker["source"]["record_sha256"]:
            raise ValueError("review record changed")
        completed.add(finished.turn_id)
        found = []
        command = _requested(answer)
        if command is not None:
            found.append(
                ("test-command", "manual-handoff/v1", _decision(finished, answer, command))
            )
        if _held(answer) is not None:
            found.append(("soak-hold", "soak-hold/v1", _soak_decision(finished, answer)))
        for handoff, version, (decision, reason) in found:
            identity = json.dumps(
                [cwd, candidate.provider, candidate.session_id, line, record, version]
            )
            results.append(
                {
                    "ref": "ar-" + sha256(identity.encode()).hexdigest()[:32],
                    "provider": candidate.provider,
                    "session_id": candidate.session_id,
                    "turn": finished.turn_id,
                    "line": line,
                    "source": "verified",
                    "handoff": handoff,
                    "decision": decision,
                    "reason": reason,
                    "source_path": str(path),
                    "offset": start,
                    "length": len(raw),
                    "record_sha256": record,
                    "prefix_bytes": offset,
                    "prefix_sha256": context_digest.hexdigest(),
                    "observed_at": observed.astimezone(UTC).isoformat(),
                }
            )
    return results, len(completed)
