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

## Campaigns and work orders

M6 turns a system inventory into an immutable campaign with deterministic
operations and privacy-safe work orders:

```console
apu system propose --inventory system-inventory.json --output campaign.json
apu system apply campaign.json --auto-only
apu system status
```

Add `--emit-prompts DIR` to export hand-run work orders; APU warns that these
copies are outside private state protection. Credential-exposure findings are
always manual-only and render file, line, and content hash without the value.
Other findings on sensitive surfaces use private sanitized staging and
structurally verified placeholders. Before the first deterministic mutation,
APU locks the campaign, records a campaign-bound snapshot, and stamps the
receipt with campaign, snapshot, and idempotency data.

## Guidance and model refresh

M7 makes guidance and model identity explicit, versioned inputs. These are the
only system commands that use the network:

```console
apu refresh guidance --profile profile.toml --output guidance-refresh.json
apu refresh models --profile profile.toml --output model-refresh.json
apu system audit --profile profile.toml --json system-inventory.json
```

Guidance refresh stores bounded exact-byte HTTPS snapshots privately and emits
a distillation work order. It never adopts model-written guidance
automatically. After reviewing a returned candidate and approval artifact:

```console
apu refresh guidance --profile profile.toml \
  --adopt baseline-candidate.json --approval approval.json
apu guidance diff BEFORE_BASELINE AFTER_BASELINE
```

Model refresh observes installed CLI versions and configured selectors
offline, then resolves them only against fixed official provider listing
endpoints. Provider credentials are read at request time from the process
environment and are never persisted. Exact published model IDs can resolve
directly; aliases and omitted defaults remain visibly unverified unless the
authoritative listing supplies their mapping. Failed refreshes preserve the
matching last-known identity as stale rather than presenting it as current.

System inventories now carry a typed baseline/model evaluation context. New
campaigns verify its immutable private artifacts and freeze it in the campaign
manifest; older M6 campaigns remain readable, while v1 inventories must be
regenerated before creating a new campaign.

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
