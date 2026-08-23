# APU v0.1 Product and Technical Specification

- **Status:** Initial implementation specification
- **Repository:** `https://github.com/mdn87/apu`
- **Product name:** Agent Policy Updater
- **CLI command:** `apu`

## 1. Goal

Build a local-first command-line application and reusable agent skill that can:

1. inventory effective agent instructions and their precedence;
2. classify what should remain local, move, become portable, be mechanized, or
   be removed;
3. produce a reviewable installation plan;
4. guide the user through consequential decisions;
5. apply approved changes transactionally;
6. validate both reduced ceremony and retained defect detection;
7. roll back every managed change.

The MVP must work without an AI API key. An agent may use the bundled skill to
interpret findings, but the CLI’s discovery, planning, mutation, and rollback
mechanisms must remain deterministic.

## 2. Scope

### 2.1 Supported instruction surfaces

The MVP supports:

- `~/.codex/AGENTS.md`;
- `~/.claude/CLAUDE.md`;
- repository and ancestor `AGENTS.md`, `CLAUDE.md`, and `CLAUDE.local.md`;
- Claude `@path` imports referenced by supported instruction files;
- project and user-level Claude rules, including `paths`-scoped rules;
- skill directories containing `SKILL.md`;
- Claude session-start hooks and local marketplace metadata;
- session-start hooks and skills contributed by enabled Claude plugins, whose
  text reaches every session even though the user did not write it;
- Codex/Claude-compatible shared skills under `~/.agents/skills`;
- explicit files or roots supplied by the user.

Provider adapters define names, precedence, import capability, managed-section
syntax, and installation behavior. The core engine must not hard-code one
provider’s paths.

### 2.2 Optional evidence sources

- Codex JSONL session directories;
- provider lifecycle-hook JSON objects;
- Git repositories and revision ranges;
- user-provided incident notes;
- previous APU installation receipts;
- outcome summaries from the monitoring window.

Trace analysis emits metadata and aggregate counts only. It must not copy
messages, prompts, command arguments, environment values, or secrets into the
report.

Provider execution evidence is normalized into `invocation`, `result`, and
`state` classes. Every persisted event uses a strict allowlisted schema and one
of `asserted`, `observed`, `verified`, `stale`, `contradicted`, or
`unverifiable`. Provider schemas remain adapter inputs rather than APU domain
models. A `verified` source status proves only that the referenced record or
independent state observation still matches; it does not prove semantic task
correctness.

### 2.3 Out of scope for v0.1

- hosted synchronization;
- a web UI or dashboard;
- a database or background daemon;
- automatic model API calls;
- arbitrary semantic merging of every Markdown format;
- organization-wide policy enforcement;
- automatic modification of production repositories;
- a built-in Superpowers adapter or vendored Superpowers framework; optional
  Superpowers integration is deferred to post-v0.1;
- automatic discovery of organization-managed Claude policy files, which is
  deferred to v0.2 but remains auditable in v0.1 when supplied explicitly.

### 2.4 State home

APU stores its own mutable state under `APU_HOME`.

Default locations:

- macOS and Linux: `${XDG_STATE_HOME}/apu` when `XDG_STATE_HOME` is set,
  otherwise `~/.local/state/apu`;
- Windows: `%LOCALAPPDATA%\apu`;
- explicit override: the absolute path in `APU_HOME`.

```text
APU_HOME/
├── inventories/
├── plans/
├── installations/
│   └── <installation-id>/
│       ├── receipt.json
│       └── backups/
├── outcomes/
│   └── <installation-id>.jsonl
├── behavior/
│   ├── evidence/
│   │   └── <provider>/<session-id-sha256>.jsonl
│   └── audits/
│       └── behavior-audit-<id>.json
├── transactions/
└── registry.json
```

`registry.json` indexes applied installations and their current receipt.
Inventories and plans are retained only when the user requests an output or
uses the guided flow. Transactions are removed after successful apply or
rollback; failed transactions remain available for diagnosis until explicitly
cleaned.

On POSIX systems, APU creates state directories with mode `0700` and files with
mode `0600`, subject to a stricter existing umask. On Windows, APU relies on the
current user's profile ACL and does not emulate POSIX modes.

## 3. Package layout

