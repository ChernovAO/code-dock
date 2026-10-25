"""Тонкий клиент HTTP API opencode V2 (2.1.2).

Только нужные `core` методы, ответы — JSON как есть, без своих моделей:
модели для событий чата появятся вместе с SSE-эндпоинтом. Файловые методы
`fs/*` не используются (см. `docs/opencode-integration.md`, «Файлы»).
"""

import logging
import re
from collections.abc import AsyncIterator, Sequence
from types import TracebackType
from typing import Any, Literal, Self

import httpx2
from pydantic import BaseModel, ValidationError

type Json = dict[str, Any]
type Decision = Literal["once", "always", "reject"]
type Effect = Literal["allow", "ask", "deny"]

log = logging.getLogger(__name__)

USER = "opencode"
# Сервер шлёт `: heartbeat` каждые 15 с. Тишина дольше — соединение мёртвое.
EVENT_READ_TIMEOUT = 45.0
PAGE_SIZE = 200
# ID чатов и запросов (`ses_…`, `per_…`) идут в путь URL: без `/`, `.` и `?`.
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


class OpencodeError(RuntimeError):
    """Ошибка ответа opencode или транспорта.

    `status` — HTTP-код (0 — сервер недоступен или ответ не понят), `tag` — поле
    `_tag` тела ошибки.
    """

    def __init__(self, status: int, message: str, tag: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.tag = tag
        self.message = message


class Durable(BaseModel):
    aggregateID: str
    seq: int
    version: int


class Event(BaseModel):
    """Кадр общего потока `/api/event`. Тип события — в поле `type`."""

    id: str
    type: str
    created: int | None = None
    location: Json | None = None
    data: Json = {}
    durable: Durable | None = None

    @property
    def session_id(self) -> str | None:
        value = self.data.get("sessionID")
        return value if isinstance(value, str) else None


class Rule(BaseModel):
    """Правило `permissions` чата. Вычисляется последним и перекрывает конфиги."""

    action: str
    resource: str
    effect: Effect


class OpencodeClient:
    def __init__(
        self,
        base_url: str,
        password: str,
        *,
        timeout: float = 30.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._http = httpx2.AsyncClient(
            base_url=base_url,
            auth=httpx2.BasicAuth(USER, password),
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # Чаты

    async def list_sessions(
        self,
        *,
        directory: str | None = None,
        project: str | None = None,
        subpath: str | None = None,
        search: str | None = None,
    ) -> list[Json]:
        """Все чаты по фильтрам, новые первыми."""
        params = {
            "directory": directory,
            "project": project,
            "subpath": subpath,
            "search": search,
            "order": "desc",
        }
        return await self._pages("/api/session", params)

    async def create_session(
        self,
        directory: str,
        *,
        title: str | None = None,
        agent: str | None = None,
        model: Json | None = None,
        permissions: Sequence[Rule] = (),
    ) -> Json:
        """Новый чат в папке проекта.

        opencode папку не проверяет: чат создаётся и для несуществующего пути.
        Путь должен пройти слой путей `core` до вызова.
        """
        body: Json = {"location": {"directory": directory}}
        if title is not None:
            body["title"] = title
        if agent is not None:
            body["agent"] = agent
        if model is not None:
            body["model"] = model
        if permissions:
            body["permissions"] = [rule.model_dump() for rule in permissions]
        return await self._object("POST", "/api/session", json=body)

    async def get_session(self, session_id: str) -> Json:
        return await self._object("GET", f"/api/session/{_id(session_id)}")

    async def prompt(self, session_id: str, text: str) -> Json:
        """Отправляет сообщение. Ответ — сообщение пользователя, ход идёт в фоне."""
        path = f"/api/session/{_id(session_id)}/prompt"
        return await self._object("POST", path, json={"text": text})

    async def messages(self, session_id: str) -> list[Json]:
        """Вся история чата, от старых к новым."""
        path = f"/api/session/{_id(session_id)}/message"
        return await self._pages(path, {"order": "asc"})

    async def interrupt(self, session_id: str) -> bool:
        """Прерывает ход. `False` — чат и так не выполнялся."""
        path = f"/api/session/{_id(session_id)}/interrupt"
        payload = await self._json("POST", path)
        return bool(payload.get("interrupted"))

    # Разрешения

    async def permissions(self, session_id: str) -> list[Json]:
        """Ожидающие ответа запросы разрешений чата."""
        path = f"/api/session/{_id(session_id)}/permission"
        found = (await self._json("GET", path)).get("data")
        if not isinstance(found, list):
            raise OpencodeError(0, f"Нет списка `data` в ответе {path}")
        return found

    async def reply_permission(
        self,
        session_id: str,
        request_id: str,
        decision: Decision,
        message: str | None = None,
    ) -> None:
        """Ответ на запрос. `always` сохраняется на весь репозиторий."""
        body: Json = {"decision": decision}
        if message is not None:
            body["message"] = message
        path = f"/api/session/{_id(session_id)}/permission/{_id(request_id)}/reply"
        await self._json("POST", path, json=body)

    # События

    async def events(self) -> AsyncIterator[Event]:
        """Живой поток событий всех чатов сервера.

        Кадры, которые не разобрались в `Event`, пропускаются с предупреждением
        в лог. Без replay: события во время обрыва теряются. Итератор заканчивается
        вместе с соединением. После обрыва подпишитесь заново и перечитайте
        историю (`messages`).
        """
        timeout = httpx2.Timeout(self._timeout, read=EVENT_READ_TIMEOUT)
        try:
            async with self._http.sse("/api/event", timeout=timeout) as source:
                await self._raise_for_status(source.response)
                async for sse in source:
                    try:
                        yield Event.model_validate_json(sse.data)
                    except ValidationError as e:
                        # Один чужой кадр не должен рвать поток всех чатов.
                        log.warning(
                            "Пропущен непонятный кадр /api/event: %s; %.200s",
                            e.error_count(),
                            sse.data,
                        )
        except httpx2.HTTPError as e:
            raise OpencodeError(0, f"Поток /api/event оборвался: {e}") from e

    # Транспорт

    async def _pages(self, path: str, params: dict[str, str | None]) -> list[Json]:
        """Все страницы списка. `cursor.next` не пустеет до пустой страницы."""
        query = {key: value for key, value in params.items() if value is not None}
        query["limit"] = str(PAGE_SIZE)
        items: list[Json] = []
        seen: set[str] = set()
        while True:
            payload = await self._json("GET", path, params=query)
            page = payload.get("data") or []
            items.extend(page)
            cursor = (payload.get("cursor") or {}).get("next")
            if not page or not cursor:
                return items
            if cursor in seen:
                raise OpencodeError(0, f"Курсор {path} повторился: {cursor!r}")
            seen.add(cursor)
            # Курсор нельзя сочетать с `order`, фильтры он несёт в себе.
            query = {"cursor": cursor, "limit": str(PAGE_SIZE)}

    async def _object(self, method: str, path: str, **kwargs: Any) -> Json:
        data = (await self._json(method, path, **kwargs)).get("data")
        if not isinstance(data, dict):
            raise OpencodeError(0, f"Нет объекта `data` в ответе {path}")
        return data

    async def _json(self, method: str, path: str, **kwargs: Any) -> Json:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx2.HTTPError as e:
            raise OpencodeError(0, f"opencode недоступен: {e}") from e
        await self._raise_for_status(response)
        if not response.content:
            return {}
        try:
            payload = response.json()
        except ValueError as e:
            raise OpencodeError(0, f"Ответ {path} не JSON: {e}") from e
        if not isinstance(payload, dict):
            raise OpencodeError(0, f"Ответ {path} не JSON-объект")
        return payload

    @staticmethod
    async def _raise_for_status(response: httpx2.Response) -> None:
        if response.is_success:
            return
        await response.aread()
        tag: str | None = None
        message = response.text[:500] or response.reason_phrase
        try:
            body = response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            # Поля берутся, только если они строки: тело ошибки не проверено.
            if isinstance(found_tag := body.get("_tag"), str):
                tag = found_tag
            if isinstance(found := body.get("message"), str) and found:
                message = found
        raise OpencodeError(response.status_code, message, tag)


def _id(value: str) -> str:
    """ID для пути URL. Иначе `../fs/write` увёл бы запрос на чужой эндпоинт."""
    if not ID_PATTERN.fullmatch(value):
        raise ValueError(f"Недопустимый ID: {value!r}")
    return value
