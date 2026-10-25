"""Приложение FastAPI: настройки, клиент opencode и перевод ошибок в HTTP."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core import api
from core.opencode import InvalidIdError, OpencodeClient, OpencodeError
from core.paths import PathError, PathNotFoundError
from core.repos import RepoError
from core.settings import Settings

# Коды opencode, которые понятны клиенту как есть. Остальное — сбой за `core`.
PASSTHROUGH = {400, 404, 409}


def create_app(
    settings: Callable[[], Settings] = Settings.from_env,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Настройки читаются на старте приложения, клиент opencode один на приложение.

    `transport` подменяет сеть до opencode в тестах.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = config = settings()
        async with OpencodeClient(
            config.opencode_url, config.opencode_password, transport=transport
        ) as client:
            app.state.opencode = client
            yield

    app = FastAPI(title="Code Dock", lifespan=lifespan)
    app.include_router(api.router)
    # Только свои ошибки: прочие `ValueError` — сбой `core`, а не запроса.
    app.add_exception_handler(PathError, _bad_request)
    app.add_exception_handler(InvalidIdError, _bad_request)
    app.add_exception_handler(PathNotFoundError, _not_found)
    app.add_exception_handler(RepoError, _repo_error)
    app.add_exception_handler(OpencodeError, _opencode_error)
    return app


async def _bad_request(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=400)


async def _not_found(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=404)


async def _repo_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RepoError)
    return JSONResponse(
        {"detail": exc.message, "symlinks": exc.symlinks}, status_code=409
    )


async def _opencode_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, OpencodeError)
    if exc.status in PASSTHROUGH:
        status, detail = exc.status, exc.message
    elif exc.status == 401:
        status, detail = 502, "opencode отклонил пароль OPENCODE_SERVER_PASSWORD"
    else:
        status, detail = 502, f"opencode не ответил: {exc.message}"
    return JSONResponse({"detail": detail, "tag": exc.tag}, status_code=status)


app = create_app()