```text
apu/
├── .github/
│   └── workflows/
│       └── ci.yml
├── pyproject.toml
├── src/apu/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── audit.py
│   ├── state.py
│   ├── filesystem.py
│   ├── render.py
│   ├── resources.py
│   ├── trace.py
│   ├── discovery.py
│   ├── precedence.py
│   ├── classify.py
│   ├── planning.py
│   ├── wizard.py
│   ├── apply.py
│   ├── rollback.py
│   ├── validate.py
│   ├── receipts.py
│   ├── outcomes.py
│   ├── evidence.py
│   ├── evidence_cli.py
│   ├── models.py
│   ├── adapters/
│   │   ├── base.py
│   │   ├── codex.py
│   │   └── claude.py
│   └── runners/
│       ├── base.py
│       ├── codex.py
│       └── claude.py
├── skills/
│   └── optimizing-agent-instructions/
│       ├── SKILL.md
│       ├── agents/openai.yaml
│       ├── scripts/analyze_agent_instructions.py
│       └── references/evaluation-scenarios.md
├── templates/
│   ├── global-AGENTS.md
│   ├── global-CLAUDE.md
│   └── repository-instructions.md
├── fixtures/behavioral/
│   ├── direct-config-edit/
│   ├── planned-coupled-change/
│   ├── delegated-independent-analysis/
│   ├── high-risk-auth-migration/
│   ├── explicit-named-skill/
│   └── seeded-boundary-defect/
└── tests/
```

Python 3.11 or newer is the target runtime. The core and initial wizard use the
standard library. A richer terminal UI may be added later without changing the
plan schema or core engine.

## 4. Core concepts

### 4.1 Instruction surface

An instruction surface is a file, skill, hook, manifest, or injected context
that can affect agent behavior.

```json
{
  "id": "sha256:...",
  "path": "/absolute/path",
  "kind": "agents|claude|skill|hook|manifest",
  "provider": "codex|claude|shared|unknown",
  "authority": "user|repository|package|generated",
  "scope": "global|workspace|repository|subtree|task",
  "real_path": "/resolved/path",
  "is_symlink": false,
  "content_sha256": "...",
  "mode": "0644 or null when not meaningful",
  "precedence": 30,
  "sensitive": false
}
```

The inventory stores hashes and metadata. Full contents may be read locally for
analysis and diff generation but are included in exported reports only when the
user explicitly requests it.

### 4.2 Instruction finding

A finding identifies a concrete policy issue or placement concern.

```json
{
  "id": "finding-<stable-hash>",
  "surface_id": "sha256:...",
  "location": {"line": 17},
  "category": "universal-skill-trigger",
  "severity": "high",
  "confidence": "high",
  "analysis_method": "structural|heuristic|agent-assisted|manual",
  "evidence": ["matched-rule", "trace:019f..."],
  "summary": "Skill invocation is required for every conversation."
}
```

The deterministic v0.1 core must detect:

- universal skill or tool trigger;
- unconditional design/approval gate;
- microtask planning or commit requirement;
- per-task implementer/reviewer loop;
- duplicated or contradictory instruction;
- stale environment or project fact;
- misplaced global or repository-specific fact;
- prose rule better enforced mechanically;
- unsupported or broken path/import/symlink;
- secret or sensitive material exposure risk.

Pattern-based findings are labeled `heuristic` and include the matched evidence
so a human or agent can verify them. Semantic judgments such as aggressive
priority language, speculative completeness, impossible-state handling, or a
test ritual detached from behavior risk are advisory `agent-assisted` or
`manual` findings in v0.1. The no-API core may surface keyword candidates for
these categories, but it must not claim semantic certainty.

Finding IDs are stable hashes of the surface ID, category, normalized location,
and detector version. Plans may therefore reference findings without embedding
their source text.

### 4.3 Residency recommendation

Every reviewed item receives one disposition:

```text
preserve
relocate
extract_to_skill
mechanize
replace_from_template
remove
manual
```

The recommendation records:

- proposed destination;
- reasoning;
- confidence;
- evidence;
- reversibility;
- whether confirmation is required.

APU may automatically propose high-confidence classifications. Mutation always
requires an approved plan.

### 4.4 Audit artifact

`apu audit --json` produces the inventory consumed by `apu propose`:

```json
{
  "schema_version": 1,
  "apu_version": "0.1.0",
  "generated_at": "RFC3339 timestamp",
  "scope": {
    "roots": ["/absolute/path"],
    "working_directories": ["/absolute/repository"],
    "root_session_id": null
  },
  "surfaces": [],
  "relationships": [
    {
      "type": "imports",
      "from_surface_id": "sha256:...",
      "to_surface_id": "sha256:... or null",
      "status": "active|missing|disabled|unknown",
      "location": {"line": 12}
    }
  ],
  "effective_stacks": [
    {
      "working_directory": "/absolute/repository",
      "surface_ids": ["sha256:..."]
    }
  ],
  "findings": [],
  "evidence_summary": {
    "sessions": null,
    "tool_calls": {},
    "git": null,
    "privacy": "Message and prompt content is not emitted."
  }
}
```

The canonical inventory hash is SHA-256 over UTF-8 JSON serialized with sorted
keys and compact separators, excluding no fields. Absolute paths are retained
in local artifacts but may be replaced with stable aliases in an explicitly
sanitized export.

