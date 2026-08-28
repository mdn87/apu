from pathlib import Path

assert "validated" in Path("implementation.txt").read_text(encoding="utf-8")
