from pathlib import Path

import pytest
from conftest import Opencode
from fastapi.testclient import TestClient

from core import repos
from core.paths import PathError, PathNotFoundError


def chat(id: str, directory: str, updated: float) -> dict[str, object]:
    return {
        "id": id,
        "location": {"directory": directory},
        "time": {"updated": updated},
    }


def test_health(api: TestClient) -> None:
    response = api.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_list_repos(api: TestClient) -> None:
    response = api.get("/api/repos")
    assert response.status_code == 200
    assert response.json() == [
        {"name": "evil", "git": True, "symlinks": ["leak"]},
        {"name": "notes", "git": True, "symlinks": []},
        {"name": "plain", "git": False, "symlinks": []},
    ]


def test_is_git(tmp_path: Path) -> None:
    assert not repos.is_git(tmp_path)
    (tmp_path / ".git").write_text("gitdir: ../main/.git/worktrees/x")
    assert repos.is_git(tmp_path)
    (tmp_path / ".git").unlink()
    (tmp_path / ".git").symlink_to(tmp_path)
    assert not repos.is_git(tmp_path)


def test_list_folders(api: TestClient) -> None:
    response = api.get("/api/repos/notes/folders")
    assert response.json() == {"path": "", "folders": ["ideas", "research"]}
    response = api.get("/api/repos/notes/folders", params={"path": "research"})
    assert response.json() == {"path": "research", "folders": ["drafts"]}


@pytest.mark.parametrize(
    ("repo", "path", "status"),
    [
        ("notes", "../evil", 400),
        ("notes", ".git", 400),
        ("notes", "inner", 400),
        ("notes", "missing", 404),
        ("missing", "", 404),
        ("away", "", 400),
        ("..", "", 404),
    ],
)
def test_list_folders_rejects(
    api: TestClient, repo: str, path: str, status: int
) -> None:
    response = api.get(f"/api/repos/{repo}/folders", params={"path": path})
    assert response.status_code == status
    assert response.json()["detail"]


def test_list_projects(api: TestClient, opencode: Opencode, repos_dir: Path) -> None:
    notes = repos_dir.resolve() / "notes"
    opencode.on(
        "GET",
        "/api/session",
        {
            "data": [
                chat("ses_1", f"{notes}/research", 10),
                chat("ses_2", str(notes), 30),
                chat("ses_3", f"{notes}/research", 20),
                chat("ses_4", f"{repos_dir.resolve()}/evil", 40),
                chat("ses_5", "/elsewhere", 50),
                {"id": "ses_6"},
                # Ответ opencode не проверен: `time` без объекта не роняет список.
                {"id": "ses_7", "location": {"directory": str(notes)}, "time": 5},
            ],
            "cursor": {"next": None},
        },
    )
    response = api.get("/api/repos/notes/projects")
    assert response.status_code == 200
    assert response.json() == [
        {"path": "", "chats": 2, "updated": 30},
        {"path": "research", "chats": 2, "updated": 20},
    ]


def test_list_projects_symlinked_repo(
    api: TestClient, opencode: Opencode, repos_dir: Path
) -> None:
    """Имя-ссылка на соседний репозиторий видит его чаты, как и `list_chats`."""
    (repos_dir / "alias").symlink_to(repos_dir / "notes")
    notes = repos_dir.resolve() / "notes"
    opencode.on(
        "GET",
        "/api/session",
        {"data": [chat("ses_1", f"{notes}/research", 10)], "cursor": {}},
    )
    response = api.get("/api/repos/alias/projects")
    assert response.json() == [{"path": "research", "chats": 1, "updated": 10}]


def test_list_chats(api: TestClient, opencode: Opencode, repos_dir: Path) -> None:
    opencode.on("GET", "/api/session", {"data": [{"id": "ses_1"}], "cursor": {}})
    response = api.get("/api/repos/notes/chats", params={"path": "research/"})
    assert response.json() == [{"id": "ses_1"}]
    sent = opencode.seen[0].url.params["directory"]
    assert sent == str(repos_dir.resolve() / "notes" / "research")


def test_list_chats_folder_gone(
    api: TestClient, opencode: Opencode, repos_dir: Path
) -> None:
    """Проект из `list_projects` открывается и после удаления папки."""
    opencode.on("GET", "/api/session", {"data": [{"id": "ses_1"}], "cursor": {}})
    response = api.get("/api/repos/notes/chats", params={"path": "gone"})
    assert response.json() == [{"id": "ses_1"}]
    sent = opencode.seen[0].url.params["directory"]
    assert sent == str(repos_dir.resolve() / "notes" / "gone")


@pytest.mark.parametrize("path", ["../evil", ".git"])
def test_list_chats_rejects(api: TestClient, opencode: Opencode, path: str) -> None:
    response = api.get("/api/repos/notes/chats", params={"path": path})
    assert response.status_code == 400
    assert opencode.seen == []


def test_open_repo(repos_dir: Path) -> None:
    assert repos.open_repo(repos_dir, "notes") == repos_dir.resolve() / "notes"
    with pytest.raises(repos.RepoError, match="не git") as info:
        repos.open_repo(repos_dir, "plain")
    assert info.value.symlinks == []
    with pytest.raises(repos.RepoError, match="symlink") as info:
        repos.open_repo(repos_dir, "evil")
    assert info.value.symlinks == ["leak"]


def test_project_dir(repos_dir: Path) -> None:
    root = repos_dir.resolve() / "notes"
    assert repos.project_dir(root, "") == root
    assert repos.project_dir(root, "./research/drafts") == root / "research" / "drafts"
    with pytest.raises(PathError):
        repos.project_dir(root, "inner")
    with pytest.raises(PathNotFoundError):
        repos.project_dir(root, "readme.md")


def test_project_path(repos_dir: Path) -> None:
    root = repos_dir.resolve() / "notes"
    assert repos.project_path(root, str(root)) == ""
    assert repos.project_path(root, f"{root}/a/b") == "a/b"
    assert repos.project_path(root, f"{repos_dir.resolve()}/evil") is None
    assert repos.project_path(root, "") is None


def test_locate(repos_dir: Path) -> None:
    base = repos_dir.resolve()
    assert repos.locate(repos_dir, f"{base}/notes") == ("notes", "")
    assert repos.locate(repos_dir, f"{base}/notes/a/b") == ("notes", "a/b")
    assert repos.locate(repos_dir, str(base)) is None
    assert repos.locate(repos_dir, "/etc") is None
    assert repos.locate(repos_dir, "") is None
