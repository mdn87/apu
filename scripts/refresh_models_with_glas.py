"""Run an authenticated model refresh with BWS provider references."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    manifest = ROOT / "config" / "glas.models.local.json"
    if not manifest.is_file():
        parser.error(f"Create the BWS reference manifest at {manifest}")
    glas_python = os.environ.get("GLAS_PYTHON", sys.executable)
    env = os.environ.copy()
    for name in ("BWS_ACCESS_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(name, None)
    sources = [str(ROOT / "src")]
    if os.environ.get("GLAS_ROOT"):
        sources.append(str(Path(os.environ["GLAS_ROOT"]) / "src"))
    if env.get("PYTHONPATH"):
        sources.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(sources)
    command = [
        glas_python, "-m", "glas", "run", "--manifest", str(manifest), "--",
        sys.executable, "-m", "apu", "refresh", "models",
        "--profile", args.profile, "--output", args.output,
    ]
    return subprocess.call(command, cwd=ROOT, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