Including `generated_at` is intentional: `inventory_sha256` identifies the
exact audit artifact approved by the user, not semantic equivalence between
audits. Surface content hashes and receipt hashes, rather than the inventory
hash, drive deduplication and drift detection.

`--root-session-id ID` restricts trace aggregation to the named root Codex
session and sessions whose metadata identifies them as descendants of that
root. Supplying it without at least one `--sessions` path is a usage error.

### 4.5 Effective precedence

For each requested working directory, APU constructs an ordered effective
stack using adapter rules:

1. applicable global instructions;
2. ancestor workspace or repository files;
3. closer subtree files;
4. relevant platform hook or injected context;
5. explicitly selected skills;
6. direct task instructions, when supplied for analysis.

The report distinguishes:

- shadowed rules;
- duplicated rules;
- conflicts where higher-precedence guidance reverses lower guidance;
- provider-specific syntax implementing the same shared policy.

## 5. Residency decision algorithm

The deterministic classifier applies these rules in order:

1. **Sensitive or volatile local state**

   Credentials, permissions, absolute machine paths, installed versions, and
   logs default to `preserve`.

2. **Repository truth**

   Build commands, architecture, domain invariants, and local completion
   criteria found above a repository default to `preserve` or `relocate` to the
   closest applicable repository.

3. **Reusable conditional method**

   A workflow that should activate only for identifiable task classes defaults
   to `extract_to_skill`.

4. **Mechanical invariant**

   A rule expressible as a deterministic block, parser, linter, permission, or
   test defaults to `mechanize`.

5. **Global personal preference**

   A stable cross-project preference that the model cannot reliably infer may
   remain global.

6. **Inferable, duplicated, stale, or speculative guidance**

   Defaults to `remove` or `manual`, depending on confidence and risk.

7. **Uncertain authority or scope**

   Always defaults to `manual`.

A recommendation cannot silently relocate a repository-owned rule into a
user-global file or export sensitive local data.

### 5.1 What the classifier reads

An instruction surface is prose, not a document. The classifier skips fenced
code blocks, unfenced diagram source, and lines carrying no alphanumeric
content, because an example command or a repeated separator is not a rule. A
repeated line is reported as `duplicate-instruction` only when it is
sentence-length; repeated headings, markers, and short labels are structure.

A build command inside a skill is documentation of a method rather than a
repository fact that belongs closer to a repository.

### 5.2 What a finding may change

`duplicate-instruction` is the only category whose correct remediation is
deleting the flagged line. Every other category — including every pressure
multiplier — is reported as a non-mutating `proposal_only` operation that
requires confirmation, because removing the line would discard the rule
instead of rewriting it, and choosing the replacement is the user's decision.

A surface with `package` authority is never rewritten. Its content is replaced
by the upstream package on the next update, so an edit would be silently lost
and would desync the local copy.

Each target path yields at most one operation. The same file can be an
effective surface for more than one provider, and two operations writing one
file would conflict.

## 6. Command-line interface

```text
apu audit [PATH ...] [--sessions PATH ...] [--git-repo PATH]
          [--root-session-id ID] [--json OUTPUT]

apu propose --inventory INVENTORY.json [--output PLAN.json]

apu review PLAN.json [--approve-all-recommended] [--output PLAN.json]

apu apply PLAN.json [--yes]

apu validate [--plan PLAN.json | --receipt RECEIPT.json |
              --fixture FIXTURE --runner codex|claude [--enable-runtime]]

apu rollback --receipt RECEIPT.json

apu status

apu outcome record --receipt RECEIPT.json [METRIC OPTIONS]

apu outcome list [--receipt RECEIPT.json]

apu evidence ingest-codex [--session-id ID] [--trace-root PATH] [--cwd PATH]

apu evidence ingest-hook --provider PROVIDER --event EVENT [--input JSON]
                         [--observe-state]

apu evidence observe-state --provider PROVIDER --session-id ID [--cwd PATH]

apu evidence show --provider PROVIDER --session-id ID [--verify]

apu init
```

### 6.1 Command behavior

`apu audit`

- is read-only;
- discovers surfaces and precedence;
- emits findings and sanitized evidence;
- uses `--root-session-id` only to select one traced session tree from supplied
  session directories and rejects the option when no session directory is
  supplied;
- writes only when an explicit output path is provided.

`apu propose`

- converts inventory and findings into operations;
- never mutates live instruction files;
- when `--output` is supplied, writes deterministic candidate files beside the
  plan in a derived `.candidates` directory; without an output path, mutations
  that require a candidate remain proposal-only;
- includes exact precondition hashes.

`apu review`

- starts the interactive review flow;
- modifies only the plan artifact;
- supports accepting, rejecting, editing, relocating, and deferring operations;
- accepts an explicit replacement candidate path for edit decisions and
  normalizes relocate decisions into one atomic remove/create pair;
