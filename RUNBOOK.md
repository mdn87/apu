# APU Cross-System Runbook

This runbook covers installing and operating APU on another workstation. APU
v0.1 is a source-distributed beta: its deterministic workflow is ready for
controlled use, while a tagged release and PyPI package are still pending.

## Supported environment

- Python 3.11 or newer
- macOS, Linux, or Windows
- Git access to `https://github.com/mdn87/apu`
- `pipx` or `uv` for an isolated command-line installation

The core audit, planning, application, validation, and rollback workflow does
not require a model API key. Optional behavioral validation requires a locally
installed and authenticated Codex or Claude CLI.

APU is tested in GitHub Actions on macOS, Ubuntu, and Windows with Python 3.11
and 3.13. The v0.1 implementation also passes the repository's 111-test suite.

## Install

For a reproducible installation, pin the reviewed v0.1 implementation:

```console
pipx install "git+https://github.com/mdn87/apu.git@7742d4e"
apu --version
apu --help
```

The expected version is `0.1.0`. With `uv`, use:

```console
uv tool install "git+https://github.com/mdn87/apu.git@7742d4e"
```

The quoted Git URL works in POSIX shells and PowerShell. To test an unpublished
checkout instead:

```console
git clone https://github.com/mdn87/apu.git
cd apu
python -m pip install .
```

## Safe first run

Run the guided command without `--apply` first. This only audits the effective
Codex and Claude instruction surfaces and writes a draft plan:

```console
apu init /path/to/repository
```

The command prints the draft plan path and a summary. Read the plan before
continuing. When its proposed operations are acceptable, start the interactive
review and application flow:

```console
apu init /path/to/repository --apply
```

Every mutating operation must receive a terminal review decision. Approved
operations execute; rejected operations are skipped; deferred operations keep
the plan in draft and prevent application. APU asks for final confirmation
before changing files.

`--yes` skips only that final confirmation. It does not approve operations or
make `apu init --apply` non-interactive.

## Explicit workflow

Use the individual commands when the plan artifacts need to be retained,
reviewed, or transferred through a separate approval process:

```console
apu audit /path/to/repository --json inventory.json
apu propose --inventory inventory.json --output plan.json
apu review plan.json
apu apply plan.json
apu validate
apu status
```

`apu review` updates the plan in place unless `--output` is supplied. For a
non-interactive policy decision, `--approve-all-recommended` approves only
recommended operations; unresolved operations leave the plan in draft.

`apu apply` accepts only an approved, schema-valid plan. Before each write it
checks the recorded precondition, stages the transaction, creates backups, and
writes a receipt.

## State and receipts

APU stores private state in:

- macOS and Linux: `$XDG_STATE_HOME/apu` when set, otherwise
  `~/.local/state/apu`
- Windows: `%LOCALAPPDATA%\apu`
- Any platform: the absolute path in `APU_HOME`, when set

The state directory contains plans, installation receipts, transaction data,
the installation registry, and optional outcome records. Retain this directory
for validation and rollback. `apu status` prints the resolved state location
and the current drift status.

## Validate

Validate every registered installation:

```console
apu validate
```

Or validate a particular artifact:

```console
apu validate --plan /path/to/plan.json
apu validate --receipt /path/to/receipt.json
```

Structural validation is local and deterministic. Optional live behavioral
checks execute the packaged fixtures through an authenticated agent CLI:

```console
apu init /path/to/repository --apply --behavioral codex
apu init /path/to/repository --apply --behavioral claude
```

Behavioral results distinguish passed, failed, skipped, and unavailable
checks. Runner-specific metadata that the selected CLI cannot observe is
skipped rather than treated as a failure.

## Roll back

Use the exact receipt path printed by `apu apply` or `apu init --apply`:

```console
apu rollback --receipt /path/to/APU_HOME/installations/INSTALLATION_ID/receipt.json
```

Rollback verifies that installed files still match the receipt. If a target
has drifted, APU refuses to overwrite it; inspect and reconcile that file
manually. A successful rollback restores backups and removes the installation
from the active registry.

Run rollback before uninstalling the CLI if the APU-managed instruction
changes should also be removed:

```console
pipx uninstall agent-policy-updater
```

Uninstalling the CLI alone does not alter installed instructions or delete APU
state.

## Platform behavior

- APU probes symlink capability in disposable private state.
- When directory symlinks are unavailable, including common Windows setups,
  skill installation uses a visible managed copy.
- Private POSIX state is restricted to the current user where the platform
  supports POSIX modes.
- Normal operation is local-first; network access is needed only to install or
  update APU, or when an optional external agent runner needs it.

## Updating

Until a tagged release exists, update deliberately to a reviewed commit:

```console
pipx install --force "git+https://github.com/mdn87/apu.git@APPROVED_COMMIT"
```

Re-run `apu --version`, `apu validate`, and `apu status` after updating. Do not
point production automation at an unpinned branch.

## Current release limitations

- There is no Git tag, GitHub Release, or PyPI publication yet.
- Installation is therefore from a Git commit rather than a signed release
  artifact or package index.
- Live Codex and Claude behavioral checks depend on locally available,
  authenticated CLIs and are not part of the deterministic release gate.
- The optional Superpowers adapter is deferred beyond v0.1.

Use v0.1 first on a non-critical repository, retain the generated plan and
receipt, and promote it more broadly only after structural validation passes
and the resulting effective instruction stack has been inspected.
