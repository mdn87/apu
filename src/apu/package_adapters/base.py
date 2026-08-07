from __future__ import annotations

import os
import stat
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from apu.package_coordinates import parse_profile_package

OBSERVATION_SCHEMA_VERSION = 1
OBSERVATION_STATUSES = frozenset(
    {"verified", "fallback", "absent", "ambiguous", "invalid"}
)


class PackageObservationError(ValueError):
    """Raised when package evidence cannot be read safely."""


@dataclass(frozen=True)
class ObservationLimits:
    max_metadata_bytes: int = 2 * 1024 * 1024
    max_tree_bytes: int = 128 * 1024 * 1024
    max_tree_entries: int = 20_000
    max_tree_depth: int = 64

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class EvidenceReference:
    kind: str
    path: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("evidence kind is required")
        if not Path(self.path).is_absolute():
            raise ValueError("evidence path must be absolute")
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("evidence sha256 must be lowercase hexadecimal")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PackageObservation:
    provider: str
    package_id: str
    package_name: str
    marketplace: str
    status: str
    confidence: str
    version: str | None
    scope: str | None
    install_path: str | None
    tree_sha256: str | None
    provenance: tuple[EvidenceReference, ...] = ()
    issues: tuple[str, ...] = ()
    schema_version: int = OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OBSERVATION_SCHEMA_VERSION:
            raise ValueError("unsupported package observation schema_version")
        if self.status not in OBSERVATION_STATUSES:
            raise ValueError(f"unsupported package observation status: {self.status}")
        if self.confidence not in {"authoritative", "cache-only", "none"}:
            raise ValueError(f"unsupported package confidence: {self.confidence}")
        coordinate = parse_profile_package(
            self.package_id,
            provider=self.provider,
        )
        if (
            coordinate.package != self.package_name
            or coordinate.source != self.marketplace
        ):
            raise ValueError("package observation identifier is inconsistent")
        for value, field in (
            (self.provider, "provider"),
            (self.marketplace, "marketplace"),
        ):
            safe_component(value, field)
        selected = self.status in {"verified", "fallback"}
        selected_values = (
            self.version,
            self.install_path,
            self.tree_sha256,
        )
        if selected and not all(value is not None for value in selected_values):
            raise ValueError(
                "selected package observations require version, path, and tree hash"
            )
        if not selected and any(value is not None for value in selected_values):
            raise ValueError(
                "unselected package observations cannot select package evidence"
            )
        if self.status == "verified" and self.scope is None:
            raise ValueError("verified observations require installation scope")
        if self.status != "verified" and self.scope is not None:
            raise ValueError("only verified observations can select a scope")
        if selected and not Path(self.install_path or "").is_absolute():
            raise ValueError("selected package install_path must be absolute")
        if self.tree_sha256 is not None and (
            len(self.tree_sha256) != 64
            or any(
                character not in "0123456789abcdef" for character in self.tree_sha256
            )
        ):
            raise ValueError("package tree_sha256 must be lowercase hexadecimal")
        if self.status == "verified" and self.confidence != "authoritative":
            raise ValueError("verified observations require authoritative confidence")
        if self.status == "fallback" and self.confidence != "cache-only":
            raise ValueError("fallback observations require cache-only confidence")
        if not selected and self.confidence != "none":
            raise ValueError("unselected observations require no confidence")
        if tuple(sorted(self.provenance, key=lambda item: (item.kind, item.path))) != (
            self.provenance
        ):
            raise ValueError("package observation provenance must be sorted")
        if tuple(sorted(set(self.issues))) != self.issues:
            raise ValueError("package observation issues must be sorted and unique")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["provenance"] = [item.to_dict() for item in self.provenance]
        value["issues"] = list(self.issues)
        return value


class PackageAdapter(Protocol):
    provider: str

    def observe(self, package_id: str) -> PackageObservation:
        """Observe one installed package without network or mutation."""


def split_package_id(package_id: str) -> tuple[str, str]:
    coordinate = parse_profile_package(package_id, provider="claude")
    return coordinate.package, coordinate.source


def safe_component(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"{field} must be one safe path component")
    return value


def absolute_logical_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.normpath(os.fspath(left))) == os.path.normcase(
        os.path.normpath(os.fspath(right))
    )


def is_within(path: Path, root: Path) -> bool:
    candidate = absolute_logical_path(path)
    boundary = absolute_logical_path(root)
    try:
        return os.path.commonpath((candidate, boundary)) == os.path.normpath(
            os.fspath(boundary)
        )
    except ValueError:
        return False


def file_evidence(
    path: Path,
    *,
    kind: str,
    limits: ObservationLimits,
) -> EvidenceReference:
    logical = absolute_logical_path(path)
    content = read_stable_file(logical, max_bytes=limits.max_metadata_bytes)
    return EvidenceReference(
        kind=kind,
        path=str(logical),
        sha256=sha256(content).hexdigest(),
    )


