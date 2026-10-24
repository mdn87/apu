from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


def run_confined(
    command: list[str],
    *,
    input: bytes,
    timeout: float,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Bound a sandbox launch and terminate its process tree on interruption."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = process.communicate(input, timeout=timeout)
    except BaseException:
        # Kill the tree while its root still exists; killing only the broker
        # can leave a provider holding its output pipes and defeat the timeout.
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.SubprocessError):
            process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
