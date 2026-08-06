# APU v0.1 Product and Technical Specification

**Status:** Initial implementation specification  
**Repository:** `https://github.com/mdn87/apu`  
**Product name:** Agent Policy Updater  
**CLI command:** `apu`

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
- repository and ancestor `AGENTS.md` and `CLAUDE.md`;
- skill directories containing `SKILL.md`;
- Claude session-start hooks and local marketplace metadata;
- Codex/Claude-compatible shared skills under `~/.agents/skills`;
- explicit files or roots supplied by the user.

Provider adapters define names, precedence, import capability, managed-section
syntax, and installation behavior. The core engine must not hard-code one
provider’s paths.

### 2.2 Optional evidence sources

- Codex JSONL session directories;
- Git repositories and revision ranges;
- user-provided incident notes;
- previous APU installation receipts;
- outcome summaries from the monitoring window.

Trace analysis emits metadata and aggregate counts only. It must not copy
messages, prompts, command arguments, environment values, or secrets into the
report.

### 2.3 Out of scope for v0.1

- hosted synchronization;
- a web UI or dashboard;
- a database or background daemon;
- automatic model API calls;
- arbitrary semantic merging of every Markdown format;
- organization-wide policy enforcement;
- automatic modification of production repositories;
- packaging or vendoring the Superpowers framework.

## 3. Package layout

```text
apu/
├── pyproject.toml
├── src/apu/
│   ├── cli.py
│   ├── discovery.py
│   ├── precedence.py
│   ├── classify.py
│   ├── planning.py
│   ├── wizard.py
│   ├── apply.py
│   ├── rollback.py
│   ├── validate.py
│   ├── receipts.py
│   ├── models.py
│   └── adapters/
│       ├── base.py
│       ├── codex.py
│       ├── claude.py
│       └── superpowers.py
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
├── fixtures/
│   ├── direct/
│   ├── planned/
│   ├── delegated/
│   └── high-risk/
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
  "mode": "0644",
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
  "surface_id": "sha256:...",
  "location": {"line": 17},
  "category": "universal-skill-trigger",
  "severity": "high",
  "confidence": "high",
  "evidence": ["matched-rule", "trace:019f..."],
  "summary": "Skill invocation is required for every conversation."
}
```

Initial categories:

- universal skill or tool trigger;
- unconditional design/approval gate;
- microtask planning or commit requirement;
- per-task implementer/reviewer loop;
- aggressive priority language;
- duplicated or contradictory instruction;
- stale environment or project fact;
- speculative completeness or impossible-state handling;
- test ritual detached from behavior risk;
- misplaced global or repository-specific fact;
- prose rule better enforced mechanically;
- unsupported or broken path/import/symlink;
- secret or sensitive material exposure risk.

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

### 4.4 Effective precedence

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

## 6. Command-line interface

```text
apu audit [PATH ...] [--sessions PATH ...] [--git-repo PATH]
          [--root-session-id ID] [--json OUTPUT]

apu propose --inventory INVENTORY.json [--output PLAN.json]

apu review PLAN.json

apu apply PLAN.json [--yes]

apu validate [--plan PLAN.json | --receipt RECEIPT.json]

apu rollback --receipt RECEIPT.json

apu status

apu init
```

### 6.1 Command behavior

`apu audit`

- is read-only;
- discovers surfaces and precedence;
- emits findings and sanitized evidence;
- writes only when an explicit output path is provided.

`apu propose`

- converts inventory and findings into operations;
- never mutates live instruction files;
- includes exact precondition hashes.

`apu review`

- starts the interactive review flow;
- modifies only the plan artifact;
- supports accepting, rejecting, editing, relocating, and deferring operations.

`apu apply`

- refuses an unapproved plan unless `--yes` is supplied;
- rechecks all precondition hashes;
- creates backups and an installation receipt;
- validates temporary results before atomic replacement.

`apu validate`

- checks structure, links, managed sections, plugin metadata, and representative
  behavioral fixtures;
- distinguishes passed, failed, skipped, and unavailable checks.

`apu rollback`

- verifies the receipt and current managed hashes;
- warns rather than overwriting user edits made after installation;
- restores byte-identical backups when safe.

`apu status`

- shows installed APU version, managed surfaces, drift, current provider
  adapters, last validation, and monitoring progress.

`apu init`

