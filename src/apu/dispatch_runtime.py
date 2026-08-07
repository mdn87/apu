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
        command = [
            self.executable,
            "sandbox",
            *(["-c", 'windows.sandbox="unelevated"'] if os.name == "nt" else []),
            "-P",
            ":workspace",
            "-C",
            str(stage_root),
            *confined_command,
        ]
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


def runtime_for(name: str, *, timeout_seconds: int = 900) -> CodexDispatchRuntime:
    if name == "codex":
        return CodexDispatchRuntime(timeout_seconds=timeout_seconds)
    if name == "claude":
        raise DispatchUnavailableError(
            "Claude automated dispatch is unavailable: no equivalent "
            "capability-tested live-root write denial is implemented"
        )
    raise ValueError(f"unsupported dispatch runner: {name}")
