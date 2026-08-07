from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Sequence

from apu import __version__
from apu.audit import build_inventory
from apu.models import Inventory, Plan, canonical_json
from apu.outcomes import append_outcome, read_outcomes, summarize_outcomes
from apu.planning import (
    approve_all_recommended,
    build_skill_install_operations,
    propose_inventory,
    update_plan_status,
)
from apu.resources import behavioral_fixtures_path, optimizer_skill_path
from apu.state import (
    ensure_state_home,
    load_registry,
    resolve_state_home,
    write_json_atomic,
)
from apu.wizard import ReviewDecision, review_plan


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _home() -> Path:
    value = os.environ.get("HOME") or os.environ.get("USERPROFILE")
    return Path(value).expanduser() if value else Path.home()


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"artifact must be a JSON object: {path}")
    return value


def _write_artifact(path: Path, value: Any) -> Path:
    destination = path.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(canonical_json(value))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _supports_directory_symlink(parent: Path) -> bool:
    """Probe directory symlink capability in private disposable state."""

    try:
        with tempfile.TemporaryDirectory(prefix="capability.", dir=parent) as name:
            root = Path(name)
            source = root / "source"
            source.mkdir()
            link = root / "link"
            os.symlink(source, link, target_is_directory=True)
            return link.is_symlink() and link.resolve() == source.resolve()
    except OSError:
        return False


def _load_plan(path: Path) -> Plan:
    plan = Plan.from_dict(_read_object(path))
    plan.validate()
    return plan


def _emit(value: Any) -> None:
    print(canonical_json(value))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apu")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    audit = commands.add_parser("audit", help="inventory instruction surfaces")
    audit.add_argument("paths", nargs="*", type=Path)
    audit.add_argument("--cwd", action="append", type=Path, default=[])
    audit.add_argument("--sessions", nargs="+", type=Path)
    audit.add_argument("--git-repo", type=Path)
    audit.add_argument("--root-session-id")
    audit.add_argument("--json", dest="output", type=Path)

    propose = commands.add_parser("propose", help="create a deterministic plan")
    propose.add_argument("--inventory", required=True, type=Path)
    propose.add_argument("--output", type=Path)

    review = commands.add_parser("review", help="record plan decisions")
    review.add_argument("plan", type=Path)
    review.add_argument("--approve-all-recommended", action="store_true")
    review.add_argument("--output", type=Path)

    apply = commands.add_parser("apply", help="apply an approved plan")
    apply.add_argument("plan", type=Path)
    apply.add_argument("--yes", action="store_true")
    apply.add_argument("--installation-id")

    validate = commands.add_parser("validate", help="run structural validation")
    selectors = validate.add_mutually_exclusive_group()
    selectors.add_argument("--plan", type=Path)
    selectors.add_argument("--receipt", type=Path)
    selectors.add_argument("--fixture", type=Path)
    validate.add_argument("--runner", choices=("codex", "claude"))
    validate.add_argument("--enable-runtime", action="store_true")

    rollback = commands.add_parser("rollback", help="roll back an installation")
    rollback.add_argument("--receipt", required=True, type=Path)

    commands.add_parser("status", help="show installations and drift")

    outcome = commands.add_parser("outcome", help="record or summarize outcomes")
    outcome_commands = outcome.add_subparsers(dest="outcome_command", required=True)
    record = outcome_commands.add_parser("record")
    record.add_argument("--receipt", required=True, type=Path)
    record.add_argument("--task-id")
    record.add_argument("--non-material", action="store_true")
    record.add_argument("--source", choices=("user", "trace", "imported"), default="user")
    record.add_argument("--elapsed-seconds", type=float)
    record.add_argument("--agent-count", type=int)
    record.add_argument("--review-count", type=int)
    record.add_argument("--remediation-count", type=int)
    record.add_argument(
        "--validation",
        choices=("passed", "failed", "partial", "unknown"),
        default="unknown",
    )
    record.add_argument("--rework", action="store_true")
    record.add_argument("--escaped-defect", action="store_true")
    record.add_argument(
        "--defect-severity",
        choices=("none", "ordinary", "serious"),
        default="none",
    )
    record.add_argument("--defect-category")
    record.add_argument("--notes")
    listing = outcome_commands.add_parser("list")
    listing.add_argument("--receipt", type=Path)

    init = commands.add_parser("init", help="run the guided first-use flow")
    init.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    init.add_argument("--apply", action="store_true")
    init.add_argument("--yes", action="store_true")
    init.add_argument("--behavioral", choices=("codex", "claude"))

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (
        args.command == "audit"
        and args.root_session_id is not None
        and not args.sessions
    ):
        parser.error("--root-session-id requires --sessions")
    try:
        return _dispatch(args)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"apu: {error}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "audit":
        return _audit(args)
    if args.command == "propose":
        return _propose(args)
    if args.command == "review":
        return _review(args)
    if args.command == "apply":
        return _apply(args)
    if args.command == "validate":
        return _validate(args)
    if args.command == "rollback":
        return _rollback(args)
    if args.command == "status":
        return _status(args)
    if args.command == "outcome":
        return _outcome(args)
    if args.command == "init":
        return _init(args)
    raise ValueError(f"unsupported command: {args.command}")


