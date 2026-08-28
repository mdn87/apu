from __future__ import annotations

import json
from pathlib import Path

import pytest

from apu.guidance import (
    FetchResponse,
    GuidanceError,
    adopt_guidance_baseline,
    detector_policy_from_baseline,
    diff_guidance_baselines,
    guidance_evaluation_stamp,
    load_current_guidance_baseline,
    load_guidance_detector_policy,
    read_guidance_work_order_snapshot,
    refresh_guidance,
    write_guidance_distillation_work_order,
)
from apu.models import canonical_json, sha256_json

NOW = "2026-08-07T00:15:00-04:00"
LATER = "2026-08-08T00:15:00-04:00"
URL = "https://example.test/agent-guidance"


def _refresh(state_home: Path, content: bytes = b"Prefer scoped guidance.\n"):
    return refresh_guidance(
        state_home,
        [URL],
        fetcher=lambda _url: FetchResponse(content, "text/plain"),
        retrieved_at=NOW,
    )


def _candidate(work_order: dict, refresh: dict, *, suffix: str = "") -> dict:
    source = refresh["sources"][0]
    return {
        "schema_version": 1,
        "artifact_type": "guidance-baseline-candidate",
        "work_order_id": work_order["work_order_id"],
        "principles": [
            {
                "principle_id": f"scoped-guidance{suffix}",
                "statement": "Keep instructions at their narrowest useful scope.",
                "sources": [
                    {
                        "source_url": source["source_url"],
                        "retrieved_at": source["retrieved_at"],
                        "content_sha256": source["content_sha256"],
                    }
                ],
                "detector_policies": [
                    {
                        "detector_id": "duplicate-instruction",
                        "setting": "minimum_words",
                        "value": 4,
                        "justification": (
                            "The cited guidance treats repeated concise "
                            "instructions as duplication."
                        ),
                        "source_sha256s": [source["content_sha256"]],
                    }
                ],
            }
        ],
    }


def _approval() -> dict:
    return {
        "status": "approved",
        "reviewer": "reviewer-1",
        "reviewed_at": NOW,
    }


def _adopted_baseline(tmp_path: Path) -> tuple[dict, dict, dict]:
    refresh = _refresh(tmp_path)
    work_order = write_guidance_distillation_work_order(tmp_path, refresh)
    baseline = adopt_guidance_baseline(
        tmp_path,
        _candidate(work_order, refresh),
        approval=_approval(),
        adopted_at=LATER,
    )
    return refresh, work_order, baseline


def test_guidance_evaluation_stamp_is_read_only_when_unconfigured(
    tmp_path: Path,
) -> None:
    state_home = tmp_path / "missing-state"

    assert guidance_evaluation_stamp(state_home) == {
        "version": None,
        "status": "unconfigured",
        "retrieved_at": None,
        "artifact_sha256": None,
    }
    assert not state_home.exists()


def test_guidance_evaluation_stamp_reports_valid_adopted_baseline(
    tmp_path: Path,
) -> None:
    refresh, _work_order, baseline = _adopted_baseline(tmp_path)

    stamp = guidance_evaluation_stamp(tmp_path)

    assert stamp == {
        "version": baseline["baseline_version"],
        "status": "adopted",
        "retrieved_at": refresh["sources"][0]["retrieved_at"],
        "artifact_sha256": sha256_json(baseline),
    }
    assert set(stamp) == {
        "version",
        "status",
        "retrieved_at",
        "artifact_sha256",
    }


def test_guidance_evaluation_stamp_marks_degraded_source_stale(
    tmp_path: Path,
) -> None:
    _refresh_value, _work_order, baseline = _adopted_baseline(tmp_path)

    refresh_guidance(
        tmp_path,
        [URL],
        fetcher=lambda _url: (_ for _ in ()).throw(ConnectionError("dead")),
        retrieved_at=LATER,
    )

    stamp = guidance_evaluation_stamp(tmp_path)
    assert stamp["version"] == baseline["baseline_version"]
    assert stamp["status"] == "stale"


