# APU — Agent Policy Updater

APU is a local-first command-line tool for auditing, simplifying, installing,
and evaluating durable coding-agent instructions.

The deterministic core inventories Codex and Claude instruction surfaces,
produces reviewable JSON plans, applies approved changes transactionally, and
rolls them back from local receipts. Optional behavioral fixtures can run
through an independently installed and authenticated Codex or Claude CLI.

## Install and use

```console
python -m pip install .
apu audit /path/to/repository --json inventory.json
apu propose --inventory inventory.json --output plan.json
apu review plan.json
apu apply plan.json
apu validate
```

`apu init /path/to/repository` creates a draft preview. Add `--apply` for the
explicit interactive review and transactional installation flow.

## System audit and restore points

M5 adds deterministic machine-level inventory and campaign-grade restore
points. Declare the roots APU may inspect in
`~/.config/apu/profile.toml` (or `%APPDATA%\apu\profile.toml` on Windows):

```toml
schema_version = 1
global_surfaces = ["~/.claude", "~/.codex", "~/.agents"]

[[roots]]
path = "~/Desktop/MyDocs"
excludes = ["node_modules", "archive/**"]
```

```console
apu system audit --json system-inventory.json
apu snapshot create --label before-policy-update
apu snapshot list
apu snapshot diff SNAPSHOT_ID
apu snapshot restore SNAPSHOT_ID
apu system status
```

Snapshot manifests preserve object types, links, junctions, empty directories,
content hashes, and meaningful POSIX modes. Restore is journaled and reverses
completed swaps on failure; interrupted recovery uses
`apu snapshot restore --resume JOURNAL_ID [--unwind]`.

## Development

```console
python -m pip install -e ".[dev]"
pytest
apu --help
```

The product rationale is in [concept.md](concept.md), the behavior contract is
in [spec.md](spec.md), and implementation sequencing is in
[implementation-plan.md](implementation-plan.md). The system-level
optimizer direction (cascading audits, work-order prompts, guidance refresh,
package research) is in [roadmap.md](roadmap.md). For cross-system
installation, validation, and rollback procedures, see
[RUNBOOK.md](RUNBOOK.md).
