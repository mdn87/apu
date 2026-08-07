from __future__ import annotations

import io
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from apu.models import sha256_bytes, sha256_json
from apu.package_coordinates import (
    SemanticVersionError,
    parse_semantic_version,
)
from apu.package_state import (
    PackageStateError,
    store_candidate_tree,
    write_package_leaf,
)
from apu.state import ensure_private_directory, ensure_state_home

GitRunner = Callable[[Sequence[str], Path | None], str]
ArchiveFetcher = Callable[[str, int], bytes]
_ALLOWED_GIT_HOSTS = frozenset({"github.com"})
_MAX_TAG_LIST_BYTES = 4 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 20_000
_MAX_EXPANDED_BYTES = 256 * 1024 * 1024
_MAX_ARCHIVE_DEPTH = 48
_MAX_LINK_TARGET_BYTES = 4 * 1024
_NORMALIZATION_POLICY = "virtual-internal-file-links-v1"


class PackageFetchError(RuntimeError):
    """Raised when an upstream package cannot be resolved immutably."""


def fetch_git_candidate(
    *,
    package_id: str,
    source_url: str,
    state_home: Path,
    requested_version: str | None = None,
    retrieved_at: str | None = None,
    git_runner: GitRunner | None = None,
    archive_fetcher: ArchiveFetcher | None = None,
) -> tuple[dict[str, Any], Path, Path]:
    """Resolve one stable git tag and store its inert tree privately."""

    sanitized_url = _validated_https_url(source_url)
    runner = git_runner or _run_git
    fetch_archive = archive_fetcher or _fetch_archive
    selected = _select_tag(
        _list_tags(sanitized_url, runner),
        requested_version=requested_version,
    )
    root = ensure_state_home(Path(state_home).expanduser().resolve())
    download_root = ensure_private_directory(root / "packages" / "downloads")

    temporary = Path(
        tempfile.mkdtemp(
            prefix=".fetch-",
            dir=download_root,
        )
    )
    export = temporary / "export"
    try:
        archive_url = github_archive_url(
            sanitized_url,
            selected["commit_oid"],
        )
        archive = fetch_archive(archive_url, _MAX_ARCHIVE_BYTES)
        links = _extract_archive(archive, export)
        content_tree_sha256, tree_path = store_candidate_tree(root, export)
        normalization = {
            "policy": _NORMALIZATION_POLICY,
            "links": list(links),
        }
        tree_sha256 = sha256_json(
            {
                "content_tree_sha256": content_tree_sha256,
                "normalization": normalization,
            }
        )
        changelog = _changelog_identity(tree_path)
        artifact = {
            "schema_version": 1,
            "artifact_type": "package-candidate",
            "package_id": package_id,
            "status": "available",
            "version": selected["version"],
            "immutable_ref": {
                "tag": selected["tag"],
                "commit_oid": selected["commit_oid"],
                "archive_sha256": sha256_bytes(archive),
                "content_tree_sha256": content_tree_sha256,
                "tree_sha256": tree_sha256,
            },
            "retrieval": {
                "retrieved_at": retrieved_at or _timestamp(),
                "source_kind": "github-commit-archive",
                "source_url": sanitized_url,
                "archive_url": archive_url,
            },
            "normalization": normalization,
            "changelog": changelog,
        }
        _, artifact_path = write_package_leaf(
            root,
            kind="candidates",
            package_id=package_id,
            value=artifact,
        )
        return artifact, artifact_path, tree_path
    except PackageFetchError:
        raise
    except (
        OSError,
        PackageStateError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ) as error:
        raise PackageFetchError(
            f"candidate fetch failed: {type(error).__name__}"
        ) from error
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _list_tags(source_url: str, runner: GitRunner) -> tuple[dict[str, str], ...]:
    output = runner(("ls-remote", "--tags", source_url), None)
    raw: dict[str, dict[str, str]] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        revision, reference = fields
        prefix = "refs/tags/"
        if not reference.startswith(prefix):
            continue
        tag = reference[len(prefix) :]
        peeled = tag.endswith("^{}")
        if peeled:
            tag = tag[:-3]
        if not tag or not _is_git_oid(revision):
            continue
        entry = raw.setdefault(tag, {})
        entry["peeled" if peeled else "direct"] = revision.lower()

    tags: list[dict[str, str]] = []
    for tag, revisions in raw.items():
        raw_version = tag.removeprefix("v")
        try:
            version = parse_semantic_version(raw_version)
        except SemanticVersionError:
            continue
        if not version.is_stable:
            continue
        commit = revisions.get("peeled", revisions.get("direct"))
        if commit is None:
            continue
        tags.append(
            {
                "tag": tag,
                "version": str(version),
                "commit_oid": commit,
            }
        )
    return tuple(
        sorted(
            tags,
            key=lambda item: (
                parse_semantic_version(item["version"]),
                item["tag"],
            ),
        )
    )