def test_guidance_evaluation_stamp_detects_changed_but_not_identical_refetch(
    tmp_path: Path,
) -> None:
    _adopted_baseline(tmp_path)

    refresh_guidance(
        tmp_path,
        [URL],
        fetcher=lambda _url: FetchResponse(b"Prefer scoped guidance.\n", "text/plain"),
        retrieved_at=LATER,
    )
    assert guidance_evaluation_stamp(tmp_path)["status"] == "adopted"

    refresh_guidance(
        tmp_path,
        [URL],
        fetcher=lambda _url: b"Materially changed guidance.",
        retrieved_at="2026-08-09T00:15:00-04:00",
    )
    assert guidance_evaluation_stamp(tmp_path)["status"] == "stale"


def test_guidance_evaluation_stamp_validates_immutable_pair(
    tmp_path: Path,
) -> None:
    _refresh_value, _work_order, baseline = _adopted_baseline(tmp_path)
    current_path = tmp_path / "guidance" / "baselines" / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["adopted_at"] = "2026-08-10T00:15:00-04:00"
    current_path.write_text(canonical_json(current), encoding="utf-8")

    with pytest.raises(GuidanceError, match="immutable artifact"):
        guidance_evaluation_stamp(tmp_path)

    assert baseline["baseline_version"] == current["baseline_version"]


def test_refresh_uses_injected_fetcher_and_stores_dated_raw_snapshot(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    contents = {
        "https://b.example.test/guide": b"beta\r\n",
        "https://a.example.test/guide": b"alpha\x00raw",
    }

    refresh = refresh_guidance(
        tmp_path,
        list(contents),
        fetcher=lambda url: (calls.append(url), contents[url])[1],
        retrieved_at=NOW,
    )

    assert calls == sorted(contents)
    assert [item["source_url"] for item in refresh["sources"]] == sorted(contents)
    assert all(item["status"] == "fresh" for item in refresh["sources"])
    for source in refresh["sources"]:
        object_path = (
            tmp_path / "guidance" / "objects" / f"{source['content_sha256']}.bin"
        )
        assert object_path.read_bytes() == contents[source["source_url"]]
        assert source["retrieved_at"] == NOW

    stored = tmp_path / "guidance" / "refreshes" / f"{refresh['refresh_id']}.json"
    assert stored.read_text(encoding="utf-8") == canonical_json(refresh)
    assert not list(stored.parent.glob(f".{stored.name}.*"))


def test_failed_refresh_marks_previous_snapshot_stale_without_error_detail(
    tmp_path: Path,
) -> None:
    first = _refresh(tmp_path)

    def fail(_url: str) -> bytes:
        raise RuntimeError("credential-shaped detail sk-live-must-not-persist")

    second = refresh_guidance(
        tmp_path,
        [URL],
        fetcher=fail,
        retrieved_at=LATER,
    )

    source = second["sources"][0]
    assert source["status"] == "stale"
    assert source["error_code"] == "RuntimeError"
    assert source["last_success"] == {
        "source_url": URL,
        "retrieved_at": NOW,
        "content_sha256": first["sources"][0]["content_sha256"],
        "media_type": "text/plain",
    }
    all_state = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (tmp_path / "guidance").rglob("*")
        if path.is_file()
    )
    assert "sk-live-must-not-persist" not in all_state


def test_first_failed_refresh_is_visibly_unavailable(tmp_path: Path) -> None:
    refresh = refresh_guidance(
        tmp_path,
        [URL],
        fetcher=lambda _url: (_ for _ in ()).throw(OSError("dead")),
        retrieved_at=NOW,
    )

    assert refresh["sources"][0] == {
        "source_url": URL,
        "status": "unavailable",
        "retrieved_at": NOW,
        "error_code": "OSError",
        "last_success": None,
    }


def test_partial_refresh_records_healthy_and_failed_sources_independently(
    tmp_path: Path,
) -> None:
    healthy = "https://healthy.example.test/guide"
    dead = "https://dead.example.test/guide"

    def fetch(url: str) -> bytes:
        if url == dead:
            raise ConnectionError("not reachable")
        return b"healthy guidance"

    refresh = refresh_guidance(
        tmp_path,
        [dead, healthy],
        fetcher=fetch,
        retrieved_at=NOW,
    )

    by_url = {source["source_url"]: source for source in refresh["sources"]}
    assert by_url[healthy]["status"] == "fresh"
    assert by_url[dead]["status"] == "unavailable"
    assert by_url[dead]["error_code"] == "ConnectionError"
    assert (
        tmp_path / "guidance" / "objects" / f"{by_url[healthy]['content_sha256']}.bin"
    ).read_bytes() == b"healthy guidance"