- records operation-level approval decisions and sets the plan to `approved`
  only when every mutating operation is either approved or rejected, at least
  one mutating operation is approved, and none is pending or deferred;
- with `--approve-all-recommended`, approves only high-confidence recommended
  operations that do not require manual confirmation; pending or deferred
  operations keep the plan in `draft` and cause a nonzero exit.

`apu apply`

- accepts only a plan whose status is `approved`;
- uses `--yes` solely to suppress the final interactive confirmation;
- rechecks every approved operation's precondition hash;
- creates backups and an installation receipt;
- validates temporary results before atomic replacement.

`apu validate`

- checks structure, links, managed sections, plugin metadata, and representative
  behavioral fixtures;
- validates all active installations in `registry.json` when neither `--plan`
  nor `--receipt` is supplied;
- distinguishes passed, failed, skipped, and unavailable checks.

`apu rollback`

- verifies the receipt and current managed hashes;
- warns rather than overwriting user edits made after installation;
- restores byte-identical backups when safe.

`apu status`

- shows installed APU version, managed surfaces, drift, current provider
  adapters, last validation, and monitoring progress.

`apu outcome record`

- appends one local outcome to the installation's JSONL monitoring record;
- accepts elapsed time, agent/review/remediation counts, validation status,
  rework, and escaped-defect severity when known;
- permits partial records and marks the source as user, trace, or imported;
- never runs as a daemon or uploads the record.

`apu outcome list`

- prints raw or summarized outcomes for one installation or all registered
  installations;
- shows both elapsed days and material-task count against the 30-day/10-task
  monitoring target.

`apu init`

- runs audit, proposal, interactive review, validation, and optional apply as a
  guided first-run flow;
- is the entry point for the complete flow in section 7;
- defaults to stopping after proposal preview; ownership selection, behavioral
  validation, apply, and postflight are explicit opt-in continuations.

## 7. Interactive wizard

`apu init` enters the full flow below. `apu review PLAN.json` enters at
recommendation review for an existing plan and ends after plan preview or
approval unless the user explicitly continues to validation and apply.
In `apu init`, steps 1–5 and plan preview are the default path; ownership
selection, behavioral validation, apply, and postflight are opt-in
continuations.

### 7.1 Flow

1. **Select mode**
   - audit only;
   - audit and propose;
   - review existing plan;
   - apply approved plan;
   - roll back;
   - evaluate installed policy.

2. **Select scope**
   - global agent configuration;
   - current repository;
   - explicit directories;
   - optional trace and Git evidence.

3. **Review effective stacks**
   - show ordered surfaces for each representative working directory;
   - resolve symlinks visibly;
   - flag missing or generated targets.

4. **Review recommendation groups**
   - keep local;
   - portable policy;
   - move to repository;
   - mechanize;
   - remove;
   - manual decision.

5. **Resolve consequential cards**
   - show source, reason, evidence, destination, and candidate diff;
   - accept, reject, edit, relocate, defer, or apply to similar findings;
   - never require one prompt for every obvious retained item.

6. **Choose ownership strategy per target**
   - imported sidecar;
   - managed section;
   - complete generated ownership;
   - proposal only.

7. **Preview plan**
   - exact files, diffs, symlinks, marketplace changes, backups, checks, and
     rollback path.

8. **Validate**
   - structural checks;
   - balanced behavior fixtures;
   - at least one seeded defect.

9. **Apply or save**
   - apply transactionally;
   - save the plan for another agent or operator;
   - exit without changing live files.

10. **Postflight**
    - show receipt path;
    - show validation results;
    - explain runtime restarts required;
    - initialize the monitoring window.

### 7.2 Interaction requirements

- Every prompt offers a recommended choice and a concise consequence.
- The user can inspect the complete diff at any time.
- No decision is represented only in terminal history; it is persisted in the
  plan.
- Batch acceptance is allowed only for equivalent high-confidence findings.
- Low-confidence or high-impact operations cannot be batch-approved by default.
- Non-interactive operation consumes the same plan schema.

## 8. Plan format

```json
{
  "schema_version": 1,
  "apu_version": "0.1.0",
  "created_at": "RFC3339 timestamp",
  "inventory_sha256": "...",
  "status": "draft|approved|applied|rolled_back",
  "operations": [
    {
      "id": "op-001",
      "action": "preserve|merge|create|remove|symlink|configure",
      "target": "/absolute/path",
      "source": "template or source path or null",
      "ownership": "user|repository|apu",
      "strategy": "sidecar|managed_section|full_file|proposal_only",
      "precondition_sha256": "... or null",
      "proposed_sha256": "... or null for remove",
      "atomic_group_id": "relocate-001 or null",
      "group_content_sha256": "... or null",
      "backup_required": true,
      "requires_confirmation": true,
      "approval": {
        "status": "pending|approved|rejected|deferred",
        "recorded_at": "RFC3339 timestamp or null",
        "method": "interactive|approve-recommended|imported|null"
      },
      "reason": "...",
      "evidence": ["finding-id"]
    }
  ],
  "validation": {
    "commands": [],
    "fixtures": [],
    "required": []
  }
}
```

