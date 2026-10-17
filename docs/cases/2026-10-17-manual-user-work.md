# Intervention case: repetitive manual user work

Status: matching global Codex and Claude guidance installed and adopted through
APU; installation verified, behavioral improvement not yet measured.

## Problem and evidence

The operator reported a Claude Code session repeatedly asking them to copy
commands into other windows, switch applications, run checks, and report results.
The task involved clipboard normalization, so some real copying and foreground
window interaction was necessary acceptance evidence.

Inspection of the local session showed multiple manual rerun requests, eventual
consolidation into guided scripts, and further reruns after defects in the test
helpers. The correction concerns the cumulative burden and preventable helper
failures; it does not establish that every requested test was unnecessary or that
a particular model caused the behavior. Exact session and snapshot line references
remain in the operator's local case record, outside this repository.

## Durable correction

The identical **Minimize Manual User Work** section in
[`templates/global-AGENTS.md`](../../templates/global-AGENTS.md) and
[`templates/global-CLAUDE.md`](../../templates/global-CLAUDE.md) preserves the
approved rule:

- Perform authorized routine work directly when tools permit it.
- Consolidate repetitive manual handoffs into tested automation proactively.
- Reserve user involvement for a concrete access, permission, or interaction need.
- Reuse valid evidence and rerun checks for specific uncertainties, relevant
  changes, or required acceptance checks.
- Finish once sufficient evidence supports the outcome, preserving authorization
  boundaries and necessary verification.

The operator approved installing this rule in both global instruction files and
then explicitly requested APU custody. The templates now retain the rule in
version control; updating the templates alone does not deploy it to other hosts.

## APU installation

APU 0.9.0 reviewed and applied two `merge` operations using the `full_file`
strategy, exact input/output hashes, and required backups. The installation ID is
`manual-user-work-policy-20261017`.

Both global files already contained the rule from the preceding approved direct
edit. The APU transaction therefore adopted their existing bytes without changing
content. Its backups include the rule: rollback restores the pre-adoption state,
not the state before the earlier direct edit.

The scoped inventory, draft and reviewed plans, full rendered instruction files,
source-session references, and validation output remain in the operator's local
case directory. The installation receipt and backups remain in APU private state
under `installations/manual-user-work-policy-20261017/`. No private transcript or
complete personal instruction file is included in this repository.

## Verification and definition of improvement

APU validation passed for the receipt schema and both installed target hashes.
An independent hash comparison confirmed that adoption preserved both files byte
for byte. The installed provider policy sections were also verified identical.

Behavioral improvement still requires observation in a relevant subsequent task:
the agent should consolidate repeated manual handoffs before being asked, retrieve
available results itself, explain necessary human actions, and retain required
acceptance evidence. Record a concrete failure if it repeats unnecessary handoffs
or skips a necessary check. No runtime success, automatic monitoring, or
model-specific improvement is claimed by this installation.
