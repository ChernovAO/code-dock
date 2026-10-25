import os
from pathlib import Path

import pytest

from core.paths import (
    PathError,
    PathNotFoundError,
    external_symlinks,
    open_inside,
    relative_to_root,
    repo_root,
    resolve,
)


@pytest.fixture
def repos(tmp_path: Path) -> Path:
    repos = tmp_path / "repos"
    (repos / "notes" / "research").mkdir(parents=True)
    (repos / "notes" / ".git").mkdir()
    (repos / "notes" / "research" / "a.md").write_text("a")
    (repos / "other").mkdir()
    (tmp_path / "outside").mkdir()
    return repos


@pytest.fixture
def root(repos: Path) -> Path:
    return repo_root(repos, "notes")


def test_repo_root(repos: Path) -> None:
    assert repo_root(repos, "notes") == (repos / "notes").resolve()


@pytest.mark.parametrize("name", [".github", "заметки", " notes ", "a b"])
def test_repo_root_any_folder_name(repos: Path, name: str) -> None:
    (repos / name).mkdir()
    assert repo_root(repos, name) == (repos / name).resolve()


@pytest.mark.parametrize(
    "name", ["", ".", "..", ".git", ".GIT", "a/b", "../notes", "/repos", "x\0"]
)
def test_repo_root_rejects_bad_names(repos: Path, name: str) -> None:
    with pytest.raises(PathError):
        repo_root(repos, name)


def test_repo_root_missing(repos: Path) -> None:
    with pytest.raises(PathNotFoundError):
        repo_root(repos, "absent")
    (repos / "file").write_text("x")
    with pytest.raises(PathNotFoundError):
        repo_root(repos, "file")


def test_repo_root_symlink_outside(repos: Path) -> None:
    (repos / "escape").symlink_to(repos.parent / "outside")
    with pytest.raises(PathError):
        repo_root(repos, "escape")


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("", ""),
        (".", ""),
        ("research", "research"),
        ("./research//a.md", "research/a.md"),
        ("research/new.md", "research/new.md"),
    ],
)
def test_resolve(root: Path, relative: str, expected: str) -> None:
    assert relative_to_root(root, resolve(root, relative)) == expected


@pytest.mark.parametrize(
    "relative",
    [
        "/etc/passwd",
        "..",
        "../other",
        "research/../../other",
        "research/../a.md",
        ".git",
        ".git/config",
        "research/.git/config",
        ".GIT/hooks",
        "a\0b",
    ],
)
def test_resolve_rejects(root: Path, relative: str) -> None:
    with pytest.raises(PathError):
        resolve(root, relative)


def test_resolve_symlink_inside(root: Path) -> None:
    (root / "link.md").symlink_to(root / "research" / "a.md")
    assert resolve(root, "link.md") == root / "research" / "a.md"


@pytest.mark.parametrize("target", ["../outside", "../../other", ".git"])
def test_resolve_symlink_escape(root: Path, target: str) -> None:
    (root / "link").symlink_to(target)
    with pytest.raises(PathError):
        resolve(root, "link")
    with pytest.raises(PathError):
        resolve(root, "link/file.md")


def test_resolve_symlink_itself(root: Path) -> None:
    link = root / "research" / "link"
    link.symlink_to("/etc/passwd")
    assert resolve(root, "research/link", follow_last=False) == link
    (root / "dir").symlink_to("../other")
    with pytest.raises(PathError):
        resolve(root, "dir/x", follow_last=False)


def test_resolve_root_is_never_a_link(root: Path) -> None:
    assert resolve(root, "", follow_last=False) == root


def test_external_symlinks(root: Path) -> None:
    (root / "ok").symlink_to("research/a.md")
    (root / "research" / "passwd").symlink_to("/etc/passwd")
    (root / "other").symlink_to("../other")
    (root / "config").symlink_to(".git/config")
    (root / "dangling").symlink_to("/nonexistent/x")
    (root / "loop").symlink_to("loop")
    (root / ".git" / "ignored").symlink_to("/etc/passwd")
    assert external_symlinks(root) == [
        "config",
        "dangling",
        "other",
        "research/passwd",
    ]


def test_open_inside_reads_and_creates(root: Path) -> None:
    fd = open_inside(root, "research/a.md", os.O_RDONLY)
    with os.fdopen(fd) as f:
        assert f.read() == "a"
    fd = open_inside(root, "research/new.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(fd, "w") as f:
        f.write("new")
    assert (root / "research" / "new.md").read_text() == "new"
    os.close(open_inside(root, "", os.O_RDONLY | os.O_DIRECTORY))


@pytest.mark.parametrize("relative", ["dir/a.md", "link.md", "dir"])
def test_open_inside_refuses_symlinks(root: Path, relative: str) -> None:
    (root / "dir").symlink_to("research")
    (root / "link.md").symlink_to("research/a.md")
    with pytest.raises(PathError, match="symlink"):
        open_inside(root, relative, os.O_RDONLY)


def test_open_inside_swapped_directory(root: Path, repos: Path) -> None:
    """Подмена каталога после `resolve`: открытие не уходит за границу."""
    resolve(root, "research/a.md")
    (repos.parent / "outside" / "a.md").write_text("secret")
    (root / "research").rename(root / "moved")
    (root / "research").symlink_to(repos.parent / "outside")
    with pytest.raises(PathError):
        open_inside(root, "research/a.md", os.O_RDONLY)


@pytest.mark.parametrize("relative", [".git/config", "../other", "/etc/passwd"])
def test_open_inside_rejects(root: Path, relative: str) -> None:
    with pytest.raises(PathError):
        open_inside(root, relative, os.O_RDONLY)


def test_open_inside_missing(root: Path) -> None:
    with pytest.raises(PathNotFoundError):
        open_inside(root, "research/absent.md", os.O_RDONLY)


def test_open_inside_other_errors(root: Path) -> None:
    with pytest.raises(IsADirectoryError):
        open_inside(root, "research", os.O_WRONLY)


@pytest.mark.skipif(os.geteuid() == 0, reason="root читает любой каталог")
def test_external_symlinks_unreadable_dir(root: Path) -> None:
    hidden = root / "hidden"
    hidden.mkdir()
    (hidden / "x").symlink_to("/etc/passwd")
    hidden.chmod(0)
    try:
        assert external_symlinks(root) == ["hidden"]
    finally:
        hidden.chmod(0o755)
