from __future__ import annotations

import argparse
from pathlib import Path

from .claude_package_upgrade import ClaudePackageUpgradeExecutor, _json
from .models import canonical_json
from .package_coordinates import parse_package_coordinate, parse_profile_package
from .package_transactions import execute_package_upgrade, record_package_restoration
from .package_upgrade import (
    PackageStateIdentity,
    PackageUpgradeRequest,
    assess_claude_package_upgrade,
)
from .state import resolve_state_home


def add_package_parser(commands):
    packages = commands.add_parser(
        "packages", help="update Claude plugins with verified APU restore points"
    )
    subcommands = packages.add_subparsers(dest="package_command", required=True)
    upgrade = subcommands.add_parser(
        "upgrade", help="back up, update, and verify an exact expected target"
    )
    upgrade.add_argument("package", help="plugin@marketplace")
    upgrade.add_argument("--target-version", required=True)
    target = upgrade.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--target-sha256",
        help="expected installed-tree digest, not a candidate artifact digest",
    )
    target.add_argument(
        "--target-tree",
        type=Path,
        help="reviewed plugin tree to hash using the installed-tree algorithm",
    )
    upgrade.add_argument("--operation-id", required=True)
    upgrade.add_argument("--attempt", type=int, default=1)
    status = subcommands.add_parser(
        "status", help="inspect a Claude restore point and current state digest"
    )
    status.add_argument("transaction_id")
    rollback = subcommands.add_parser(
        "rollback", help="restore captured Claude files and installation records"
    )
    rollback.add_argument("transaction_id")
    rollback.add_argument(
        "--expected-state",
        help="explicitly reconcile interrupted or later-changed state using its reviewed digest",
    )
    for parser in (upgrade, status, rollback):
        parser.add_argument(
            "--claude-home",
            type=Path,
            help="home containing .claude (defaults to the operator home)",
        )


def run_packages(args: argparse.Namespace, *, home: Path) -> int:
    home = args.claude_home or home
    state = resolve_state_home()
    if args.package_command == "upgrade":
        from .package_adapters.base import ObservationLimits, hash_tree_bounded

        target_hash = args.target_sha256
        if args.target_tree is not None:
            target_hash = hash_tree_bounded(
                args.target_tree, limits=ObservationLimits()
            )
        coordinate = parse_profile_package(args.package)
        capability = assess_claude_package_upgrade(coordinate, home=home)
        if capability.status != "available":
            print(canonical_json(capability.to_dict()))
            return 1
        request = PackageUpgradeRequest(
            coordinate.package_id,
            args.operation_id,
            args.attempt,
            capability.pre_state,
            args.target_version,
            target_hash,
        )
        # Existing operations retain their original pre-state, even after a
        # successful update. The coordinator binds the entire request on retry.
        point_path = state / "claude-restore-points" / f"{request.transaction_id}.json"
        if point_path.exists():
            original = _json(point_path)["request"]
            request = PackageUpgradeRequest(
                coordinate.package_id,
                args.operation_id,
                args.attempt,
                PackageStateIdentity(**original["pre_state"]),
                args.target_version,
                target_hash,
            )
            from dataclasses import replace

            capability = replace(capability, pre_state=request.pre_state)
        executor = ClaudePackageUpgradeExecutor(state, home, request)
        receipt = execute_package_upgrade(
            state, coordinate, request, capability, executor=executor
        )
        print(
            canonical_json(
                {"transaction_id": request.transaction_id, "receipt": receipt}
            )
        )
        return 0 if receipt["status"] == "verified" else 1
    identifier = args.transaction_id
    if len(identifier) != 64 or any(c not in "0123456789abcdef" for c in identifier):
        raise ValueError("transaction id must be lowercase SHA-256")
    point = _json(state / "claude-restore-points" / f"{identifier}.json")
    raw = point["request"]
    coordinate = parse_package_coordinate(raw["package_id"])
    request = PackageUpgradeRequest(
        coordinate.package_id,
        raw["operation_id"],
        raw["attempt"],
        PackageStateIdentity(**raw["pre_state"]),
        raw["target"]["version"],
        raw["target"]["tree_sha256"],
    )
    if request.transaction_id != identifier:
        raise ValueError("restore point transaction identity mismatch")
    executor = ClaudePackageUpgradeExecutor(state, home, request)
    if args.package_command == "status":
        print(
            canonical_json(
                {
                    "transaction_id": identifier,
                    "package_id": coordinate.package_id,
                    "status": point["status"],
                    "snapshot_id": point["snapshot_id"],
                    "current_state_sha256": executor.current_state_digest(),
                }
            )
        )
        return 0
    result = executor.rollback_exact(
        coordinate, request.pre_state, expected_state=args.expected_state
    )
    receipt = record_package_restoration(state, coordinate, request, executor=executor)
    print(
        canonical_json(
            {"transaction_id": identifier, "restoration": result, "receipt": receipt}
        )
    )
    return 0