The plan must be JSON for standard-library parsing and cross-platform
interchange. Human-readable summaries and diffs are generated alongside it.

For `create` and `symlink` operations, and a `configure` operation that creates
new metadata, `precondition_sha256: null` means the target must not exist at
apply time. A missing target is therefore an explicit precondition, not an
unchecked case.

`relocate` is a review decision, not a one-target executable action. Before a
plan can be approved, the planner expands it into a paired `remove` and
`create`. The `remove` targets the source and uses its content hash as the
precondition; the `create` targets the destination, requires that target to be
missing, and uses the same hash as its proposed content. Both operations carry
the same non-null `atomic_group_id` and `group_content_sha256`. Review records
one decision for the group and persists identical approval state on both
members. Plan validation rejects an incomplete pair, unequal content hashes,
or divergent decisions. Apply preflights both members before either mutation
and rolls the pair back as one transaction unit. Non-relocation operations must
set both atomic-group fields to `null`.

A mutating operation is resolved when its approval status is `approved` or
`rejected`. A plan becomes `approved` only when all mutating operations are
resolved and at least one is approved. `pending` and `deferred` are unresolved
states and keep the plan in `draft`; a plan whose mutating operations are all
rejected also remains `draft`. Apply executes only approved operations.
Preserve and rejected operations are retained as review history but are never
executed.

For approval purposes, a mutating operation has an action of `merge`, `create`,
`remove`, `symlink`, or `configure` and a strategy other than
`proposal_only`. `preserve` and `proposal_only` operations are non-mutating.

### 8.1 Outcome record

Each line under `APU_HOME/outcomes/<installation-id>.jsonl` is independently
parseable:

```json
{
  "schema_version": 1,
  "installation_id": "install-...",
  "recorded_at": "RFC3339 timestamp",
  "task_id": "user-supplied-or-generated",
  "material": true,
  "source": "user|trace|imported",
  "elapsed_seconds": null,
  "agent_count": null,
  "review_count": null,
  "remediation_count": null,
  "validation": "passed|failed|partial|unknown",
  "rework": false,
  "escaped_defect": {
    "present": false,
    "severity": "none|ordinary|serious",
    "category": null
  },
  "notes": null
}
```

Notes are optional, local, and excluded from sanitized exports by default.
Monitoring completion means both 30 elapsed days and 10 records marked
`material: true`; it is not an automated quality verdict.

## 9. Installation strategies

### 9.1 Sidecar

Use when a provider supports importing another instruction file. APU owns the
sidecar and adds or proposes one import in the user-owned file.

### 9.2 Managed section

Use explicit markers:

```markdown
<!-- apu:begin policy version=0.1.0 -->
[managed policy]
<!-- apu:end policy -->
```

APU stores the installed section hash. If the section changes outside APU,
future application requires a new review.

### 9.3 Full-file ownership

Allowed only when:

- the file does not exist; or
- the user explicitly grants APU ownership.

The receipt must still include the prior state and rollback information.

### 9.4 Proposal only

Use when the provider lacks safe imports, the file structure is unfamiliar, or
authority is uncertain. APU emits a candidate diff without mutation.

### 9.5 Canonical optimizer-skill installation

The source of the bundled optimizer skill is the versioned package resource or
an explicit canonical checkout selected by the user. Adapters must resolve and
hash that source before proposing installation. They must not use a generated
plugin or marketplace cache as the source.

The Codex adapter proposes a `symlink` from the canonical source to
`~/.agents/skills/optimizing-agent-instructions`. The Claude adapter proposes a
`symlink` into `~/.claude/skills/optimizing-agent-instructions` and, when the
user selects marketplace ownership, a separate `configure` operation that
points local marketplace metadata at the canonical source. These are ordinary
reviewable plan operations with exact source and target preconditions. When
symlinks are unavailable, the adapter may propose an APU-owned copy only after
the capability fallback is visible in the plan. Existing unknown or
user-managed targets default to `preserve` or `proposal_only`.

## 10. Transaction and rollback

Before applying:

1. resolve every approved target without following an unexpected changed
   symlink;
2. verify approved-operation precondition hashes and file modes;
3. verify the plan’s approval state;
4. verify every atomic group is complete, internally consistent, and uniformly
   approved or rejected;
5. reject duplicate resolved mutation targets, protected filesystem, home,
   state, and configured roots, and standalone recursive directory removal;
