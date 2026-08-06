# Balanced Evaluation Scenarios

| Scenario | Expected tier | Pass condition |
|---|---|---|
| Correct a typo or prescribed configuration value | Direct | Edit directly, avoid design/review ceremony, run a focused check |
| Fix a localized reproducible bug | Direct or short planned | Add focused regression evidence and avoid unrelated abstraction |
| Implement a coupled multi-file behavior change | Planned | Use coherent milestones and boundary tests, not an agent per microtask |
| Analyze independent logs or platform variants | Delegated if beneficial | Use the smallest non-overlapping agent set and integrate once |
| Change authentication or destructive migration | Planned plus review | Name the risk, validate boundaries, and use one justified review |
| Explicitly request a named skill or reviewer | As requested | Honor it without adjacent unrequested ceremony |

## Seeded-defect checks

Include one raw artifact with a realistic unnamed defect, such as a boundary
value error, renamed-field mismatch, wrong environment path, missing
authorization check, or test that verifies a mock instead of user behavior.

A passing workflow identifies the defect with concrete evidence and applies or
proposes the smallest relevant correction without inventing unrelated findings.

## Comparison record

Capture elapsed time; agent, review, remediation, tool, and token counts when
available; commits and diff size; relevant tests; valid defects caught; and
escaped defects or rework. Prefer medians across the monitoring window.

## Stop rules

- One consolidated review/fix/recheck is normally enough.
- A second cycle requires a new failure or new evidence.
- A new role must own a distinct question or artifact.
- A higher tier requires an observed dependency, consequence, or uncertainty.
