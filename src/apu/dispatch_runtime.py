from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .dispatch import (
    DispatchRejectedError,
    DispatchUnavailableError,
    IsolationProbeRequest,
    IsolationProbeResult,
    RunnerRequest,
)
from .dispatch_process import run_confined

_MAX_RESULT_BYTES = 8 * 1024 * 1024
_MECHANISM = "codex-workspace-write-unelevated-v1"


class CodexDispatchRuntime:
    """A Codex CLI runtime whose shell and capability probe share one policy."""

    def __init__(
        self,
        *,
        executable: str | None = None,
        timeout_seconds: int = 900,
    ) -> None:
        resolved = executable or shutil.which("codex")
        if not resolved:
            raise DispatchUnavailableError("Codex CLI is unavailable")
        if timeout_seconds < 1:
            raise ValueError("dispatch timeout must be positive")
        self.executable = resolved
        self.timeout_seconds = timeout_seconds

    def probe(self, request: IsolationProbeRequest) -> IsolationProbeResult:
        stage_root = request.stage_root.resolve()
        probe_target = request.probe_target.resolve()
        marker = stage_root / ".apu-isolation-probe"
        if os.name == "nt":
            marker_literal = str(marker).replace("'", "''")
            target_literal = str(probe_target).replace("'", "''")
            script = (
                "$ErrorActionPreference='Stop';"
                f"$marker='{marker_literal}';"
                f"$target='{target_literal}';"
                "Set-Content -LiteralPath $marker -Value 'probe';"
                "Remove-Item -LiteralPath $marker -Force;"
                "try {"
                "$handle=[System.IO.File]::Open("
                "$target,[System.IO.FileMode]::Open,"
                "[System.IO.FileAccess]::Write,"
                "[System.IO.FileShare]::ReadWrite);"
                "$handle.Dispose(); exit 41"
                "} catch [System.UnauthorizedAccessException] { exit 0 }"
                "catch [System.Security.SecurityException] { exit 0 }"
            )
            confined_command = [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ]
        else:
            environment = dict(os.environ)
            environment.update(
                {
                    "APU_DISPATCH_PROBE_TARGET": str(probe_target),
                    "APU_DISPATCH_STAGE_MARKER": str(marker),
                }
            )
            script = (
                'set -eu; marker="$APU_DISPATCH_STAGE_MARKER"; '
                'target="$APU_DISPATCH_PROBE_TARGET"; '
                ': > "$marker"; rm -f "$marker"; '
                'if (exec 9>>"$target") 2>/dev/null; then exit 41; else exit 0; fi'
            )
            confined_command = ["sh", "-c", script]
        command = self._sandbox_command(stage_root, confined_command)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                env=environment if os.name != "nt" else None,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DispatchUnavailableError(
                f"Codex isolation probe failed: {type(error).__name__}"
            ) from error
        if marker.exists():
            marker.unlink(missing_ok=True)
            raise DispatchUnavailableError(
                "Codex isolation probe left a stage artifact"
            )
        return IsolationProbeResult(
            context_id=_MECHANISM,
            mechanism=_MECHANISM,
            attempted=completed.returncode in {0, 41},
            write_denied=completed.returncode == 0,
        )

    def _sandbox_command(self, stage_root: Path, command: list[str]) -> list[str]:
        return [
            self.executable,
            "sandbox",
            *(["-c", 'windows.sandbox="unelevated"'] if os.name == "nt" else []),
            "-P",
            ":workspace",
            "-C",
            str(stage_root),
            *command,
        ]

    def run(self, request: RunnerRequest) -> dict[str, Any]:
        if request.isolation_context_id != _MECHANISM:
            raise DispatchUnavailableError(
                "runner isolation context does not match the proven policy"
            )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["edits"],
            "properties": {
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "content"],
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                }
            },
        }
        stage_map = {
            logical: path.relative_to(request.stage_root).as_posix()
            for logical, path in request.staged_files.items()
        }
        prompt = (
            request.work_order
            + "\n\n## Confined staged inputs\n\n"
            + json.dumps(stage_map, sort_keys=True)
            + "\n\nRead only the staged paths above. Do not edit any file. "
            "Return exactly one JSON object matching the supplied schema. "
            "Each `edits` item must contain the absolute logical target path "
            "and the complete proposed UTF-8 file content."
        )
        with tempfile.TemporaryDirectory(
            prefix=".apu-codex-result-",
            dir=request.stage_root.parent,
        ) as result_directory:
            result_root = Path(result_directory)
            schema_path = result_root / "schema.json"
            result_path = result_root / "result.json"
            schema_path.write_text(
                json.dumps(schema, sort_keys=True),
                encoding="utf-8",
            )
            command = [
                self.executable,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--ignore-rules",
                "-s",
                "workspace-write",
                *(["-c", 'windows.sandbox="unelevated"'] if os.name == "nt" else []),
                "-C",
                str(request.stage_root),
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
                "-",
            ]
            try:
                completed = subprocess.run(
                    command,
                    input=prompt.encode("utf-8"),
                    check=False,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise DispatchUnavailableError(
                    f"Codex dispatch failed: {type(error).__name__}"
                ) from error
            if completed.returncode != 0:
                raise DispatchUnavailableError(
                    f"Codex dispatch exited with status {completed.returncode}"
                )
            try:
                if result_path.stat().st_size > _MAX_RESULT_BYTES:
                    raise DispatchRejectedError(
                        "Codex plan candidate exceeds the size limit"
                    )
                value = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise DispatchRejectedError(
                    "Codex did not return a valid plan candidate"
                ) from error
        if not isinstance(value, dict) or set(value) != {"edits"}:
            raise DispatchRejectedError("Codex plan candidate must be a JSON object")
        edits = value["edits"]
        if not isinstance(edits, list):
            raise DispatchRejectedError("Codex plan candidate edits must be an array")
        files: dict[str, str] = {}
        for item in edits:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "content"}
                or not isinstance(item["path"], str)
                or not isinstance(item["content"], str)
                or item["path"] in files
            ):
                raise DispatchRejectedError(
                    "Codex plan candidate edits are invalid or duplicated"
                )
            files[item["path"]] = item["content"]
        return {"files": files}