6. reject an installation ID already present in registry or installation
   storage;
7. create a private temporary transaction directory;
8. copy original bytes and metadata into the transaction;
9. render all proposed outputs;
10. parse and validate rendered outputs.

Commit the transaction with `os.replace` where the platform permits it. On
Windows, open-file sharing violations or unsupported replacements fail before
the affected target is counted as applied; APU retries only a small bounded
number of transient sharing violations and otherwise rolls back already-applied
operations in reverse order.
Atomic relocation groups are preflighted before either member is committed and
are restored as a unit if either member fails.
Temporary symlink names are collision-safe and never cause an unrelated sibling
to be removed. Tree hashes include object type, symlink destination, empty
directories, relative paths, and file bytes.

Symlink creation is capability-tested. On Windows, when symlink privilege or
Developer Mode is unavailable, the adapter must select a reviewed copy,
managed-section, or proposal-only operation instead of silently substituting a
different link type. POSIX mode capture and restoration are skipped when the
platform does not expose meaningful POSIX modes.

The installation receipt contains:

- APU and schema versions;
- timestamp and host identifier hash;
- applied operation IDs;
- original and installed hashes;
- backup paths;
- original file modes;
- created symlinks and their targets;
- provider/plugin source changes;
- validation results;
- rollback status.

Receipts contain no secrets or full trace contents.

Rollback accepts only the canonical receipt registered under the same
`APU_HOME`, validates every backup path and hash before the first mutation, and
then applies drift guards to each rollback unit.

Rollback also removes a symlink created by APU when, and only when, it still
points to the target recorded in the receipt. A changed link, replacement file,
or user-created object at that path is reported as drift and left untouched.
Copied fallbacks and generated files follow the same installed-hash drift rule
before removal or restoration.

## 11. Provider adapters

An adapter implements:

```python
class ProviderAdapter:
    def discover(self, roots): ...  # returns surfaces and relationships
    def precedence(self, cwd, discovery): ...
    def import_strategy(self, target): ...
    def validate_surface(self, path): ...
    def plan_install(self, policy, inventory): ...
    def postflight(self, receipt): ...
```

### 11.1 Codex adapter

- discovers global and hierarchical `AGENTS.md`;
- discovers shared `~/.agents/skills`;
- validates skill frontmatter and optional `agents/openai.yaml`;
- treats generated/versioned plugin caches as noncanonical;
- plans canonical optimizer-skill installation under `~/.agents/skills` with
  explicit `symlink` or reviewed capability-fallback operations;
- uses managed-section or proposal-only installation where imports are not
  supported.

### 11.2 Claude adapter

- discovers global and hierarchical `CLAUDE.md` and `CLAUDE.local.md`;
- follows Claude Code's documented hierarchy, including loading
  `CLAUDE.local.md` after `CLAUDE.md` at the same directory level;
