# Contributing to APU

APU supports Python 3.11 and newer on macOS, Linux, and Windows. Its deterministic
tests do not require provider credentials or network access.

## Development environment

Create and activate an isolated environment, then install the editable
development package on macOS or Linux:

```console
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

In PowerShell, replace the activation command with
`.venv\Scripts\Activate.ps1`.

Run the full deterministic suite with:

```console
python -m pytest
```

The two Windows junction assertions are skipped on platforms that cannot create
Windows junctions. Optional live Codex and Claude checks require separately
installed and authenticated provider CLIs; never add provider credentials to
tests, fixtures, commits, or workflow configuration.

## Quality checks and hooks

Install the pinned opt-in hooks after installing the development dependencies:

```console
pre-commit install --hook-type pre-commit --hook-type pre-push
pre-commit run --all-files
```

The pre-commit stage validates configuration and package resources and applies
the repository-wide Ruff lint and formatting baseline to `src/apu`, `tests`,
and `scripts`. The pre-push stage runs the full deterministic suite. CI repeats
every hook check, so hooks are a convenience rather than the enforcement
boundary.

## Packaged resources

The top-level `fixtures`, `schemas`, `skills`, and `templates` directories are
the readable authoring assets. The installable mirror lives at
`src/apu/_resources` so it works under virtual-environment, user-base, and
`--target` installations. After changing an authoring asset, synchronize and
check the mirror:

```console
python scripts/sync_resources.py --write
python scripts/sync_resources.py
python -m pytest -q tests/test_resources.py
```

CI compares every resource path and hash after installing both distribution
formats. Do not add scheme-level `data-files`; package resources must remain
inside `apu`.

## Distribution checks

Build from a clean worktree and inspect both artifacts:

```console
python -m build --outdir dist
python -m twine check dist/*
```

The CI `distribution` job installs the wheel and source distribution into
separate clean environments, checks all five console entry points, compares the
installed resources with the authoring assets, and repeats the resource check
for isolated user-base and `--target` installations.

## Release process

Version metadata has one source: `src/apu/__init__.py`. For a stable release:

1. Replace the development version with `MAJOR.MINOR.PATCH`.
2. Change the matching changelog heading from `(Unreleased)` to an ISO date.
3. Run `pre-commit run --all-files`, `python -m pytest`, and the distribution
   checks above.
4. Merge the prepared release commit, create an annotated `vMAJOR.MINOR.PATCH`
   tag on that commit, and push the tag.

The tag-triggered release workflow rejects mismatched tags, versions, or
changelog headings. It reruns deterministic and distribution checks, generates
SHA-256 checksums and GitHub artifact attestations, and publishes the wheel,
source distribution, and checksum file to a GitHub Release. PyPI publication is
not currently configured.
