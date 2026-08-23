# Repository guidance

Document only stable repository facts that an agent cannot reliably infer:

- build, test, and validation commands;
- architecture and domain invariants;
- repository-specific safety constraints;
- test-environment preconditions and platform assumptions -- interpreter
  floor, checkout depth, sibling repositories or services the suite needs,
  and any OS-specific behaviour a test depends on;
- completion criteria required by this project.

Keep reusable methods in skills and mechanical invariants in tooling.
