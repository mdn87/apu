# APU Cross-System Runbook

This runbook covers installing and operating APU on another workstation. APU
is a tagged beta whose deterministic workflow is ready for controlled use.
PyPI publication is not currently provided.

## Supported environment

- Python 3.11 or newer
- macOS, Linux, or Windows
- Git access to `https://github.com/mdn87/apu`
- `pipx` or `uv` for an isolated command-line installation

The core audit, planning, application, validation, and rollback workflow does
not require a model API key. Optional behavioral validation requires a locally
installed and authenticated Codex or Claude CLI.

APU is tested in GitHub Actions on macOS, Ubuntu, and Windows. Run the current
repository suite before installing a development commit.

## Install

For a reproducible installation, pin the release tag:

```console
pipx install "git+https://github.com/mdn87/apu.git@v0.8.0"
apu --version
apu --help
```

With `uv`, use:

```console
uv tool install "git+https://github.com/mdn87/apu.git@v0.8.0"
```

The quoted Git URL works in POSIX shells and PowerShell. To test a checkout
instead:

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

## Dispatch a campaign work order

Use only the private path printed by `apu system propose`; exported prompts are
deliberately non-dispatchable through the automated command.

```console
apu dispatch /path/to/APU_HOME/campaigns/CAMPAIGN_ID/work-orders/WORK_ORDER_ID.md
apu review /path/to/APU_HOME/campaigns/CAMPAIGN_ID/plans/PLAN_ID.json \
  --output reviewed-plan.json
apu apply reviewed-plan.json
```

Dispatch creates or reuses the campaign snapshot before invoking the runner.
It fails closed if live-root write denial cannot be demonstrated. Accepted
runner output is still only a draft plan; review and apply remain separate.

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

## Diagnose a live autonomy-loss incident

Run these commands from the repository associated with the Codex session:

```console
apu-event "asked me to approve a reversible filename choice"
apu-wtf
apu-intervene
```

The event command selects the most recent active Codex JSONL session in the
current working directory. Use `--session-id` or `--trace-root` only when the
automatic selection is not the intended session. If no event was marked,
`apu-wtf` selects the most recent incomplete run itself.

For non-interactive Codex sessions, `apu-intervene` sends the temporary resume
instruction through `codex exec resume`. For Codex Desktop sessions it prints
and records a `codex resume` continuation; pass `--execute` to launch it. Use
`--dry-run` to verify selection without resuming anything. When an interactive
continuation finishes, attest the observed result with one of:

```console
apu-intervene --result completed
apu-intervene --result blocked
apu-intervene --result failed
```

The watcher refuses automatic intervention when the marked description names a
credential or permission barrier, destructive or external side effect, or
material information gap. It never changes global or repository policy. Its
private artifacts live under `APU_HOME/behavior` and contain hashes and safe
event metadata rather than transcript or tool content.

## Operate passive provider hooks

Preview the exact project-local fragment and target before applying it:

```console
apu hooks render --provider codex --scope project --repository . --passive-watch
apu hooks install --provider codex --scope project --repository . --passive-watch
```

Apply only after reviewing the preview, then verify the structural registration:

```console
apu hooks install --provider codex --scope project --repository . --passive-watch --apply
apu hooks status --provider codex --scope project --repository .
apu hooks doctor --provider codex --scope project --repository .
```

For Codex, open `/hooks` and separately review/trust the resulting non-managed
hook. APU records configuration but never grants trust. To remove only the APU
handlers while retaining unrelated provider configuration, preview and then run:

```console
apu hooks remove --provider codex --scope project --repository .
apu hooks remove --provider codex --scope project --repository . --apply
```

Replace `codex` with `claude` for Claude Code, or use `--scope user` without a
repository for user-level configuration. The bridge remains silent and
fail-open; malformed input or unavailable evidence storage does not block the
provider event.

## Platform behavior

- APU probes symlink capability in disposable private state.
- When directory symlinks are unavailable, including common Windows setups,
  skill installation uses a visible managed copy.
- Private POSIX state is restricted to the current user where the platform
  supports POSIX modes.
- Normal operation is local-first; network access is needed only to install or
  update APU, or when an optional external agent runner needs it.

## Updating

Update deliberately to a reviewed release tag:

```console
pipx install --force "git+https://github.com/mdn87/apu.git@v0.8.0"
```

Re-run `apu --version`, `apu validate`, and `apu status` after updating. Do not
point production automation at an unpinned branch.

## Publishing a tagged release

Release metadata has one source in `src/apu/__init__.py`; package metadata reads
that value during the build. Before tagging, replace the development version
with `MAJOR.MINOR.PATCH`, date the matching `CHANGELOG.md` heading, and run the
documented contributor checks from a clean checkout.

Create and push an annotated `vMAJOR.MINOR.PATCH` tag only after the release
commit is merged. The tag-triggered GitHub workflow verifies that the tag,
runtime version, and dated changelog entry agree. It reruns the deterministic
suite, builds and installs both distribution formats, checks every console entry
point and packaged resource, creates SHA-256 checksums and artifact provenance,
and publishes a GitHub Release. A mismatched or development-version tag fails
before publication.

Detailed maintainer commands are in `CONTRIBUTING.md`. The automated workflow
publishes GitHub Release assets only; it does not publish to PyPI.

## Current release limitations

- There is no PyPI publication yet; install from the Git tag or the wheel
  attached to the GitHub Release.
- Live Codex and Claude behavioral checks depend on locally available,
  authenticated CLIs and are not part of the deterministic release gate.
- Provider-managed package upgrades remain unavailable unless the provider can
  select an exact version and prove rollback to the captured version and tree.

Use APU first on a non-critical repository, retain the generated plan and
receipt, and promote it more broadly only after structural validation passes
and the resulting effective instruction stack has been inspected.
