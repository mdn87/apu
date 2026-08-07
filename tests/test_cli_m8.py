from __future__ import annotations

import json
from pathlib import Path

from apu.cli import main


def test_research_packages_routes_tracked_coordinate_and_emits_summary(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    home = tmp_path / "home"
    state = tmp_path / "state"
    profile = tmp_path / "profile.toml"
    profile.write_text(
        f"""
schema_version = 1
roots = ["{tmp_path.as_posix()}"]
global_surfaces = []
packages = ["superpowers@claude-plugins-official"]
""",
        encoding="utf-8",
    )
    report_path = state / "packages" / "reports" / "report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text("{}", encoding="utf-8")
    seen = {}

    def fake_research(coordinate, **kwargs):
        seen["coordinate"] = coordinate.package_id
        seen["home"] = kwargs["home"]
        return (
            {
                "research_id": "a" * 64,
                "recommendation": {
                    "decision": "hold",
                    "reason_codes": ["no-static-improvement"],
                    "eligible_for_pin_plan": False,
                    "mutation_status": "unavailable-provider-pin-unsupported",
                },
                "classifier_comparison": {
                    "delta": {
                        "finding_counts": {},
                        "resolved": [],
                        "introduced": [],
                        "severity_changed": [],
                        "verdict": "unchanged",
                    }
                },
            },
            report_path,
        )

    monkeypatch.setenv("APU_HOME", str(state))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "apu.package_research.research_package",
        fake_research,
    )

    assert (
        main(
            [
                "research",
                "packages",
                "superpowers",
                "--profile",
                str(profile),
            ]
        )
        == 0
    )

    emitted = json.loads(capsys.readouterr().out)
    assert seen == {
        "coordinate": "claude:superpowers@claude-plugins-official",
        "home": home.resolve(),
    }
    assert emitted["reports"][0]["report_path"] == str(report_path)
    assert emitted["reports"][0]["recommendation"]["decision"] == "hold"


def test_research_packages_requires_a_tracked_unambiguous_package(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    profile = tmp_path / "profile.toml"
    profile.write_text(
        f"""
schema_version = 1
roots = ["{tmp_path.as_posix()}"]
global_surfaces = []
packages = []
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("APU_HOME", str(tmp_path / "state"))

    assert (
        main(["research", "packages", "--profile", str(profile)])
        == 1
    )
    assert "does not track any packages" in capsys.readouterr().err
    assert not (tmp_path / "state").exists()