- runs audit, proposal, interactive review, validation, and optional apply as a
  guided first-run flow;
- defaults to stopping after proposal preview.

## 7. Interactive wizard

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
      "action": "preserve|merge|create|relocate|remove|symlink|configure",
      "target": "/absolute/path",
      "source": "template or source path",
      "ownership": "user|repository|apu",
      "strategy": "sidecar|managed_section|full_file|proposal_only",
      "precondition_sha256": "...",
      "proposed_sha256": "...",
      "backup_required": true,
      "requires_confirmation": true,
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

## 10. Transaction and rollback

Before applying:

1. resolve every target without following an unexpected changed symlink;
2. verify precondition hashes and file modes;
3. verify the plan’s approval state;
4. create a private temporary transaction directory;
5. copy original bytes and metadata into the transaction;
6. render all proposed outputs;
7. parse and validate rendered outputs.

Commit the transaction by atomically replacing files where the platform permits
it. If any operation fails, restore already-applied operations in reverse order.

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

## 11. Provider adapters

An adapter implements:

```python
class ProviderAdapter:
    def discover(self, roots): ...
    def precedence(self, cwd, surfaces): ...
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
- uses managed-section or proposal-only installation where imports are not
  supported.

### 11.2 Claude adapter

- discovers global and hierarchical `CLAUDE.md`;
- discovers `.claude/rules`, skills, hooks, and marketplaces;
- prefers a sidecar import when safe;
- configures canonical local marketplace sources rather than editing caches;
- reports when a restart is required.

### 11.3 Superpowers adapter

- accepts an explicit canonical checkout;
- checks branch, cleanliness, symlinks, and installed version;
- scans active skills and session-start injection;
- proposes patches or validates an already revised fork;
- never rebases, commits, or pushes without explicit authorization.

## 12. Privacy and safety

- Audit is read-only.
- Exported inventory defaults to paths, hashes, categories, and counts.
- Prompt and message bodies are not emitted from session traces.
- Environment variables, tokens, cookies, and credential-shaped values are
  redacted or omitted.
- Backups and receipts use user-only permissions where supported.
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

### 13.3 Transaction tests

- apply followed by rollback restores byte-identical files and modes;
- changed precondition hash aborts before mutation;
- simulated failure midway restores earlier operations;
- external edits after apply block automatic overwrite;
- proposal-only operations never write;
- a broken symlink is reported and not silently replaced.

## 14. Acceptance criteria

The v0.1 MVP is complete when:

1. `apu audit` inventories Codex and Claude global/project surfaces without
   modifying them.
2. The effective precedence stack can be shown for an arbitrary working
   directory.
3. The classifier identifies seeded universal triggers, per-task review loops,
   stale environment facts, and a mechanical-enforcement candidate.
4. `apu propose` creates a deterministic JSON plan with exact preconditions.
5. `apu review` can accept, reject, edit, relocate, and defer operations.
6. `apu apply` creates backups and a receipt and refuses stale preconditions.
7. `apu rollback` restores byte-identical fixtures.
8. Codex and Claude adapters install the shared optimizer skill using canonical
   sources rather than versioned caches.
9. Structural and balanced behavioral fixtures pass, including seeded-defect
   detection.
10. Exported trace reports contain metrics but no prompt bodies or environment
    values.
11. The tool installs and runs on macOS, Linux, and Windows with Python 3.11+.
12. The package has no required model API, database, daemon, or web-service
    dependency.

## 15. Delivery sequence

### Milestone 1: Read-only foundation

- data models;
- Codex and Claude discovery;
- precedence mapping;
- deterministic findings;
- sanitized JSON/text reports;
- synthetic fixtures and tests.

### Milestone 2: Proposal and wizard

- residency classifier;
- plan schema;
- proposal generation;
- interactive review;
- diff rendering;
- no mutation beyond explicit plan output.

### Milestone 3: Transactional installation

- sidecar and managed-section strategies;
- backups and receipts;
- apply and rollback;
- drift detection;
- provider postflight checks.

### Milestone 4: Evaluation and packaging

- balanced behavioral fixtures;
- seeded-defect evaluation;
- bundled optimizer skill and templates;
- `pipx`/`uv tool` installation;
- versioned GitHub release artifact;
- concise operator documentation.

No milestone introduces a dashboard, database, background service, or automatic
review-agent loop.
