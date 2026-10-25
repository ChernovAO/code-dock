"""Эндпоинты `/api`. Пути и имена из запроса проходят слой путей до opencode."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from core import paths, repos
from core.chat import stream
from core.opencode import Decision, FormValue, Json, OpencodeClient, Rule
from core.repos import Repo
from core.settings import Settings

# Правила каждого нового чата. `permissions` чата вычисляются последними, поэтому
# запрет Code Mode держится, даже если системный конфиг не загрузился (так уже
# было из-за прав на `/etc/opencode`, см. prototype-plan.md).
CHAT_RULES = (Rule(action="execute", resource="*", effect="deny"),)

router = APIRouter(prefix="/api")


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_opencode(request: Request) -> OpencodeClient:
    client: OpencodeClient = request.app.state.opencode
    return client


type SettingsDep = Annotated[Settings, Depends(get_settings)]
type OpencodeDep = Annotated[OpencodeClient, Depends(get_opencode)]


class Folders(BaseModel):
    path: str
    folders: list[str]


class Project(BaseModel):
    path: str
    chats: int
    updated: float | None


class NewChat(BaseModel):
    path: str = ""
    title: str | None = None


class Message(BaseModel):
    text: str = Field(min_length=1)


class PermissionReply(BaseModel):
    decision: Decision
    message: str | None = None


class FormReply(BaseModel):
    answer: dict[str, FormValue]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# Репозитории и проекты


@router.get("/repos")
def list_repos(settings: SettingsDep) -> list[Repo]:
    return repos.list_repos(settings.repos_dir)


@router.get("/repos/{repo}/folders")
def list_folders(repo: str, settings: SettingsDep, path: str = "") -> Folders:
    """Папки внутри `path`: из них выбирают проект."""
    root = _root(settings, repo)
    return Folders(path=path, folders=repos.list_folders(root, path))


@router.get("/repos/{repo}/projects")
async def list_projects(
    repo: str, settings: SettingsDep, opencode: OpencodeDep
) -> list[Project]:
    """Папки репозитория, в которых есть чаты. Свежие первыми.

    Чаты сравниваются с развёрнутым корнем: имя-symlink на соседний
    репозиторий показывает его проекты, как и `list_chats`.
    """
    root = _root(settings, repo)
    found: dict[str, list[Json]] = {}
    for chat in await opencode.list_sessions():
        path = repos.project_path(root, _directory(chat))
        if path is not None:
            found.setdefault(path, []).append(chat)
    projects = [
        Project(path=path, chats=len(chats), updated=_latest(chats))
        for path, chats in found.items()
    ]
    return sorted(projects, key=lambda p: p.updated or 0, reverse=True)


@router.get("/repos/{repo}/chats")
async def list_chats(
    repo: str, settings: SettingsDep, opencode: OpencodeDep, path: str = ""
) -> list[Json]:
    """Чаты проекта, новые первыми.

    Папку не обязательно иметь на диске: проект из `list_projects` остаётся
    в списке и после удаления или переименования папки.
    """
    directory = paths.resolve(_root(settings, repo), path)
    return await opencode.list_sessions(directory=str(directory))


# Чаты


@router.post("/repos/{repo}/chats", status_code=201)
async def create_chat(
    repo: str, body: NewChat, settings: SettingsDep, opencode: OpencodeDep
) -> Json:
    """Новый чат в папке проекта. opencode папку не проверяет, проверяет `core`."""
    directory = repos.project_dir(repos.open_repo(settings.repos_dir, repo), body.path)
    return await opencode.create_session(
        str(directory), title=body.title, permissions=CHAT_RULES
    )


async def get_place(
    chat_id: str, settings: SettingsDep, opencode: OpencodeDep
) -> tuple[str, str]:
    """Репозиторий и проект чата. Чужие чаты opencode (вне `/repos`) — 404."""
    found = await opencode.get_session(chat_id)
    place = repos.locate(settings.repos_dir, _directory(found))
    if place is None:
        raise paths.PathNotFoundError(f"Чат {chat_id!r} не относится к репозиториям")
    return place


type PlaceDep = Annotated[tuple[str, str], Depends(get_place)]


@router.post("/chats/{chat_id}/messages")
async def send_message(
    chat_id: str,
    body: Message,
    place: PlaceDep,
    settings: SettingsDep,
    opencode: OpencodeDep,
) -> Json:
    """Отправляет сообщение. Ход агента идёт в фоне, результат приходит в SSE."""
    _check_repo(settings, place)
    return await opencode.prompt(chat_id, body.text)


@router.get(
    "/chats/{chat_id}/events",
    response_class=EventSourceResponse,
    dependencies=[Depends(get_place)],
)
async def chat_events(
    chat_id: str, opencode: OpencodeDep
) -> AsyncIterator[ServerSentEvent]:
    """SSE чата: снимок, затем события. После обрыва opencode — новый снимок."""
    async for item in stream(opencode, chat_id):
        yield item


@router.post(
    "/chats/{chat_id}/permissions/{request_id}",
    status_code=204,
)
async def reply_permission(
    chat_id: str,
    request_id: str,
    body: PermissionReply,
    place: PlaceDep,
    settings: SettingsDep,
    opencode: OpencodeDep,
) -> None:
    """`always` сохраняется на весь репозиторий, а не на проект.

    Разрешение продолжает ход агента, поэтому репозиторий проверяется, как
    перед сообщением. Отказ проходит всегда: он ход останавливает.
    """
    if body.decision != "reject":
        _check_repo(settings, place)
    await opencode.reply_permission(chat_id, request_id, body.decision, body.message)


@router.post(
    "/chats/{chat_id}/forms/{form_id}",
    status_code=204,
)
async def reply_form(
    chat_id: str,
    form_id: str,
    body: FormReply,
    place: PlaceDep,
    settings: SettingsDep,
    opencode: OpencodeDep,
) -> None:
    """Ответ продолжает ход агента: репозиторий проверяется, как перед сообщением."""
    _check_repo(settings, place)
    await opencode.reply_form(chat_id, form_id, body.answer)


@router.delete(
    "/chats/{chat_id}/forms/{form_id}",
    status_code=204,
    dependencies=[Depends(get_place)],
)
async def cancel_form(
    chat_id: str,
    form_id: str,
    opencode: OpencodeDep,
    message: str | None = None,
) -> None:
    await opencode.cancel_form(chat_id, form_id, message)


def _root(settings: Settings, repo: str) -> Path:
    return paths.repo_root(settings.repos_dir, repo)


def _check_repo(settings: Settings, place: tuple[str, str]) -> None:
    """Проверка перед ходом агента: symlink могли прийти с pull или от агента."""
    name, path = place
    repos.project_dir(repos.open_repo(settings.repos_dir, name), path)


def _directory(chat: Json) -> str:
    """Папка чата. Ответ opencode не проверен: без папки чат ни к чему не относится."""
    location = chat.get("location")
    directory = location.get("directory") if isinstance(location, dict) else None
    return directory if isinstance(directory, str) else ""


def _latest(chats: list[Json]) -> float | None:
    times = [
        updated
        for chat in chats
        if isinstance(time := chat.get("time"), dict)
        and isinstance(updated := time.get("updated"), int | float)
    ]
    return max(times, default=None)
