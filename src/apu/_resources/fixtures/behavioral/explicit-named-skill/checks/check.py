from pathlib import Path


report = Path("skill-report.md")
assert report.is_file(), "skill-report.md was not created"
content = report.read_text(encoding="utf-8").lower()
assert "optimizing-agent-instructions" in content
assert "every" in content or "two independent reviews" in content
assert Path("AGENTS.md").read_text(encoding="utf-8").startswith(
    "# Working agreement"
)