_CLAUDE_MECHANISM = "claude-in-codex-workspace-v1"


class ClaudeDispatchRuntime(CodexDispatchRuntime):
    """Run a tool-free Claude CLI inside the same OS sandbox used by its probe.

    Codex is the sandbox broker, not the model provider. Staged input is sent
    through stdin; only proposed file contents leave the confined process.
    """

    def __init__(
        self,
        *,
        executable: str | None = None,
        sandbox_executable: str | None = None,
        timeout_seconds: int = 900,
    ) -> None:
        resolved = executable or shutil.which("claude")
        if not resolved:
            raise DispatchUnavailableError("Claude CLI is unavailable")
        path = Path(resolved)
        if os.name == "nt" and path.suffix.casefold() in {".cmd", ".bat", ".ps1"}:
            # The npm native distribution provides a direct executable. Avoid
            # passing JSON arguments through cmd.exe's shell interpretation.
            native = (
                path.parent / "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
            )
            if not native.is_file():
                raise DispatchUnavailableError(
                    "Claude dispatch requires a native executable"
                )
            resolved = str(native)
        self.claude_executable = resolved
        super().__init__(executable=sandbox_executable, timeout_seconds=timeout_seconds)
        self._proven_stage: Path | None = None

    def _sandbox_command(self, stage_root: Path, command: list[str]) -> list[str]:
        # A model running inside the sandbox needs its own API connection.
        # Define this per invocation; retain inherited filesystem protections
        # and managed requirements rather than changing the user's profile.
        return [
            self.executable,
            "sandbox",
            "--include-managed-config",
            *(["-c", 'windows.sandbox="unelevated"'] if os.name == "nt" else []),
            "-c",
            'permissions.apu-claude-dispatch.extends=":workspace"',
            "-c",
            "permissions.apu-claude-dispatch.network.enabled=true",
            "-c",
            'permissions.apu-claude-dispatch.network.domains={"api.anthropic.com"="allow"}',
            "-c",
            "features.network_proxy=true",
            "-P",
            "apu-claude-dispatch",
            "-C",
            str(stage_root),
            *command,
        ]

    def probe(self, request: IsolationProbeRequest) -> IsolationProbeResult:
        self._proven_stage = None
        result = super().probe(request)
        if result.attempted and result.write_denied:
            self._proven_stage = request.stage_root.resolve()
        return IsolationProbeResult(
            context_id=_CLAUDE_MECHANISM,
            mechanism=_CLAUDE_MECHANISM,
            attempted=result.attempted,
            write_denied=result.write_denied,
        )

    def run(self, request: RunnerRequest) -> dict[str, Any]:
        stage = request.stage_root.resolve()
        proven = self._proven_stage
        self._proven_stage = None
        if request.isolation_context_id != _CLAUDE_MECHANISM or proven != stage:
            raise DispatchUnavailableError(
                "Claude requires a fresh successful probe for this stage"
            )
        inputs: dict[str, str] = {}
        consumed = len(request.work_order.encode("utf-8"))
        for logical, source in request.staged_files.items():
            selected = source.resolve()
            if not selected.is_relative_to(stage) or not selected.is_file():
                raise DispatchRejectedError("Claude input is outside the proven stage")
            remaining = _MAX_RESULT_BYTES - consumed
            if remaining < 0:
                raise DispatchRejectedError(
                    "Claude staged input exceeds the size limit"
                )
            with selected.open("rb") as stream:
                raw = stream.read(remaining + 1)
            consumed += len(raw)
            if consumed > _MAX_RESULT_BYTES:
                raise DispatchRejectedError(
                    "Claude staged input exceeds the size limit"
                )
            try:
                inputs[logical] = raw.decode("utf-8")
            except UnicodeError as error:
                raise DispatchRejectedError("Claude input must be UTF-8") from error
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["edits"],
            "properties": {
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "content"],
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                    },
                }
            },
        }
        prompt = (
            request.work_order
            + "\n\nStaged input files (logical path to UTF-8 content):\n"
            + json.dumps(inputs, sort_keys=True)
            + "\nReturn proposed complete file contents using the supplied schema. "
            "Only propose edits to the listed logical paths."
        )
        command = self._sandbox_command(
            stage,
            [
                self.claude_executable,
                "--print",
                "--safe-mode",
                "--restricted",
                "--tools",
                "",
                "--permission-mode",
                "dontAsk",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
                "--disable-slash-commands",
                "--no-session-persistence",
                "--setting-sources",
                "",
                "--settings",
                '{"disableAllHooks":true}',
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(schema),
            ],
        )
        try:
            completed = run_confined(
                command,
                input=prompt.encode("utf-8"),
                timeout=self.timeout_seconds,
                cwd=stage,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise DispatchUnavailableError(
                f"Claude dispatch failed: {type(error).__name__}"
            ) from error
        if completed.returncode != 0:
            raise DispatchUnavailableError(
                f"Claude dispatch exited with status {completed.returncode}"
            )
        if len(completed.stdout) > _MAX_RESULT_BYTES:
            raise DispatchRejectedError("Claude plan candidate exceeds the size limit")
        try:
            envelope = json.loads(completed.stdout)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise DispatchRejectedError(
                "Claude did not return a valid result"
            ) from error
        if (
            not isinstance(envelope, dict)
            or envelope.get("type") != "result"
            or envelope.get("subtype") != "success"
            or envelope.get("is_error") not in (None, False)
        ):
            raise DispatchRejectedError("Claude did not return a successful result")
        value = envelope.get("structured_output")
        if (
            not isinstance(value, dict)
            or set(value) != {"edits"}
            or not isinstance(value["edits"], list)
        ):
            raise DispatchRejectedError("Claude did not return structured edits")
        files: dict[str, str] = {}
        for item in value["edits"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "content"}
                or not isinstance(item["path"], str)
                or item["path"] not in inputs
                or not isinstance(item["content"], str)
                or item["path"] in files
            ):
                raise DispatchRejectedError(
                    "Claude edits are invalid, duplicated, or outside the work order"
                )
            files[item["path"]] = item["content"]
        return {"files": files}


def runtime_for(
    name: str, *, timeout_seconds: int = 900
) -> CodexDispatchRuntime | ClaudeDispatchRuntime:
    if name == "codex":
        return CodexDispatchRuntime(timeout_seconds=timeout_seconds)
    if name == "claude":
        return ClaudeDispatchRuntime(timeout_seconds=timeout_seconds)
    raise ValueError(f"unsupported dispatch runner: {name}")
