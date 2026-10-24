from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from apu.evidence import append_evidence_events, normalize_hook_event
from apu.evidence_retention import apply_retention, preview_retention

NOW = datetime(2026, 10, 24, tzinfo=UTC)
OLD = datetime(2026, 8, 1, tzinfo=UTC)


def evidence(home, session="old", version=3, timestamp=OLD):
    event = normalize_hook_event(
        "codex",
        "PreToolUse",
        {"session_id": session},
        observed_at=timestamp.isoformat(),
        schema_version=version,
    )
    path, _ = append_evidence_events(home, [event])
    os.utime(path, (timestamp.timestamp(), timestamp.timestamp()))
    return path, event


def test_preview_is_read_only_and_apply_is_receipted_and_idempotent(tmp_path):
    home = tmp_path / "state"
    path, _ = evidence(home)
    provider_log = tmp_path / "provider.jsonl"
    provider_log.write_text("private provider history")
    before = sorted(str(p) for p in home.rglob("*"))
    plan = preview_retention(home, now=NOW)
    assert len(plan["candidates"]) == 1
    assert path.exists()
    assert sorted(str(p) for p in home.rglob("*")) == before
    receipt = apply_retention(home, plan, now=NOW)
    assert receipt["status"] == "complete"
    assert receipt["removed_files"] == 1
    assert not path.exists()
    assert provider_log.read_text() == "private provider history"
    assert apply_retention(home, plan, now=NOW) == receipt


@pytest.mark.parametrize("reference", ["event", "session", "path", "prose"])
def test_references_pin_every_version_of_session(tmp_path, reference):
    path, event = evidence(tmp_path, version=1)
    other, _ = evidence(tmp_path, version=3)
    ref = {
        "event": event["event_id"],
        "session": event["session_id"],
        "path": str(path),
        "prose": f"Investigate {event['event_id']} before retiring this record.",
    }[reference]
    (tmp_path / "saved.json").write_text(json.dumps({"evidence": ref}))
    plan = preview_retention(tmp_path, now=NOW)
    assert plan["candidates"] == []
    assert len(plan["retained"]) == 2
    assert other.exists()


def test_recent_append_pins_old_versions_and_unknown_data_is_retained(tmp_path):
    evidence(tmp_path, version=1)
    evidence(tmp_path, version=3, timestamp=NOW)
    malformed = tmp_path / "behavior/evidence/codex/bad.jsonl"
    malformed.write_text("not JSON")
    plan = preview_retention(tmp_path, now=NOW)
    assert not plan["candidates"]
    assert len(plan["retained"]) == 3


@pytest.mark.parametrize("change", ["append", "reference", "replace"])
def test_apply_refuses_stale_preview_without_deleting(tmp_path, change):
    path, event = evidence(tmp_path)
    plan = preview_retention(tmp_path, now=NOW)
    if change == "append":
        evidence(tmp_path, timestamp=NOW)
    elif change == "reference":
        (tmp_path / "saved.json").write_text(json.dumps(event["event_id"]))
    else:
        path.write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        apply_retention(tmp_path, plan, now=NOW)
    assert path.exists()


def test_corrupt_references_and_scan_limits_fail_closed(tmp_path):
    path, _ = evidence(tmp_path)
    with pytest.raises(ValueError, match="limit"):
        preview_retention(tmp_path, now=NOW, max_bytes=10)
    (tmp_path / "broken.json").write_text("{")
    with pytest.raises(ValueError, match="reference"):
        preview_retention(tmp_path, now=NOW)
    assert path.exists()


def test_empty_preview_creates_no_state(tmp_path):
    home = tmp_path / "absent"
    assert preview_retention(home, now=NOW)["candidates"] == []
    assert not home.exists()


@pytest.mark.parametrize("plan", [None, [], {}, {"schema_version": 2}])
def test_invalid_plans_are_rejected_without_state_creation(tmp_path, plan):
    home = tmp_path / "absent"
    with pytest.raises(ValueError):
        apply_retention(home, plan, now=NOW)
    assert not home.exists()


def test_modified_plan_cannot_delete_arbitrary_file(tmp_path):
    path, _ = evidence(tmp_path)
    plan = preview_retention(tmp_path, now=NOW)
    plan["candidates"][0]["path"] = "../outside.jsonl"
    with pytest.raises(ValueError):
        apply_retention(tmp_path, plan, now=NOW)
    assert path.exists()


def test_interrupted_deletion_resumes_without_repeating_removed_files(
    tmp_path, monkeypatch
):
    first, _ = evidence(tmp_path, session="first")
    second, _ = evidence(tmp_path, session="second")
    plan = preview_retention(tmp_path, now=NOW)
    original = Path.unlink
    deleted = []

    def interrupt(path, *args, **kwargs):
        if path.suffix == ".jsonl":
            if deleted:
                raise OSError("simulated interruption")
            deleted.append(path)
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupt)
        with pytest.raises(OSError, match="interruption"):
            apply_retention(tmp_path, plan, now=NOW)
    assert first.exists() != second.exists()
    receipt = apply_retention(tmp_path, plan, now=NOW)
    assert receipt["removed_files"] == 2
    assert not first.exists() and not second.exists()


def test_lock_excludes_other_threads_and_processes_and_releases_afterward(tmp_path):
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor

    from apu.behavior_lock import behavior_lock

    path, _ = evidence(tmp_path)
    plan = preview_retention(tmp_path, now=NOW)
    source = str(Path(__file__).resolve().parents[1] / "src")
    script = "import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from apu.behavior_lock import behavior_lock;\nwith behavior_lock(Path(sys.argv[2])): print('acquired')"
    with behavior_lock(tmp_path):
        with ThreadPoolExecutor() as pool, pytest.raises(RuntimeError, match="busy"):
            pool.submit(append_evidence_events, tmp_path, []).result()
        result = subprocess.run(
            [sys.executable, "-c", script, source, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0 and "busy" in result.stderr
        assert path.exists()
    assert apply_retention(tmp_path, plan, now=NOW)["status"] == "complete"


def test_cli_saves_and_applies_preview(tmp_path, monkeypatch, capsys):
    from apu.cli import main

    home = tmp_path / "state"
    path, _ = evidence(home, timestamp=datetime(2020, 1, 1, tzinfo=UTC))
    monkeypatch.setenv("APU_HOME", str(home))
    plan = tmp_path / "plan.json"
    assert main(["behavior", "prune", "--out", str(plan), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["candidates"]) == 1
    assert path.exists()
    assert main(["behavior", "prune", "--apply", str(plan), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["removed_files"] == 1
    assert not path.exists()


def test_links_are_never_followed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "state"
    path, _ = evidence(home)
    link = home / "linked"
    if os.name == "nt":
        import subprocess

        # Directory junctions work without developer mode or elevated rights.
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="linked"):
        preview_retention(home, now=NOW)
    assert path.exists()
