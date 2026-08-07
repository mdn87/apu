from pathlib import Path

text = Path("finding.md").read_text(encoding="utf-8").casefold()
assert "limit.py" in text
assert "10" in text
assert any(word in text for word in ("exclusive", "boundary", "< 10"))