def read_stable_file(path: Path, *, max_bytes: int) -> bytes:
    logical = absolute_logical_path(path)
    try:
        before = logical.lstat()
    except (OSError, RuntimeError) as error:
        raise PackageObservationError(
            f"evidence file is unavailable: {logical}"
        ) from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise PackageObservationError(f"evidence path is not a regular file: {logical}")
    if before.st_size > max_bytes:
        raise PackageObservationError(f"evidence file exceeds size limit: {logical}")

    digest_content = bytearray()
    try:
        with logical.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _same_file_state(before, opened):
                raise PackageObservationError(
                    f"evidence file changed while opening: {logical}"
                )
            while chunk := stream.read(min(1024 * 1024, max_bytes + 1)):
                digest_content.extend(chunk)
                if len(digest_content) > max_bytes:
                    raise PackageObservationError(
                        f"evidence file exceeds size limit: {logical}"
                    )
        after = logical.lstat()
    except OSError as error:
        raise PackageObservationError(
            f"evidence file could not be read: {logical}"
        ) from error
    if not _same_file_state(before, after):
        raise PackageObservationError(f"evidence file changed while reading: {logical}")
    return bytes(digest_content)


def hash_tree_bounded(
    root: Path,
    *,
    limits: ObservationLimits,
) -> str:
    """Hash a stable tree without following links or escaping its root."""

    logical_root = absolute_logical_path(root)
    try:
        root_metadata = logical_root.lstat()
    except OSError as error:
        raise PackageObservationError(
            f"package tree is unavailable: {logical_root}"
        ) from error
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise PackageObservationError(
            f"package tree root must be a regular directory: {logical_root}"
        )
    try:
        resolved_root = logical_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise PackageObservationError(
            f"package tree root cannot be resolved: {logical_root}"
        ) from error

    digest = sha256()
    entry_count = 0
    total_bytes = 0
    visited: set[tuple[int, int]] = set()

    def visit(directory: Path, relative: Path, depth: int) -> None:
        nonlocal entry_count, total_bytes
        if depth > limits.max_tree_depth:
            raise PackageObservationError("package tree exceeds depth limit")
        metadata = directory.lstat()
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in visited:
            raise PackageObservationError("package tree contains a directory cycle")
        visited.add(identity)
        try:
            if not directory.resolve(strict=True).is_relative_to(resolved_root):
                raise PackageObservationError("package tree directory escapes root")
            children: list[Path] = []
            for child in directory.iterdir():
                if entry_count + len(children) >= limits.max_tree_entries:
                    raise PackageObservationError("package tree exceeds entry limit")
                children.append(child)
            children.sort(key=lambda item: item.name)
        except (OSError, RuntimeError) as error:
            raise PackageObservationError(
                f"package tree directory could not be read: {directory}"
            ) from error

        for child in children:
            child_relative = relative / child.name
            relative_bytes = child_relative.as_posix().encode("utf-8")
            entry_count += 1
            if entry_count > limits.max_tree_entries:
                raise PackageObservationError("package tree exceeds entry limit")
            try:
                child_metadata = child.lstat()
            except OSError as error:
                raise PackageObservationError(
                    f"package tree entry could not be read: {child}"
                ) from error

            if stat.S_ISLNK(child_metadata.st_mode):
                target = os.readlink(child)
                try:
                    resolved = (
                        Path(target)
                        if Path(target).is_absolute()
                        else child.parent / target
                    ).resolve(strict=False)
                except RuntimeError as error:
                    raise PackageObservationError(
                        "package tree link cannot be resolved"
                    ) from error
                if not resolved.is_relative_to(resolved_root):
                    raise PackageObservationError("package tree link escapes root")
                digest.update(b"L\0" + relative_bytes + b"\0")
                digest.update(os.fsencode(target))
            elif stat.S_ISDIR(child_metadata.st_mode):
                digest.update(b"D\0" + relative_bytes + b"\0")
                visit(child, child_relative, depth + 1)
            elif stat.S_ISREG(child_metadata.st_mode):
                content = read_stable_file(
                    child,
                    max_bytes=limits.max_tree_bytes - total_bytes,
                )
                total_bytes += len(content)
                if total_bytes > limits.max_tree_bytes:
                    raise PackageObservationError("package tree exceeds byte limit")
                digest.update(b"F\0" + relative_bytes + b"\0")
                digest.update(sha256(content).digest())
            else:
                raise PackageObservationError(
                    f"package tree contains unsupported object: {child}"
                )
            digest.update(b"\0")
        if not _same_file_state(metadata, directory.lstat()):
            raise PackageObservationError(
                f"package tree directory changed while hashing: {directory}"
            )

    visit(logical_root, Path(), 0)
    after_root = logical_root.lstat()
    if not _same_file_state(root_metadata, after_root):
        raise PackageObservationError("package tree root changed while hashing")
    return digest.hexdigest()


def sorted_provenance(
    evidence: list[EvidenceReference],
) -> tuple[EvidenceReference, ...]:
    return tuple(sorted(evidence, key=lambda item: (item.kind, item.path)))


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
        right.st_size,
        right.st_mtime_ns,
    )