def test_work_order_excludes_source_prose_and_reviewed_candidate_is_adopted(
    tmp_path: Path,
) -> None:
    secret_source_text = b"guidance including private-token-1234567890"
    refresh = _refresh(tmp_path, secret_source_text)
    work_order = write_guidance_distillation_work_order(tmp_path, refresh)
    encoded_work_order = canonical_json(work_order)

    assert secret_source_text.decode() not in encoded_work_order
    assert work_order["candidate_schema"]["fixed_values"] == {
        "schema_version": 1,
        "artifact_type": "guidance-baseline-candidate",
    }
    assert work_order["instructions"]
    assert work_order["acceptance_criteria"]
    access = work_order["private_snapshot_access"]
    assert access["mode"] == "read-only"
    assert access["resolver"] == ("apu.guidance.read_guidance_work_order_snapshot")
    snapshot_ref = work_order["sources"][0]["snapshot"]["ref"]
    assert access["allowed_refs"] == [snapshot_ref]
    assert (
        read_guidance_work_order_snapshot(tmp_path, work_order, snapshot_ref)
        == secret_source_text
    )
    with pytest.raises(GuidanceError, match="not allowed"):
        read_guidance_work_order_snapshot(
            tmp_path,
            work_order,
            "guidance/objects/" + "f" * 64 + ".bin",
        )

    candidate = _candidate(work_order, refresh)
    baseline = adopt_guidance_baseline(
        tmp_path,
        candidate,
        approval=_approval(),
        adopted_at=LATER,
    )

    assert baseline["principles"][0]["sources"][0] == {
        "source_url": URL,
        "retrieved_at": NOW,
        "content_sha256": refresh["sources"][0]["content_sha256"],
    }
    policy = baseline["principles"][0]["detector_policies"][0]
    assert policy["setting"] == "minimum_words"
    assert policy["value"] == 4
    assert policy["justification"].startswith("The cited guidance")
    assert (
        detector_policy_from_baseline(baseline).duplicate_instruction_minimum_words == 4
    )
    assert (
        load_guidance_detector_policy(tmp_path).duplicate_instruction_minimum_words == 4
    )
    assert load_current_guidance_baseline(tmp_path) == baseline
    stored = (
        tmp_path / "guidance" / "baselines" / f"{baseline['baseline_version']}.json"
    )
    assert json.loads(stored.read_text(encoding="utf-8")) == baseline
    assert stored.read_text(encoding="utf-8") == canonical_json(baseline)


def test_adoption_requires_approval_and_provenance_bearing_snapshot(
    tmp_path: Path,
) -> None:
    refresh = _refresh(tmp_path)
    work_order = write_guidance_distillation_work_order(tmp_path, refresh)
    candidate = _candidate(work_order, refresh)

    with pytest.raises(GuidanceError, match="approved review"):
        adopt_guidance_baseline(
            tmp_path,
            candidate,
            approval={**_approval(), "status": "pending"},
            adopted_at=LATER,
        )

    candidate["principles"][0]["sources"][0]["content_sha256"] = "f" * 64
    candidate["principles"][0]["detector_policies"][0]["source_sha256s"] = ["f" * 64]
    with pytest.raises(GuidanceError, match="outside its work order"):
        adopt_guidance_baseline(
            tmp_path,
            candidate,
            approval=_approval(),
            adopted_at=LATER,
        )

    candidate = _candidate(work_order, refresh)
    content_sha256 = refresh["sources"][0]["content_sha256"]
    (tmp_path / "guidance" / "objects" / f"{content_sha256}.bin").write_bytes(
        b"corrupted"
    )
    with pytest.raises(GuidanceError, match="not present in APU state"):
        adopt_guidance_baseline(
            tmp_path,
            candidate,
            approval=_approval(),
            adopted_at=LATER,
        )


def test_adoption_rejects_secret_shaped_distillation(tmp_path: Path) -> None:
    refresh = _refresh(tmp_path)
    work_order = write_guidance_distillation_work_order(tmp_path, refresh)
    candidate = _candidate(work_order, refresh)
    candidate["principles"][0]["statement"] = (
        "Use this key: sk-proj-abcdefghijklmnopqrstuvwxyz"
    )

    with pytest.raises(GuidanceError, match="credential-shaped"):
        adopt_guidance_baseline(
            tmp_path,
            candidate,
            approval=_approval(),
            adopted_at=LATER,
        )
    assert not (tmp_path / "guidance" / "baselines" / "current.json").exists()


