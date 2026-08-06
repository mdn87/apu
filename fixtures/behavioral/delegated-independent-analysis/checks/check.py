from pathlib import Path

text = Path("analysis.txt").read_text(encoding="utf-8").casefold()
assert "alpha" in text and "beta" in text
