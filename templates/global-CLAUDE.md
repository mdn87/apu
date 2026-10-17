# Global Claude Code guidance

- Keep process proportional to observed complexity and consequence.
- Use a plan for coupled or ambiguous work, not routine local edits.
- Delegate only independent work that benefits from parallelism or isolation.
- Run focused checks for changed behavior and one consolidated final review.
- Preserve explicit user requests and closer project instructions.

## Minimize Manual User Work

- Perform authorized commands and routine workflow steps yourself when your tools can do them. Ask the user to act when there is a concrete reason, such as unavailable access, required permission, or a real interaction the task needs to verify; explain that reason briefly.
- Track the cumulative burden of command copying, window switching, manual tests, and result reporting. When these become repetitive or burdensome, proactively automate the steps or provide one tested script that guides only the necessary human actions. Do not wait for the user to ask for consolidation.
- Validate test helpers before handing them to the user, collect results automatically where possible, and reuse valid evidence. Repeat a check only to resolve a specific uncertainty, verify a relevant change, or satisfy a required acceptance check; explain what the rerun will establish.
- Preserve necessary checks, but stop adding tests or manual steps once sufficient evidence supports the requested outcome. Finish the task within existing authorization; automation never expands permissions or bypasses genuine human approval.
