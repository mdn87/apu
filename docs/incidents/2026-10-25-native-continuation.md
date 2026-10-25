# Claude native continuation qualification — October 25, 2026

APU now delivers an explicitly prepared continuation through a native Claude
panel button, with durable duplicate prevention and exact turn observations.
This closes the implementation gap recovered by the multiproject finishing
pass. It does not establish fleet rollout or recovery effectiveness.

## Contract and implementation

The optional `apu-session` plugin adds `/apu-continue` after `/apu-diagnose` and
`/apu-prepare`. Opening the panel submits nothing. A fresh button action checks
the current main session, directory and adapter generation, asks the portable
CLI to reserve the attempt, revalidates and admits it, then calls
`$.prompt.submit({text})`. It preserves plugin attribution and never executes
the prepared resume argv. No background service, policy edit, database migration
or global hook is introduced.

`native_continuation.py` uses APU's existing process lock and atomic artifact
writer. A deterministic key binds provider, session, normalized directory,
evidence hash and prompt hash. The durable reservation precedes returning the
prompt. A lost response, crash, repeat or reload cannot reclaim the same
evidence. Admission repeats the saved authority, current barrier, evidence and
identity checks. Unknown outcomes stay reserved without automatic replay.

Observations record the native turn ID. Out-of-order queued acknowledgments
cannot downgrade a started or ended turn. Model completion leaves
`outcome_verification: unverified`; the existing operator result command is the
separate recovery attestation mechanism.

## Runtime findings and corrections

Claude Code 2.1.296 rejects submission from `command.run`, even when the promise
is not awaited. The implementation therefore uses the supported `ui.press`
button callback. The same plugin's own `prompt.submit` middleware did not
intercept its submission in the runtime test, so admission runs directly in
the button handler before the effect.

Live `/apu-*` invocations append `system/local_command` invocation and output
rows. Comparing the complete raw transcript hash made the normal workflow
invalidate itself. The separate native evidence hash excludes only recognized
APU command rows; the complete raw hash remains on the incident/receipt. User or
model content resembling command markers, tool denials, other commands,
nonempty arguments, policy changes, invalid records and unknown bytes remain
evidence. The live journal showed a changed raw hash with an unchanged native
evidence hash after diagnosis.

The live provider wraps plugin prompts with attribution text before `turn.start`.
The adapter matches either the exact prompt or the complete wrapper observed in
2.1.296, then correlates the same turn ID at completion. It does not match a
substring of an unrelated turn. Unsupported wrapper formats leave observations
unconfirmed rather than guessing a match.

## Verification

- Python suite: **722 passed, 2 skipped** on Windows Python 3.11. The skips are
  POSIX file mode and unavailable Windows symlink creation.
- Native plugin tests: **19 passed** on Claude Code 2.1.296, covering terminal
  and Desktop render harnesses, exact binding, duplicate claims, admission
  refusal, stale callbacks, subagent views, unavailable submit responses and
  observation-write failure. Harness Desktop checks are not live Desktop proof.
- A new bookkeeping regression failed against the raw-hash implementation and
  passed after the evidence projection correction. The observed wrapped prompt
  likewise failed turn correlation before its correction.
- Independent review identified the evidence-hash and unsupported Button
  `disabled` issues. Ineligible views now expose no button and stale rendered
  actions cannot reserve.

The controlled live Windows CLI session was
`b683dd16-292a-40ac-8cd8-4680c75215f1`, launched with the development plugin and
`PYTHONPATH` pointing to its source. State was isolated under
`MyDocs/.local/finishing-execution/apu-native-live`. No installed global package
or watcher setting was changed for the proof.

The fixture explicitly instructed Claude not to use tools or change files,
to emit a known diagnostic phrase, and to answer a later continuation with
`Native continuation received.` This is a transport/observation test, not an
actual autonomy failure or a recovered production task.

Successful final receipt:

| Field | Observed value |
| --- | --- |
| Incident | `incident-126a4dd63e124b7f89c34fc64bf91eaa` |
| Diagnosis | `diagnosis-941b29d0a8fd40518d726bc6e0dbc1ee` |
| Intervention | `native-68345d04251d35cbbbf1943c63012c31e90aeedfcd2a222db1c67ea307b867c1` |
| Native turn | `e77173b7-aa9d-43a5-8e35-9c30bb52ebd4` |
| Started | `2026-10-25T21:04:11.309039Z` |
| Ended | `2026-10-25T21:04:14.308832Z`, reason `answer` |
| Final state | `turn-ended`, `executed: true`, `turn_completed: true` |
| Recovery | `unverified`; no operator result recorded |

The earlier live attempt delivered successfully but retained `queued` because
it preceded the wrapper correction. Its original receipt is preserved, not
rewritten as a success. One subsequent claim refused after additional policy
records changed the evidence; it produced no native reservation or prompt.
The operator then explicitly diagnosed the current evidence and ran the final
successful fixture. Both disposable CLI processes were exited after use.

## Supported boundary and remaining work

Use this optional panel in an idle main session and stay there until delivery
settles. The SDK has no expected-session argument or cancellation handle on
submit. Preflight checks reject observed session/directory/generation changes,
but a clear/resume/directory change while the native call is in flight is not
atomically protected. An unknown outcome does not prove another session could
not receive a prompt. Do not advertise that race as qualified or use this as an
unattended continuation transport.

Remaining: installed CLI/plugin rollout, a genuine recovery observation with
operator attestation, live Desktop and other operating-system qualification,
and operator-effort measurement. The 0.10.0 release milestone and waived I3
measurement remain closed. Mobile approvals and the other Codex session's
Autowork recovery/limits/attention/pilot batches are separate owners' work.