def _audit(args: argparse.Namespace) -> int:
    roots = tuple(args.paths or (Path.cwd(),))
    inventory = build_inventory(
        roots,
        home=_home(),
        working_directories=args.cwd,
        session_paths=args.sessions or (),
        root_session_id=args.root_session_id,
        git_repository=args.git_repo,
    )
    value = inventory.to_dict()
    if args.output:
        _write_artifact(args.output, value)
        print(args.output)
    else:
        _emit(value)
    return 0


def _propose(args: argparse.Namespace) -> int:
    inventory = Inventory.from_dict(_read_object(args.inventory))
    candidate_dir = (
        args.output.parent / f"{args.output.stem}.candidates"
        if args.output is not None
        else None
    )
    plan = propose_inventory(
        inventory,
        created_at=_timestamp(),
        candidate_dir=candidate_dir,
    )
    value = plan.to_dict()
    if args.output:
        _write_artifact(args.output, value)
        print(args.output)
    else:
        _emit(value)
    return 0


def _review(args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    if args.approve_all_recommended:
        reviewed = replace(
            plan,
            operations=approve_all_recommended(
                plan.operations, recorded_at=_timestamp()
            ),
        )
        reviewed = update_plan_status(reviewed)
    else:
        decisions: dict[str, str] = {}

        def decide(operation):
            key = operation.atomic_group_id or operation.id
            if key not in decisions:
                answer = input(
                    f"{operation.id} {operation.action} {operation.target} "
                    "[a]pprove/[r]eject/[d]efer/[e]dit/[m]ove: "
                ).strip().lower()
                if answer == "e":
                    source = Path(
                        input("Replacement candidate path: ").strip()
                    )
                    return ReviewDecision(
                        "approved", replacement_source=source
                    )
                if answer == "m":
                    destination = Path(
                        input("Relocation destination path: ").strip()
                    )
                    return ReviewDecision(
                        "approved", relocate_target=destination
                    )
                decisions[key] = {
                    "a": "approved",
                    "r": "rejected",
                    "d": "deferred",
                }.get(answer, "deferred")
            return decisions[key]

        reviewed = review_plan(plan, decide=decide, recorded_at=_timestamp())
    destination = args.output or args.plan
    _write_artifact(destination, reviewed.to_dict())
    print(destination)
    return 0 if reviewed.status == "approved" else 1


def _apply(args: argparse.Namespace) -> int:
    from apu.apply import apply_plan

    plan = _load_plan(args.plan)
    if not args.yes:
        answer = input("Apply approved plan? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            return 1
    installation_id = args.installation_id or (
        "install-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    receipt = apply_plan(
        plan,
        state_home=resolve_state_home(),
        installation_id=installation_id,
        confirmed=True,
    )
    print(receipt)
    return 0


def _validate(args: argparse.Namespace) -> int:
    from apu.validate import run_behavioral_fixture, validate

    if args.fixture is not None:
        if args.runner is None:
            raise ValueError("--fixture requires --runner")
        result = run_behavioral_fixture(
            args.fixture,
            args.runner,
            runtime_enabled=args.enable_runtime,
        )
        _emit(result.to_dict())
        return 0 if result.status in {"passed", "skipped", "unavailable"} else 1

    result = validate(
        plan_path=args.plan,
        receipt_path=args.receipt,
        state_home=resolve_state_home(),
    )
    _emit(result.to_dict())
    return 0 if result.status in {"passed", "skipped", "unavailable"} else 1


def _rollback(args: argparse.Namespace) -> int:
    from apu.rollback import rollback_receipt

    result = rollback_receipt(args.receipt)
    _emit(result)
    return 0 if result["status"] == "rolled_back" else 1


def _status(_args: argparse.Namespace) -> int:
    from apu.validate import validate_registered_installations

    state_home = resolve_state_home()
    registry = load_registry(state_home)
    installations: dict[str, Any] = {}
    for installation_id, stored in registry["installations"].items():
        entry = dict(stored)
        entry["monitoring"] = summarize_outcomes(
            read_outcomes(state_home, installation_id),
            monitoring_started_at=entry.get(
                "monitoring_started_at", entry.get("applied_at")
            ),
        )
        installations[installation_id] = entry
    validation = validate_registered_installations(state_home)
    _emit(
        {
            "apu_version": __version__,
            "state_home": str(state_home),
            "installations": installations,
            "validation": validation.to_dict(),
        }
    )
    return 0 if validation.status == "passed" else 1


def _outcome(args: argparse.Namespace) -> int:
    from apu.receipts import load_receipt

    state_home = resolve_state_home()
    if args.outcome_command == "record":
        receipt = load_receipt(args.receipt)
        record = {
            "schema_version": 1,
            "installation_id": receipt["installation_id"],
            "recorded_at": _timestamp(),
            "task_id": args.task_id or "task-" + datetime.now(timezone.utc).strftime(
                "%Y%m%dT%H%M%S%fZ"
            ),
            "material": not args.non_material,
            "source": args.source,
            "elapsed_seconds": args.elapsed_seconds,
            "agent_count": args.agent_count,
            "review_count": args.review_count,
            "remediation_count": args.remediation_count,
            "validation": args.validation,
            "rework": args.rework,
            "escaped_defect": {
                "present": args.escaped_defect,
                "severity": args.defect_severity,
                "category": args.defect_category,
            },
            "notes": args.notes,
        }
        path = append_outcome(state_home, record)
        print(path)
        return 0

    installation_ids: list[str]
    if args.receipt:
        installation_ids = [load_receipt(args.receipt)["installation_id"]]
    else:
        installation_ids = sorted(load_registry(state_home)["installations"])
    summaries = {}
    for installation_id in installation_ids:
        records = read_outcomes(state_home, installation_id)
        summaries[installation_id] = {
            "records": records,
            "summary": summarize_outcomes(records),
        }
    _emit(summaries)
    return 0


def _init(args: argparse.Namespace) -> int:
    state_home = ensure_state_home(resolve_state_home())
    plan_path = state_home / "plans" / (
        "init-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
    )
    inventory = build_inventory(
        [args.path],
        home=_home(),
        working_directories=[args.path],
    )
    plan = propose_inventory(
        inventory,
        created_at=_timestamp(),
        candidate_dir=plan_path.parent / f"{plan_path.stem}.candidates",
    )
    skill_operations = build_skill_install_operations(
        package_skill=optimizer_skill_path(),
        home=_home(),
        include_claude=True,
        symlink_supported=_supports_directory_symlink(state_home),
    )
    plan = replace(plan, operations=(*plan.operations, *skill_operations))
    plan = update_plan_status(plan)
    write_json_atomic(plan_path, plan.to_dict())
    print(f"Draft plan: {plan_path}")
    print(
        f"Surfaces: {len(inventory.surfaces)}; findings: {len(inventory.findings)}; "
        f"operations: {len(plan.operations)}"
    )
    if not args.apply:
        return 0
    if not sys.stdin.isatty():
        raise ValueError("apu init --apply requires an interactive review")

    reviewed = review_plan(
        plan,
        decide=lambda operation: {
            "a": "approved",
            "r": "rejected",
            "d": "deferred",
        }.get(
            input(
                f"{operation.id} {operation.action} {operation.target} "
                "[a]pprove/[r]eject/[d]efer: "
            )
            .strip()
            .lower(),
            "deferred",
        ),
        recorded_at=_timestamp(),
    )
    write_json_atomic(plan_path, reviewed.to_dict())
    if reviewed.status != "approved":
        print("Plan remains draft; no files changed.")
        return 1
    if not args.yes:
        answer = input("Apply approved plan? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            return 1

    from apu.apply import apply_plan
    from apu.validate import validate_receipt_path

    installation_id = (
        "install-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    receipt = apply_plan(
        reviewed,
        state_home=state_home,
        installation_id=installation_id,
        confirmed=True,
    )
    print(receipt)
    structural = validate_receipt_path(receipt)
    _emit({"structural_validation": structural.to_dict()})
    if structural.status != "passed":
        return 1

    postflight = build_inventory(
        [args.path],
        home=_home(),
        working_directories=[args.path],
    )
    _emit(
        {
            "provider_postflight": {
                "providers": sorted(
                    {surface.provider for surface in postflight.surfaces}
                ),
                "surface_count": len(postflight.surfaces),
                "relationship_count": len(postflight.relationships),
            }
        }
    )
    if args.behavioral:
        from apu.validate import run_behavioral_fixture

        failures = 0
        for fixture in sorted(
            path for path in behavioral_fixtures_path().iterdir() if path.is_dir()
        ):
            behavioral = run_behavioral_fixture(
                fixture,
                args.behavioral,
                runtime_enabled=True,
            )
            _emit({"fixture": fixture.name, **behavioral.to_dict()})
            failures += int(behavioral.status == "failed")
        if failures:
            return 1
    return 0
