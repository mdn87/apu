# Independent review request: Claude failure loop

This packet asks for an outside software-engineering opinion. It is not a code
review, and no source code or raw private transcript is included.

## The 60-second version

The operator said `enter apu-wtf`, intending Claude to run an existing failure
diagnostic command.

The recorded sequence shows:

1. Voice transcription delivered those exact words correctly.
2. Conversation compaction then summarized the request as an investigation of
   whether APU should contain the voice system.
3. Claude followed that summarized task instead of the original command.
4. Claude subsequently designed a replacement WTF feature and inferred intent
   from an unrelated repository name.
5. A custom start-of-turn approval gate forced each correction into another
   plan-and-wait cycle and eventually denied the requested report write.
6. At the time, the installed `apu-wtf` could inspect Codex sessions but not
   Claude sessions.

The current working hypothesis is that compaction introduced the initial
semantic error, request substitution continued it, and the approval gate
amplified it. Please treat that as a hypothesis to challenge, not a required
conclusion.

## Questions for the reviewer

1. What was the primary failure: command ambiguity, compaction fidelity, model
   behavior, gate design, voice triage, or an interaction among them?
2. Which factors caused the first error, and which merely prolonged it?
3. Is “request substitution” an accurate description of the model behavior?
4. What is the smallest robust change that would prevent the same loop?
5. What evidence would you need before accepting or rejecting the current
   hypothesis?

## Contents

- `START-HERE.md` — this neutral review request.
- `FULL-INCIDENT-REPORT.md` — the detailed timeline, evidence references,
  causal analysis, completed tooling repair, and remaining risks.

The original 11 MB Claude JSONL transcript is intentionally excluded because
it contains unrelated private conversation history. Four relevant record
numbers are identified in the full report and can be sanitized separately if
the reviewer needs primary evidence.
