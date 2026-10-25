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
pipx install "git+https://github.com/mdn87/apu.git@v0.10.0"
apu --version
apu --help
```

With `uv`, use:

```console
uv tool install "git+https://github.com/mdn87/apu.git@v0.10.0"
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

`apu apply` also checks that the install continues the recorded chain. An
install that starts from exactly what the earlier one wrote needs nothing: the
earlier receipt validates as `superseded by INSTALLATION_ID (continuous chain:
...)`. If the latest active installation that wrote a file recorded a different
hash than the file has now, something other than APU changed it in between. The apply stops
before any write and names the file, the earlier installation and both hashes.
If the change was intended, say so and the install proceeds:

```console
apu apply plan.json --acknowledge-drift "hook is edited in place by other work"
```

The reason is recorded on the new receipt. While the new installation stays
active, the older installation's mismatch on that file is reported as
`superseded by INSTALLATION_ID (drift acknowledged: REASON)` instead of failing.
Rolling back or retiring the new installation ends that exemption. Files APU
only adopted unchanged, and installations that were retired, never trigger the
check. A dispatched plan takes the flag through `apu apply` too; `apu system
apply` has no such flag and stops on a gap.

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

Receipt validation re-hashes each installed file. A file APU wrote fails when it
later differs from the receipt. A file APU adopted unchanged (recorded installed
hash equal to original hash) is not tracked for drift, so later edits by other
tools do not turn `apu status` red.

An installed gate-hook change can also be checked directly. After the Dias
note-path exemption was applied, this reports `applied (already applied)` and
would report `not applied` if a later edit to the live hook dropped it:

```console
python incidents/scripts/gate_intervention_2026_09_23_dias.py --check
```

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

Select the runner with `--runner codex` or `--runner claude`. The Claude runner
runs inside verified workspace isolation. `--attempt` and `--timeout` are
available for both.

## Update a Claude plugin with a restore point

Package updates are explicit and recoverable. APU captures the plugin store,
settings, and marketplace checkout, updates, then verifies the exact installed
version and tree; a failed or different result is restored and re-verified.

```console
apu packages upgrade PLUGIN@MARKETPLACE --target-version VERSION \
  --target-tree ./reviewed-plugin --operation-id OPERATION_ID
apu packages status TRANSACTION_ID
apu packages rollback TRANSACTION_ID
```

Use `--target-sha256` instead of `--target-tree` when you hold the installed-tree
digest (not a candidate artifact digest). `--claude-home HOME` targets an
isolated home. `rollback --expected-state DIGEST` reconciles an interrupted or
later-changed state against a reviewed digest. See the README for the full
contract.

## Prune retained evidence

Retention is previewed first and applied only from the saved, unchanged preview:

```console
apu behavior prune --out retention-plan.json
apu behavior prune --apply retention-plan.json
```

Save the preview outside the APU state directory. `--days`, `--max-files`, and
`--max-bytes` adjust the policy. A session referenced by any saved APU record is
retained. See the README for the pinning rules.

## Roll back

Use the exact receipt path printed by `apu apply` or `apu init --apply`:

```console
apu rollback --receipt /path/to/APU_HOME/installations/INSTALLATION_ID/receipt.json
```

Rollback verifies that installed files still match the receipt. If a target
has drifted, APU refuses to overwrite it; inspect and reconcile that file
manually. A successful rollback restores backups and removes the installation
from the active registry.

Rollback keeps the registry entry's earlier fields (`applied_at`,
`monitoring_started_at`, and any retirement record). It refuses a retired
installation unless you pass `--allow-retired`.

## Retire a stale installation record

When a file APU wrote is later changed by something else, and a newer
installation now covers that file, the older receipt fails validation for good.
Retire it to acknowledge the change; nothing on disk is restored or removed:

```console
apu retire --receipt /path/to/APU_HOME/installations/INSTALLATION_ID/receipt.json \
  --reason "hook is edited in place by the voice stack"
```

Retirement sets the registry status to `retired` and records `retired_at`, the
reason, the live hash of each target at that moment, and `superseded_by` (the
active installations that cover the same targets). The receipt, backup and
outcome records are not touched, and the entry never becomes active again.
`apu status` and `apu validate` list it as a passing `retired` row. Its
monitoring display stops at the retirement time.

APU refuses to retire an installation when no other active installation covers
its targets (`--allow-uncovered` overrides this and stops watching those files)
or when it still validates (`--force` overrides this). Retiring twice changes
nothing. A file APU wrote and has not retired still fails on any later edit.

Run rollback before uninstalling the CLI if the APU-managed instruction
changes should also be removed:

```console
pipx uninstall agent-policy-updater
```

Uninstalling the CLI alone does not alter installed instructions or delete APU
state.

## Diagnose a live autonomy-loss incident

Run these commands from the repository associated with the agent session:

```console
apu-event "asked me to approve a reversible filename choice"
apu wtf
# Run the printed apu-intervene --diagnosis ... command.
```

The event command selects one fresh, incomplete Codex or Claude Code JSONL
session in the current working directory. Use `--provider codex|claude-code`,
`--session-id`, or `--trace-root` when automatic selection is ambiguous or is
not the intended session. If no matching event was marked, `apu wtf` selects
one fresh, incomplete run in the current directory. It never reuses another
directory's incident by default. Use `apu wtf --fresh` to capture the run
again after it changes, or `apu wtf --incident ID` to inspect a saved incident
from any directory. The legacy `apu-wtf` spelling still works.

When the stop was an easy call the agent should have made on its own, use the
one-step form instead of `apu-event` plus `apu-wtf`:

```console
apu ezpz
# Run the printed apu-intervene --diagnosis ... command.
```

`apu ezpz` (also `apu-ezpz`) marks a fresh incident with the asserted `easy-decision-gate`
signal and diagnoses it. The diagnosis recommends the decide-and-continue
resume template, which `apu-intervene` then sends or prints. A barrier in the
evidence still blocks automatic intervention.

Read the session and directory in the output before continuing. The printed
command is bound to that diagnosis even if another incident is marked later.
If the result is `insufficient-evidence`, describe the stop with `apu-event`
and diagnose it again. Attribution errors include a recovery hint; `--json`
keeps the machine-readable result without additional prose.

For non-interactive Codex sessions, `apu-intervene` sends the temporary resume
instruction through `codex exec resume`. For Codex Desktop sessions it prints
and records a `codex resume` continuation. For Claude Code it prints and records
`claude --resume`; because the persisted entrypoint does not reliably identify
`--print`, APU never launches that continuation without `--execute`. Use
`--dry-run` to verify provider, session, and cwd binding without resuming
anything. When an interactive continuation finishes, attest the observed result
with one of:

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
pipx install --force "git+https://github.com/mdn87/apu.git@v0.10.0"
```

Re-run `apu --version`, `apu validate`, and `apu status` after updating. Do not
point production automation at an unpinned branch.

## Current release limitations

- There is no PyPI publication yet; install from the Git tag or the wheel
  attached to the GitHub Release.
- Live Codex and Claude behavioral checks depend on locally available,
  authenticated CLIs and are not part of the deterministic release gate.
- Package upgrades are supported for Claude plugins through APU's own restore
  points. Other providers remain unavailable unless the provider can select an
  exact version and prove rollback to the captured version and tree.

Use APU first on a non-critical repository, retain the generated plan and
receipt, and promote it more broadly only after structural validation passes
and the resulting effective instruction stack has been inspected.
