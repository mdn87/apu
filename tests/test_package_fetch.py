from __future__ import annotations

import io
import os
import stat
import zipfile
from pathlib import Path

import pytest

from apu.models import sha256_bytes
from apu.package_fetch import (
    PackageFetchError,
    _isolated_git_environment,
    fetch_git_candidate,
)


class FakeGit:
    def __init__(self) -> None:
        self.commit_9 = "9" * 40
        self.commit_10 = "a" * 40
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []

    def __call__(self, arguments, cwd):
        args = tuple(arguments)
        self.calls.append((args, cwd))
        if args[:2] == ("ls-remote", "--tags"):
            return "\n".join(
                (
                    f"{self.commit_9}\trefs/tags/v9.0.0",
                    f"{self.commit_10}\trefs/tags/v10.0.0",
                    f"{'b' * 40}\trefs/tags/v11.0.0-beta.1",
                )
            )
        raise AssertionError(args)


def _archive(
    files: dict[str, bytes] | None = None,
    *,
    extra: tuple[zipfile.ZipInfo, bytes] | None = None,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, content in (
            files
            or {
                "SKILL.md": b"Always use this skill before every task.",
                "CHANGELOG.md": b"release notes",
            }
        ).items():
            info = zipfile.ZipInfo(f"superpowers-commit/{relative}")
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, content)
        if extra is not None:
            archive.writestr(*extra)
    return output.getvalue()


def test_fetch_resolves_latest_stable_to_immutable_tree(tmp_path: Path) -> None:
    fake = FakeGit()
    fetched: list[tuple[str, int]] = []

    def archive_fetcher(url: str, limit: int) -> bytes:
        fetched.append((url, limit))
        return _archive()

    artifact, artifact_path, tree = fetch_git_candidate(
        package_id="claude:superpowers@official",
        source_url="https://github.com/example/superpowers.git",
        state_home=tmp_path / "state",
        retrieved_at="2026-08-07T02:00:00Z",
        git_runner=fake,
        archive_fetcher=archive_fetcher,
    )

    assert artifact["version"] == "10.0.0"
    assert artifact["immutable_ref"]["commit_oid"] == "a" * 40
    assert artifact["immutable_ref"]["archive_sha256"]
    assert artifact["immutable_ref"]["content_tree_sha256"] == tree.name
    assert artifact["immutable_ref"]["tree_sha256"] != tree.name
    assert artifact["normalization"] == {
        "policy": "virtual-internal-file-links-v1",
        "links": [],
    }
    assert artifact["changelog"]["content_sha256"]
    assert artifact_path.is_file()
    assert (tree / "SKILL.md").is_file()
    assert not (tree / ".git").exists()
    assert fetched[0][0].endswith("/example/superpowers/zip/" + "a" * 40)


def test_fetch_rejects_ambiguous_versions(tmp_path: Path) -> None:
    class Ambiguous(FakeGit):
        def __call__(self, arguments, cwd):
            if tuple(arguments)[:2] == ("ls-remote", "--tags"):
                return "\n".join(
                    (
                        f"{'a' * 40}\trefs/tags/v10.0.0",
                        f"{'b' * 40}\trefs/tags/10.0.0",
                    )
                )
            return super().__call__(arguments, cwd)

    with pytest.raises(PackageFetchError, match="ambiguously"):
        fetch_git_candidate(
            package_id="claude:superpowers@official",
            source_url="https://github.com/example/superpowers.git",
            state_home=tmp_path / "other-state",
            git_runner=Ambiguous(),
            archive_fetcher=lambda _url, _limit: _archive(),
        )


