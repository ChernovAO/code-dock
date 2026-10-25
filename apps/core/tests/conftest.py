import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from core.main import create_app
from core.settings import Settings

type Handler = Callable[[httpx2.Request], httpx2.Response]


class Opencode:
    """Мок opencode: ответы по `(метод, путь)`, запросы копятся в `seen`.

    На один маршрут можно задать несколько ответов: они отдаются по очереди,
    последний повторяется.
    """

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[Handler]] = {}
        self.seen: list[httpx2.Request] = []

    def on(self, method: str, path: str, data: Any = None, status: int = 200) -> None:
        def reply(request: httpx2.Request) -> httpx2.Response:
            if status == 204:
                return httpx2.Response(204)
            if status >= 400:
                return httpx2.Response(status, json=data)
            wrapped = isinstance(data, dict) and "data" in data
            return httpx2.Response(status, json=data if wrapped else {"data": data})

        self.routes.setdefault((method, path), []).append(reply)

    def sse(self, *frames: Any) -> None:
        """Ещё одно подключение к `/api/event`: кадры, затем конец потока."""
        text = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)

        def reply(request: httpx2.Request) -> httpx2.Response:
            headers = {"content-type": "text/event-stream"}
            return httpx2.Response(200, headers=headers, text=text)

        self.routes.setdefault(("GET", "/api/event"), []).append(reply)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        queue = self.routes.get((request.method, request.url.path))
        if not queue:
            raise AssertionError(f"Неожиданный запрос: {request.method} {request.url}")
        route = queue.pop(0) if len(queue) > 1 else queue[0]
        return route(request)

    def bodies(self, method: str, path: str) -> list[Any]:
        return [
            json.loads(r.content) if r.content else None
            for r in self.seen
            if (r.method, r.url.path) == (method, path)
        ]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def repos_dir(tmp_path: Path) -> Path:
    """`notes` — git с проектами, `plain` — не git, `evil` — git с symlink наружу."""
    base = tmp_path / "repos"
    (base / "notes" / ".git").mkdir(parents=True)
    (base / "notes" / "research" / "drafts").mkdir(parents=True)
    (base / "notes" / "ideas").mkdir()
    (base / "notes" / "readme.md").write_text("x")
    (base / "notes" / "inner").symlink_to(base / "notes" / "research")
    (base / "plain").mkdir()
    (base / "evil" / ".git").mkdir(parents=True)
    (base / "evil" / "leak").symlink_to("/etc")
    (base / "file.txt").write_text("x")
    (base / "away").symlink_to(tmp_path)
    return base


@pytest.fixture
def opencode() -> Opencode:
    return Opencode()


@pytest.fixture
def api(repos_dir: Path, opencode: Opencode) -> Iterator[TestClient]:
    settings = Settings(repos_dir, "http://opencode:4096", "secret")
    app = create_app(lambda: settings, httpx2.MockTransport(opencode.handle))
    with TestClient(app) as client:
        yield client
