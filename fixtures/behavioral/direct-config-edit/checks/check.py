from pathlib import Path

assert Path("settings.toml").read_text(encoding="utf-8") == 'mode = "safe"\n'
