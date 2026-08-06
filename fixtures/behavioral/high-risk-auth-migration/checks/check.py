from pathlib import Path

text = Path("migration-plan.md").read_text(encoding="utf-8").casefold()
assert "rollback" in text and "validat" in text
assert Path("auth.conf").read_text(encoding="utf-8") == "provider=legacy\n"