- discovers project and user-level `.claude/rules`, skills, hooks, and
  marketplaces; evaluates `paths` frontmatter when constructing an effective
  stack; treats no directory at or above the user's home as a repository, so a
  repository stored beneath home does not re-discover global configuration
  with repository authority; `.claude/rules` is a documented Claude Code
  instruction surface
  (<https://code.claude.com/docs/en/memory#organize-rules-with-clauderules>);
- resolves `@path` imports relative to the containing instruction file,
  recursively up to Claude Code's documented limit, and reports missing,
  circular, disabled, or unreadable imports;
- reports an APU-owned sidecar as orphaned when it exists but no active
  supported instruction surface imports it;
- prefers a sidecar import when safe;
- plans canonical optimizer-skill installation under `~/.claude/skills` with
  explicit `symlink` or reviewed capability-fallback operations;
- configures canonical local marketplace sources rather than editing caches;
- reports when a restart is required;
- defers automatic discovery of organization-managed `CLAUDE.md` policy
  locations to v0.2; explicit roots remain supported in v0.1.

### 11.3 Post-v0.1 Superpowers integration

v0.1 ships no Superpowers adapter module, bundled checkout, discovery promise,
or acceptance requirement. A later optional integration may accept an explicit
canonical checkout and inspect its branch, cleanliness, installed links, active
skills, and session-start injection without treating generated caches as
canonical. That integration requires its own reviewed specification before it
can mutate, rebase, commit, or push anything.

## 12. Privacy and safety

- Audit is read-only.
- Exported inventory defaults to paths, hashes, categories, and counts.
- Prompt and message bodies are not emitted from session traces.
- Evidence records never contain reasoning, command text, tool input/output
  bodies, environment content, or raw provider records. Hook inputs are
  projected before persistence; changed repository paths are stored as hashes.
- Environment variables, tokens, cookies, and credential-shaped values are
  redacted or omitted.
- `APU_HOME`, backups, receipts, plans, and inventories use user-only
  permissions where POSIX modes are supported and the current user profile ACL
  on Windows.
- Destructive operations require exact resolved targets.
- APU never recursively deletes a workspace, repository root, home directory,
  or configured instruction root.
- Existing user edits after installation prevent automatic rollback over the
  changed content.
- Network access is unnecessary for normal audit, planning, apply, validation,
  and rollback.

## 13. Validation

### 13.1 Structural tests

- instruction and skill frontmatter parses;
- managed-section markers are balanced and unique;
- JSON plans and receipts validate against their schema;
- symlink targets resolve to intended canonical paths;
- plugin and marketplace manifests agree on versions;
- installed files match receipt hashes;
- audit mode performs no writes.

### 13.2 Behavioral fixtures

| Fixture | Expected behavior |
|---|---|
| Typo or prescribed configuration edit | Direct work and focused parse/diff check |
| Localized reproducible bug | Focused regression evidence |
| Coupled multi-file behavior | Concise plan, no agent per microtask |
| Independent read-heavy analyses | Small bounded parallel set when beneficial |
| Authentication or destructive migration | Explicit plan and one justified review |
| Explicit named skill/reviewer request | Request honored without adjacent ceremonies |

At least one fixture includes an unnamed realistic defect, such as a caller and
callee disagreeing on a renamed field or a boundary check accepting an invalid
value. Passing requires identifying the defect with concrete evidence without
inventing unrelated findings.

### 13.3 Behavioral execution harness

Behavioral fixtures are optional runtime-backed evaluations, not part of the
no-model deterministic core. Each fixture contains:

```text
fixture-name/
├── case.json
├── prompt.md
├── repo/
└── checks/
```

`case.json` declares:

- supported runners;
- expected proportionality tier;
- allowed and forbidden normalized delegation/review events;
- files or outputs expected after execution;
- deterministic validation commands;
- seeded-defect success criteria;
- timeout and cleanup behavior.

Runner adapters initially support:

- `codex exec` when a compatible Codex CLI is installed and authenticated;
- `claude -p` when a compatible Claude Code CLI is installed and authenticated;
- an exported manual bundle when neither runtime is available.

The runner copies the fixture repository to a private temporary directory,
invokes one selected runtime, captures its exit status and supported
tool/delegation metadata, runs the deterministic checks, and removes the
temporary checkout after recording a sanitized result. It does not use a
provider API directly and does not provision credentials.

Each runner adapter declares the normalized event types it can observe for the
detected CLI version. The Codex adapter consumes `codex exec --json` JSONL
events; the Claude adapter consumes `claude -p --output-format stream-json`
events with the additional flags required by that CLI version. Raw provider
events are translated into the runner's declared capability set rather than
assuming equivalent schemas.

Every result includes the selected CLI name, detected version when available,
authentication state after invocation, invocation shape, and effective
observable-event set.

Each check records its own status. A check that requires an event type the
selected runner cannot observe is `skipped`, not `failed`. A fixture is
`passed` only when all required checks pass, `failed` when any observable
required check fails, and `skipped` when one or more required checks are
unobservable and none fails.

Every behavioral result is one of:

- `passed`;
- `failed`;
- `skipped` because the case does not support the selected runtime;
- `unavailable` because no supported authenticated runtime is present.

Structural validation always runs. Behavioral validation is required for a
release only when CI or the release environment declares at least one supported
authenticated runner. Otherwise the release report must show it as unavailable
and may not claim the behavioral fixtures passed.

### 13.4 Transaction tests

- apply followed by rollback restores byte-identical files and modes;
- changed precondition hash aborts before mutation;
- simulated failure midway restores earlier operations;
- external edits after apply block automatic overwrite;
- proposal-only operations never write;
- atomic relocation pairs are rejected when incomplete or internally
  inconsistent and roll back as a unit after a simulated partial failure;
- a broken symlink is reported and not silently replaced;
- Windows fallback fixtures omit POSIX mode assertions and exercise a
  non-symlink installation strategy.

### 13.5 Lugos Orca behavior evidence

The Orca behavior boundary is import-only. APU validates an Autowork
`lugos.autowork.behavior-delegation-receipt`, verifies its canonical receipt
hash, and projects it into
`lugos.apu.behavior-evaluation-evidence`. The version-1 projection accepts only
adaptation tiers 0–2 and stores no prompt, response, reasoning, command, tool
body, environment, credential, or free-text rejection reason.

Behavior evidence is content-addressed by the canonical JSON body excluding
`evidence_id`. A registry candidate patch is likewise content-addressed by its
body excluding `proposal_id`. Candidate operations carry canonical value hashes
and, for replacements or removals, the prior value hash. Operations may target
only `/behavior/...`, must have unique non-overlapping paths, and cannot contain
private fields or credential-shaped strings.

Every `lugos.apu.lugos-orca.behavior-registry-candidate-patch` must:

- name the exact full Lugos Orca commit and behavior-tree SHA-256 it targets;
- cite at least one validated supporting evidence record from that exact
  revision;
- set `requires_review` to `true` and `apply_authorized` to `false`; and
- remain a proposal only. APU does not launch a provider for this contract and
  does not expose an operation that applies the proposal.

The checked-in JSON schemas use JSON Schema draft 2020-12 and disallow unknown
properties at every defined object boundary. These contracts extend but do not
replace or alter APU execution evidence version 1.

## 14. Acceptance criteria

The v0.1 MVP is complete when:

1. `apu audit` inventories Codex and Claude global/project surfaces without
   modifying them, including supported Claude local files, rule applicability,
   and `@path` imports.
2. The effective precedence stack can be shown for an arbitrary working
   directory.
3. The deterministic and heuristic classifier identifies seeded universal
   triggers, per-task review loops, stale environment-pattern candidates, and a
   mechanical-enforcement candidate while labeling semantic judgments as
   agent-assisted or manual.
4. `apu propose` creates a deterministic JSON plan with exact preconditions.
5. `apu review` can accept, reject, edit, relocate, and defer operations.
6. Approval-state tests prove that rejected operations may coexist with
   approved operations, pending or deferred operations block approval, and
   `apu apply` executes only approved operations, refuses stale preconditions,
   and creates backups and a receipt.
7. `apu rollback` restores byte-identical fixtures.
8. Codex and Claude adapters install the shared optimizer skill using canonical
   sources rather than versioned caches, using explicit reviewed
   `symlink`/`configure` operations and a visible platform fallback.
9. Structural checks pass unconditionally. Balanced behavioral fixtures,
   including seeded-defect detection, pass when a supported authenticated agent
   runtime is available; otherwise they are reported as unavailable and are
   never represented as passed. Event-dependent checks are skipped when the
   selected runner does not declare the required observation capability.
10. Exported trace reports contain metrics but no prompt bodies or environment
    values.
11. The tool installs and runs on macOS, Linux, and Windows with Python 3.11+,
    using capability-tested platform fallbacks for symlinks, POSIX modes, and
    file replacement.
12. The package has no required model API, database, daemon, or web-service
    dependency. Optional behavioral evaluation may use an independently
    installed and authenticated agent CLI.
13. `apu outcome record` stores local monitoring records, and `apu status`
    reports elapsed days and material-task progress toward the 30-day/10-task
    window.
14. `apu validate` without an explicit plan or receipt validates every active
    installation in the local registry.
15. Provider execution evidence is append-only, content-minimized, idempotent
    for repeated source records, request/result-correlated where the provider
    exposes an identifier, and replay-verifiable for bounded Codex transcript
    prefixes even after the source file grows.
16. `apu behavior audit` defaults to the current project, seven days, twenty
    sessions, and 256 MiB; prioritizes incident-marked sessions; never offers an
    unbounded history mode; labels source integrity honestly; suppresses
    barrier-explained incident findings; and emits no provider content.

## 15. Delivery sequence

### Milestone 1: Read-only foundation

- data models;
- state-home and registry initialization;
- Codex and Claude discovery;
- Claude local-file, rule-applicability, import, skill, session-hook, and local
  marketplace discovery;
- precedence mapping;
- deterministic findings;
- sanitized JSON/text reports;
- synthetic fixtures and tests;
- GitHub Actions matrix for Python 3.11+ on macOS, Linux, and Windows.

### Milestone 2: Proposal and wizard

- residency classifier;
- plan schema;
- proposal generation;
- interactive review;
- approval-state transition tests;
- atomic relocation-pair generation and validation;
- diff rendering;
- no mutation beyond explicit plan output.

### Milestone 3: Transactional installation

- sidecar and managed-section strategies;
- backups and receipts;
- apply and rollback;
- drift detection;
- platform capability probes and Windows fallbacks;
- canonical optimizer-skill installation for Codex and Claude through explicit
  symlink/configure plan operations;
- provider postflight checks;
- `apu init` opt-in continuation through ownership, apply, structural
  validation, and postflight.

### Milestone 4: Evaluation and packaging

- balanced behavioral fixtures;
- optional Codex and Claude CLI runners with unavailable-state reporting;
- runner capability maps and normalized event handling;
- seeded-defect evaluation;
- local outcome recording and monitoring summaries;
- `apu init` opt-in behavioral validation and monitoring initialization;
- bundled optimizer skill and templates;
- `pipx`/`uv tool` installation;
- versioned GitHub release artifact;
- concise operator documentation.

No milestone introduces a dashboard, database, background service, or automatic
review-agent loop. A Superpowers adapter remains outside the v0.1 package,
acceptance criteria, and delivery sequence.