def test_baseline_diff_is_deterministic_and_reports_semantic_changes(
    tmp_path: Path,
) -> None:
    refresh = _refresh(tmp_path)
    work_order = write_guidance_distillation_work_order(tmp_path, refresh)
    before = adopt_guidance_baseline(
        tmp_path,
        _candidate(work_order, refresh),
        approval=_approval(),
        adopted_at=NOW,
    )

    candidate = _candidate(work_order, refresh)
    candidate["principles"][0]["statement"] = "Changed statement."
    added = json.loads(canonical_json(candidate["principles"][0]))
    added["principle_id"] = "added-principle"
    candidate["principles"].insert(0, added)
    changed = adopt_guidance_baseline(
        tmp_path,
        candidate,
        approval=_approval(),
        adopted_at=LATER,
    )

    delta = diff_guidance_baselines(before, changed)

    assert [item["principle_id"] for item in delta["added"]] == ["added-principle"]
    assert delta["removed"] == []
    assert [item["principle_id"] for item in delta["changed"]] == ["scoped-guidance"]
    assert delta == diff_guidance_baselines(before, changed)


def test_baseline_version_ignores_unchanged_later_retrieval(
    tmp_path: Path,
) -> None:
    first_refresh, first_work_order, first = _adopted_baseline(tmp_path)
    later_refresh = refresh_guidance(
        tmp_path,
        [URL],
        fetcher=lambda _url: FetchResponse(b"Prefer scoped guidance.\n", "text/plain"),
        retrieved_at="2026-08-09T00:15:00-04:00",
    )
    later_work_order = write_guidance_distillation_work_order(
        tmp_path,
        later_refresh,
    )
    later = adopt_guidance_baseline(
        tmp_path,
        _candidate(later_work_order, later_refresh),
        approval={
            **_approval(),
            "reviewed_at": "2026-08-09T00:20:00-04:00",
        },
        adopted_at="2026-08-09T00:30:00-04:00",
    )

    assert (
        later_refresh["sources"][0]["retrieved_at"]
        != (first_refresh["sources"][0]["retrieved_at"])
    )
    assert later_work_order["work_order_id"] != first_work_order["work_order_id"]
    assert later["baseline_version"] == first["baseline_version"]
    assert later == first
    assert diff_guidance_baselines(first, later)["changed"] == []


def test_strict_schemas_and_refresh_inputs_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(GuidanceError, match="embedded credentials"):
        refresh_guidance(
            tmp_path,
            ["https://user:secret@example.test/guide"],
            fetcher=lambda _url: b"",
            retrieved_at=NOW,
        )
    with pytest.raises(GuidanceError, match="must be unique"):
        refresh_guidance(
            tmp_path,
            [URL, URL],
            fetcher=lambda _url: b"",
            retrieved_at=NOW,
        )

    refresh = _refresh(tmp_path)
    work_order = write_guidance_distillation_work_order(tmp_path, refresh)
    candidate = _candidate(work_order, refresh)
    candidate["unexpected"] = True
    with pytest.raises(GuidanceError, match="fields must be exactly"):
        adopt_guidance_baseline(
            tmp_path,
            candidate,
            approval=_approval(),
            adopted_at=LATER,
        )


def test_detector_policy_schema_rejects_unknown_or_wrong_typed_settings(
    tmp_path: Path,
) -> None:
    refresh = _refresh(tmp_path)
    work_order = write_guidance_distillation_work_order(tmp_path, refresh)
    candidate = _candidate(work_order, refresh)
    policy = candidate["principles"][0]["detector_policies"][0]
    policy["setting"] = "unknown"
    with pytest.raises(GuidanceError, match="not allowlisted"):
        adopt_guidance_baseline(
            tmp_path,
            candidate,
            approval=_approval(),
            adopted_at=LATER,
        )

    candidate = _candidate(work_order, refresh)
    candidate["principles"][0]["detector_policies"][0]["value"] = True
    with pytest.raises(GuidanceError, match="integer between"):
        adopt_guidance_baseline(
            tmp_path,
            candidate,
            approval=_approval(),
            adopted_at=LATER,
        )