def test_fetch_requires_credential_free_https_and_exact_stable_version(
    tmp_path: Path,
) -> None:
    for url in (
        "http://example.test/repo.git",
        "https://user:secret@example.test/repo.git",
        "ssh://example.test/repo.git",
        "https://example.test/repo.git?token=secret",
        "https://unapproved.example/repo.git",
    ):
        with pytest.raises(PackageFetchError, match="credential-free HTTPS"):
            fetch_git_candidate(
                package_id="claude:superpowers@official",
                source_url=url,
                state_home=tmp_path / "state",
                git_runner=FakeGit(),
                archive_fetcher=lambda _url, _limit: _archive(),
            )

    with pytest.raises(PackageFetchError, match="stable release"):
        fetch_git_candidate(
            package_id="claude:superpowers@official",
            source_url="https://github.com/example/repo.git",
            state_home=tmp_path / "state",
            requested_version="11.0.0-beta.1",
            git_runner=FakeGit(),
            archive_fetcher=lambda _url, _limit: _archive(),
        )


def test_git_discovery_environment_drops_host_configuration_and_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": "Authorization: secret",
        "GIT_HTTP_EXTRA_HEADER": "Authorization: secret",
        "HTTPS_PROXY": "https://user:secret@proxy.example",
        "ALL_PROXY": "socks5://proxy.example",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    environment = _isolated_git_environment(tmp_path / "isolated")

    assert not hostile.keys() & environment.keys()
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["HOME"] == str(tmp_path / "isolated")


@pytest.mark.parametrize(
    "unsafe",
    (
        "traversal",
        "rooted-backslash",
        "embedded-backslash",
        "trailing-dot",
        "trailing-space",
        "trailing-alias",
        "case-collision",
        "symlink",
    ),
)
def test_fetch_rejects_unsafe_archives(
    tmp_path: Path,
    unsafe: str,
) -> None:
    if unsafe == "traversal":
        info = zipfile.ZipInfo("superpowers-commit/../escape")
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        content = _archive({}, extra=(info, b"escape"))
    elif unsafe == "rooted-backslash":
        info = zipfile.ZipInfo("superpowers-commit/\\escape")
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        content = _archive({}, extra=(info, b"escape"))
    elif unsafe == "embedded-backslash":
        info = zipfile.ZipInfo("superpowers-commit/nested\\..\\escape")
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o644) << 16
        content = _archive({}, extra=(info, b"escape"))
    elif unsafe == "trailing-dot":
        content = _archive({"unsafe.": b"escape"})
    elif unsafe == "trailing-space":
        content = _archive({"unsafe ": b"escape"})
    elif unsafe == "trailing-alias":
        content = _archive({"unsafe": b"one", "unsafe.": b"two"})
    elif unsafe == "case-collision":
        content = _archive({"Policy.md": b"one", "policy.md": b"two"})
    else:
        info = zipfile.ZipInfo("superpowers-commit/link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        content = _archive({}, extra=(info, b"../outside"))

    with pytest.raises(PackageFetchError, match="archive"):
        fetch_git_candidate(
            package_id="claude:superpowers@official",
            source_url="https://github.com/example/superpowers.git",
            state_home=tmp_path / unsafe,
            git_runner=FakeGit(),
            archive_fetcher=lambda _url, _limit: content,
        )


def test_fetch_represents_safe_internal_file_link_without_creating_it(
    tmp_path: Path,
) -> None:
    info = zipfile.ZipInfo("superpowers-commit/AGENTS.md")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    content = _archive(
        {"CLAUDE.md": b"Always follow the package guidance."},
        extra=(info, b"CLAUDE.md"),
    )

    artifact, _, tree = fetch_git_candidate(
        package_id="claude:superpowers@official",
        source_url="https://github.com/example/superpowers.git",
        state_home=tmp_path / "safe-link",
        git_runner=FakeGit(),
        archive_fetcher=lambda _url, _limit: content,
    )

    assert not (tree / "AGENTS.md").exists()
    assert (tree / "CLAUDE.md").is_file()
    assert artifact["normalization"]["links"] == [
        {
            "relative_path": "AGENTS.md",
            "target": "CLAUDE.md",
            "resolved_target": "CLAUDE.md",
            "target_content_sha256": sha256_bytes(
                b"Always follow the package guidance."
            ),
        }
    ]
