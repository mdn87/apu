"""Install-time check that a new installation continues the recorded chain.

A new operation records the target's hash as it stands (its original). When the
latest active installation that wrote the same target recorded a different
installed hash, something other than APU changed the file in between. That gap
is surfaced before anything is written, and installing past it needs an
operator-supplied reason, recorded on the new receipt.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from apu.models import PlanOperation
from apu.receipts import load_receipt
from apu.state import load_registry
from apu.validate import _registry_receipt_path, normalized_target


@dataclass(frozen=True)
class ChainGap:
    target: str
    prior_installation_id: str
    prior_installed_sha256: str
    observed_original_sha256: str | None

    def acknowledgment(self, reason: str) -> dict[str, str | None]:
        return {
            "target": self.target,
            "prior_installation_id": self.prior_installation_id,
            "prior_installed_sha256": self.prior_installed_sha256,
            "observed_original_sha256": self.observed_original_sha256,
            "reason": reason,
        }


def find_chain_gaps(
    state_home: Path,
    operations: Iterable[PlanOperation],
    *,
    installation_id: str,
) -> list[ChainGap]:
    """Report operations whose observed original breaks the recorded chain.

    Only active installations count, and an operation APU merely adopted
    unchanged (installed hash equal to original) is not a write APU owns, so it
    never forms a chain.
    """

    latest: dict[str, tuple[str, str, str]] = {}
    registry = load_registry(state_home)
    for other_id, entry in registry["installations"].items():
        if other_id == installation_id or entry.get("status") != "active":
            continue
        try:
            receipt = load_receipt(_registry_receipt_path(state_home, other_id, entry))
        except (OSError, TypeError, ValueError):
            continue
        created_at = str(receipt.get("created_at", ""))
        for recorded in receipt["operations"]:
            target = recorded.get("target")
            installed = recorded.get("installed_sha256")
            if (
                not isinstance(target, str)
                or installed is None
                or installed == recorded.get("original_sha256")
            ):
                continue
            key = normalized_target(target)
            if key not in latest or created_at > latest[key][0]:
                latest[key] = (created_at, other_id, installed)

    gaps: list[ChainGap] = []
    for operation in operations:
        prior = latest.get(normalized_target(operation.target))
        if prior is None:
            continue
        # The original apply will record: the live hash for an existing target
        # (apply verifies it equals the precondition), none for a new one.
        observed = operation.precondition_sha256
        if prior[2] != observed:
            gaps.append(
                ChainGap(
                    target=operation.target,
                    prior_installation_id=prior[1],
                    prior_installed_sha256=prior[2],
                    observed_original_sha256=observed,
                )
            )
    return gaps


def describe_gaps(gaps: Iterable[ChainGap]) -> str:
    lines = []
    for gap in gaps:
        observed = (
            "absent" if gap.observed_original_sha256 is None
            else gap.observed_original_sha256[:12]
        )
        lines.append(
            f"{gap.target}: installation {gap.prior_installation_id} recorded "
            f"{gap.prior_installed_sha256[:12]}, but the file is {observed} now"
        )
    return "; ".join(lines)