def _select_tag(
    tags: Sequence[dict[str, str]],
    *,
    requested_version: str | None,
) -> dict[str, str]:
    if requested_version is None:
        if not tags:
            raise PackageFetchError("upstream has no stable semantic-version tags")
        version = max(
            parse_semantic_version(item["version"]) for item in tags
        )
    else:
        try:
            version = parse_semantic_version(requested_version)
        except SemanticVersionError as error:
            raise PackageFetchError(
                "requested package version must be exact SemVer"
            ) from error
        if not version.is_stable:
            raise PackageFetchError(
                "requested package version must be a stable release"
            )
    matches = [
        item
        for item in tags
        if parse_semantic_version(item["version"]) == version
    ]
    if not matches:
        raise PackageFetchError(
            f"upstream does not expose package version {version}"
        )
    commits = {item["commit_oid"] for item in matches}
    if len(matches) != 1 or len(commits) != 1:
        raise PackageFetchError(
            f"upstream version {version} resolves ambiguously"
        )
    return dict(matches[0])


def _validated_https_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise PackageFetchError("package source URL is required")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname.casefold() not in _ALLOWED_GIT_HOSTS
    ):
        raise PackageFetchError(
            "package source must be a credential-free HTTPS URL on an "
            "adapter-authorized host"
        )
    return value


def _run_git(arguments: Sequence[str], cwd: Path | None) -> str:
    command = [
        "git",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "http.followRedirects=false",
        "-c",
        "credential.helper=",
        *arguments,
    ]
    with tempfile.TemporaryDirectory(prefix="apu-git-config-") as config_home:
        environment = _isolated_git_environment(Path(config_home))
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert process.stdout is not None
            output = process.stdout.read(_MAX_TAG_LIST_BYTES + 1)
            if len(output) > _MAX_TAG_LIST_BYTES:
                process.kill()
                process.wait(timeout=10)
                raise PackageFetchError("git tag listing exceeds the byte limit")
            return_code = process.wait(timeout=120)
            if return_code != 0:
                raise PackageFetchError("git candidate request failed")
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait(timeout=10)
            raise PackageFetchError("git candidate request timed out") from error
        except OSError as error:
            raise PackageFetchError(
                f"git candidate request failed: {type(error).__name__}"
            ) from error
    try:
        return output.decode("utf-8")
    except UnicodeError as error:
        raise PackageFetchError("git tag listing is not UTF-8") from error


def _isolated_git_environment(config_home: Path) -> dict[str, str]:
    allowed = (
        "COMSPEC",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    )
    environment = {
        key: os.environ[key]
        for key in allowed
        if key in os.environ
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(config_home),
        }
    )
    return environment


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


def _fetch_archive(url: str, max_bytes: int) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/zip",
            "User-Agent": "apu-package-research/1",
        },
        method="GET",
    )
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    try:
        with opener.open(request, timeout=60) as response:
            if response.status != 200:
                raise PackageFetchError(
                    f"candidate archive returned HTTP {response.status}"
                )
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    content_length = int(raw_length)
                except ValueError as error:
                    raise PackageFetchError(
                        "candidate archive Content-Length is invalid"
                    ) from error
                if content_length < 0 or content_length > max_bytes:
                    raise PackageFetchError(
                        "candidate archive exceeds the transfer limit"
                    )
            content = response.read(max_bytes + 1)
    except PackageFetchError:
        raise
    except OSError as error:
        raise PackageFetchError(
            f"candidate archive request failed: {type(error).__name__}"
        ) from error
    if len(content) > max_bytes:
        raise PackageFetchError("candidate archive exceeds the transfer limit")
    return content


def github_archive_url(source_url: str, commit_oid: str) -> str:
    parsed = urlsplit(source_url)
    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) != 2
        or "%" in parsed.path
        or not _is_git_oid(commit_oid)
    ):
        raise PackageFetchError("GitHub package source path is unsupported")
    owner, repository = parts
    repository = repository.removesuffix(".git")
    if not owner or not repository:
        raise PackageFetchError("GitHub package source path is unsupported")
    return (
        "https://codeload.github.com/"
        f"{quote(owner, safe='')}/{quote(repository, safe='')}/zip/{commit_oid}"
    )


def _extract_archive(
    content: bytes,
    destination: Path,
) -> tuple[dict[str, str], ...]:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        if not entries or len(entries) > _MAX_ARCHIVE_ENTRIES:
            raise PackageFetchError(
                "candidate archive entry count is invalid or exceeds the limit"
            )
        prepared: list[tuple[zipfile.ZipInfo, Path, str]] = []
        members: dict[PurePosixPath, tuple[zipfile.ZipInfo, str]] = {}
        normalized_paths: set[str] = set()
        root_name: str | None = None
        expanded_bytes = 0
        for entry in entries:
            filename = entry.filename
            raw_parts = filename.split("/")
            path_parts = raw_parts[:-1] if raw_parts[-1:] == [""] else raw_parts
            raw = PurePosixPath(entry.filename)
            if (
                "\\" in filename
                or "\0" in filename
                or not path_parts
                or any(part in {"", ".", ".."} for part in path_parts)
                or raw.is_absolute()
                or ".." in raw.parts
                or not raw.parts
                or entry.flag_bits & 0x1
            ):
                raise PackageFetchError("candidate archive contains an unsafe path")
            if root_name is None:
                root_name = raw.parts[0]
            if raw.parts[0] != root_name:
                raise PackageFetchError(
                    "candidate archive has multiple top-level roots"
                )
            relative = Path(*raw.parts[1:])
            if not relative.parts:
                continue
            if len(relative.parts) > _MAX_ARCHIVE_DEPTH:
                raise PackageFetchError(
                    "candidate archive exceeds the depth limit"
                )
            entry_type = _validate_archive_entry(entry, relative)
            normalized = "/".join(part.casefold() for part in relative.parts)
            if normalized in normalized_paths:
                raise PackageFetchError(
                    "candidate archive contains duplicate or case-colliding paths"
                )
            normalized_paths.add(normalized)
            relative_posix = PurePosixPath(*relative.parts)
            members[relative_posix] = (entry, entry_type)
            if entry_type == "file":
                expanded_bytes += entry.file_size
                if expanded_bytes > _MAX_EXPANDED_BYTES:
                    raise PackageFetchError(
                        "candidate archive exceeds the expanded byte limit"
                    )
            prepared.append((entry, relative, entry_type))

        links = _validate_archive_links(archive, members)
        expanded_bytes += sum(
            members[PurePosixPath(link["resolved_target"])][0].file_size
            for link in links
        )
        if expanded_bytes > _MAX_EXPANDED_BYTES:
            raise PackageFetchError(
                "candidate archive exceeds the logical expanded byte limit"
            )

        destination.mkdir(parents=True)
        written_bytes = 0
        for entry, relative, entry_type in prepared:
            target = destination / relative
            if not target.is_relative_to(destination):
                raise PackageFetchError(
                    "candidate archive path escapes the staging directory"
                )
            if entry_type == "directory":
                target.mkdir(parents=True, exist_ok=True)
                continue
            if entry_type == "symlink":
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry, "r") as source, target.open("xb") as output:
                while True:
                    chunk = source.read(min(1024 * 1024, entry.file_size + 1))
                    if not chunk:
                        break
                    written_bytes += len(chunk)
                    if written_bytes > _MAX_EXPANDED_BYTES:
                        raise PackageFetchError(
                            "candidate archive exceeds the expanded byte limit"
                        )
                    output.write(chunk)
            if target.stat().st_size != entry.file_size:
                raise PackageFetchError(
                    "candidate archive entry size does not match metadata"
                )
        return tuple(
            {
                **link,
                "target_content_sha256": _sha256_file(
                    destination / Path(*PurePosixPath(link["resolved_target"]).parts)
                ),
            }
            for link in links
        )


def _validate_archive_entry(
    entry: zipfile.ZipInfo,
    relative: Path,
) -> str:
    for part in relative.parts:
        trimmed = part.rstrip(" .")
        stem = trimmed.split(".", 1)[0].casefold()
        if (
            not trimmed
            or trimmed != part
            or ":" in part
            or stem in {
                "aux",
                "clock$",
                "con",
                "nul",
                "prn",
                *(f"com{index}" for index in range(1, 10)),
                *(f"lpt{index}" for index in range(1, 10)),
            }
        ):
            raise PackageFetchError(
                "candidate archive contains a non-portable path"
            )
    mode = entry.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        return "symlink"
    if file_type not in {0, stat.S_IFDIR, stat.S_IFREG}:
        raise PackageFetchError(
            "candidate archive contains an unsupported object"
        )
    if entry.is_dir() or file_type == stat.S_IFDIR:
        return "directory"
    return "file"


def _validate_archive_links(
    archive: zipfile.ZipFile,
    members: dict[PurePosixPath, tuple[zipfile.ZipInfo, str]],
) -> tuple[dict[str, str], ...]:
    links: list[dict[str, str]] = []
    for link_path, (entry, entry_type) in sorted(
        members.items(),
        key=lambda item: item[0].as_posix(),
    ):
        if entry_type != "symlink":
            continue
        if entry.file_size < 1 or entry.file_size > _MAX_LINK_TARGET_BYTES:
            raise PackageFetchError(
                "candidate archive symbolic-link target size is invalid"
            )
        raw_target = archive.read(entry)
        if len(raw_target) != entry.file_size:
            raise PackageFetchError(
                "candidate archive symbolic-link target size does not match"
            )
        try:
            target_text = raw_target.decode("utf-8")
        except UnicodeError as error:
            raise PackageFetchError(
                "candidate archive symbolic-link target is not UTF-8"
            ) from error
        target = PurePosixPath(target_text)
        if (
            not target_text
            or "\0" in target_text
            or "\\" in target_text
            or target.is_absolute()
            or target_text.startswith(("/", "//"))
            or ":" in target_text
            or any(part in {"", ".", ".."} for part in target.parts)
        ):
            raise PackageFetchError(
                "candidate archive symbolic-link target is unsafe"
            )
        resolved_target = link_path.parent / target
        resolved = members.get(resolved_target)
        if resolved is None or resolved[1] != "file":
            raise PackageFetchError(
                "candidate archive symbolic link must target a regular file"
            )
        links.append(
            {
                "relative_path": link_path.as_posix(),
                "target": target_text,
                "resolved_target": resolved_target.as_posix(),
            }
        )
    return tuple(links)


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _changelog_identity(root: Path) -> dict[str, str | None]:
    names = ("CHANGELOG.md", "CHANGES.md", "HISTORY.md")
    for name in names:
        path = root / name
        if path.is_file():
            return {
                "relative_path": name,
                "content_sha256": sha256_bytes(path.read_bytes()),
            }
    return {"relative_path": None, "content_sha256": None}


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _is_git_oid(value: str) -> bool:
    return (
        len(value) in {40, 64}
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )
