import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from conftest import Opencode
from fastapi.testclient import TestClient

from core import chat
from core.opencode import OpencodeClient, OpencodeError

SESSION = "/api/session/ses_1"


@pytest.fixture
def notes(repos_dir: Path) -> Path:
    return repos_dir.resolve() / "notes"


@pytest.fixture
def research_chat(opencode: Opencode, notes: Path) -> dict[str, Any]:
    found = {"id": "ses_1", "location": {"directory": f"{notes}/research"}}
    opencode.on("GET", SESSION, found)
    return found


def frames(text: str) -> list[tuple[str, Any]]:
    """Кадры SSE как пары (event, data). Комментарии keepalive пропускаются."""
    found = []
    for block in text.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if not line.startswith(":")
        )
        if fields:
            found.append((fields.get("event", ""), json.loads(fields["data"])))
    return found


def event(type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"evt_{type}", "type": type, "data": data}


# Создание чата


def test_create_chat(api: TestClient, opencode: Opencode, notes: Path) -> None:
    opencode.on("POST", "/api/session", {"id": "ses_1"})
    response = api.post(
        "/api/repos/notes/chats", json={"path": "research", "title": "Черновик"}
    )
    assert response.status_code == 201
    assert response.json() == {"id": "ses_1"}
    assert opencode.bodies("POST", "/api/session") == [
        {
            "location": {"directory": f"{notes}/research"},
            "title": "Черновик",
            "permissions": [{"action": "execute", "resource": "*", "effect": "deny"}],
        }
    ]


def test_create_chat_in_root(api: TestClient, opencode: Opencode, notes: Path) -> None:
    opencode.on("POST", "/api/session", {"id": "ses_1"})
    assert api.post("/api/repos/notes/chats", json={}).status_code == 201
    body = opencode.bodies("POST", "/api/session")[0]
    assert body["location"] == {"directory": str(notes)}
    assert "title" not in body


@pytest.mark.parametrize(
    ("repo", "path", "status", "symlinks"),
    [
        ("evil", "", 409, ["leak"]),
        ("plain", "", 409, []),
        ("notes", "inner", 400, None),
        ("notes", "../evil", 400, None),
        ("notes", ".git", 400, None),
        ("notes", "missing", 404, None),
        ("notes", "readme.md", 404, None),
        ("missing", "", 404, None),
    ],
)
def test_create_chat_rejects(
    api: TestClient,
    opencode: Opencode,
    repo: str,
    path: str,
    status: int,
    symlinks: list[str] | None,
) -> None:
    response = api.post(f"/api/repos/{repo}/chats", json={"path": path})
    assert response.status_code == status
    if symlinks is not None:
        assert response.json()["symlinks"] == symlinks
    assert opencode.seen == []


# Сообщения


def test_send_message(
    api: TestClient, opencode: Opencode, research_chat: dict[str, Any]
) -> None:
    opencode.on("POST", f"{SESSION}/prompt", {"id": "inb_1", "type": "user"})
    response = api.post("/api/chats/ses_1/messages", json={"text": "привет"})
    assert response.status_code == 200
    assert response.json() == {"id": "inb_1", "type": "user"}
    assert opencode.bodies("POST", f"{SESSION}/prompt") == [{"text": "привет"}]


def test_send_message_refuses_symlinks(
    api: TestClient, opencode: Opencode, research_chat: dict[str, Any], notes: Path
) -> None:
    (notes / "research" / "env").symlink_to("/proc/self/environ")
    response = api.post("/api/chats/ses_1/messages", json={"text": "привет"})
    assert response.status_code == 409
    assert response.json()["symlinks"] == ["research/env"]
    assert opencode.bodies("POST", f"{SESSION}/prompt") == []


def test_send_message_folder_gone(
    api: TestClient, opencode: Opencode, research_chat: dict[str, Any], notes: Path
) -> None:
    (notes / "research" / "drafts").rmdir()
    (notes / "research").rmdir()
    response = api.post("/api/chats/ses_1/messages", json={"text": "привет"})
    assert response.status_code == 404


def test_send_empty_message(api: TestClient, research_chat: dict[str, Any]) -> None:
    assert api.post("/api/chats/ses_1/messages", json={"text": ""}).status_code == 422


@pytest.mark.parametrize("directory", ["/elsewhere", None])
def test_chat_outside_repos(
    api: TestClient, opencode: Opencode, directory: str | None
) -> None:
    found: dict[str, Any] = {"id": "ses_1"}
    if directory:
        found["location"] = {"directory": directory}
    opencode.on("GET", SESSION, found)
    response = api.post("/api/chats/ses_1/messages", json={"text": "привет"})
    assert response.status_code == 404
    assert "не относится" in response.json()["detail"]


def test_bad_chat_id(api: TestClient, opencode: Opencode) -> None:
    response = api.post("/api/chats/ses.1/messages", json={"text": "привет"})
    assert response.status_code == 400
    assert response.json() == {"detail": "Недопустимый ID: 'ses.1'"}
    assert opencode.seen == []


@pytest.mark.parametrize(
    ("status", "body", "expected", "detail"),
    [
        (404, {"_tag": "SessionNotFoundError", "message": "нет"}, 404, "нет"),
        (409, {"_tag": "ConflictError", "message": "занят"}, 409, "занят"),
        (401, {"_tag": "UnauthorizedError"}, 502, "OPENCODE_SERVER_PASSWORD"),
        (500, {"message": "сбой"}, 502, "opencode не ответил: сбой"),
    ],
)
def test_opencode_errors(
    api: TestClient,
    opencode: Opencode,
    status: int,
    body: dict[str, str],
    expected: int,
    detail: str,
) -> None:
    opencode.on("GET", SESSION, body, status=status)
    response = api.post("/api/chats/ses_1/messages", json={"text": "привет"})
    assert response.status_code == expected
    assert detail in response.json()["detail"]
    assert response.json()["tag"] == body.get("_tag")


def test_opencode_down(api: TestClient, opencode: Opencode) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("All connection attempts failed", request=request)

    opencode.routes[("GET", SESSION)] = [refuse]
    response = api.post("/api/chats/ses_1/messages", json={"text": "привет"})
    assert response.status_code == 502
    assert response.json()["detail"].startswith("opencode не ответил")


# Разрешения и формы


def test_reply_permission(
    api: TestClient, opencode: Opencode, research_chat: dict[str, Any]
) -> None:
    path = f"{SESSION}/permission/per_1/reply"
    opencode.on("POST", path, status=204)
    url = "/api/chats/ses_1/permissions/per_1"
    assert api.post(url, json={"decision": "once"}).status_code == 204
    body = {"decision": "reject", "message": "нельзя"}
    assert api.post(url, json=body).status_code == 204
    assert api.post(url, json={"decision": "maybe"}).status_code == 422
    assert opencode.bodies("POST", path) == [{"decision": "once"}, body]


def test_reply_permission_refuses_symlinks(
    api: TestClient, opencode: Opencode, research_chat: dict[str, Any], notes: Path
) -> None:
    """Разрешение продолжает ход агента, отказ — нет."""
    path = f"{SESSION}/permission/per_1/reply"
    opencode.on("POST", path, status=204)
    (notes / "research" / "env").symlink_to("/proc/self/environ")
    url = "/api/chats/ses_1/permissions/per_1"
    for decision in ("once", "always"):
        response = api.post(url, json={"decision": decision})
        assert response.status_code == 409
        assert response.json()["symlinks"] == ["research/env"]
    assert api.post(url, json={"decision": "reject"}).status_code == 204
    assert opencode.bodies("POST", path) == [{"decision": "reject"}]


def test_reply_form_refuses_symlinks(
    api: TestClient, opencode: Opencode, research_chat: dict[str, Any], notes: Path
) -> None:
    path = f"{SESSION}/form/frm_1"
    opencode.on("POST", f"{path}/reply", status=204)
    opencode.on("DELETE", path, status=204)
    (notes / "research" / "env").symlink_to("/proc/self/environ")
    response = api.post("/api/chats/ses_1/forms/frm_1", json={"answer": {}})
    assert response.status_code == 409
    assert api.delete("/api/chats/ses_1/forms/frm_1").status_code == 204
    assert opencode.bodies("POST", f"{path}/reply") == []


def test_reply_form(
    api: TestClient, opencode: Opencode, research_chat: dict[str, Any]
) -> None:
    path = f"{SESSION}/form/frm_1"
    opencode.on("POST", f"{path}/reply", status=204)
    opencode.on("DELETE", path, status=204)
    answer = {"q0": "A", "q1": ["x"], "q2": True, "q3": 1.5}
    response = api.post("/api/chats/ses_1/forms/frm_1", json={"answer": answer})
    assert response.status_code == 204
    assert opencode.bodies("POST", f"{path}/reply") == [{"answer": answer}]
    response = api.delete("/api/chats/ses_1/forms/frm_1", params={"message": "нет"})
    assert response.status_code == 204
    cancel = [r for r in opencode.seen if r.method == "DELETE"]
    assert dict(cancel[0].url.params) == {"message": "нет"}


# SSE


def test_events(
    api: TestClient,
    opencode: Opencode,
    research_chat: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat, "RETRY_DELAYS", (0.0,))
    form = {"id": "frm_1", "sessionID": "ses_1", "title": "Questions", "fields": []}
    delta = event("session.text.delta", {"sessionID": "ses_1", "delta": "при"})
    created = event("form.created", {"form": form})
    # Первое подключение: события своего и чужого чата, затем обрыв.
    opencode.sse(
        event("server.connected", {}),
        delta,
        event("session.text.delta", {"sessionID": "ses_2", "delta": "x"}),
        created,
    )
    # Второе и третье падают, четвёртое открывается, но чат к этому времени удалён.
    opencode.on("GET", "/api/event", {"message": "сбой"}, status=500)
    opencode.on("GET", "/api/event", {"message": "не спеши"}, status=429)
    opencode.sse(event("server.connected", {}))
    opencode.on("GET", f"{SESSION}/message", {"data": [{"id": "msg_1"}], "cursor": {}})
    opencode.on("GET", f"{SESSION}/message", {"message": "удалён"}, status=404)
    opencode.on("GET", f"{SESSION}/permission", [{"id": "per_1"}])
    opencode.on("GET", f"{SESSION}/form", [form])

    with api.stream("GET", "/api/chats/ses_1/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        received = frames(response.read().decode())

    assert received == [
        (
            "snapshot",
            {
                "messages": [{"id": "msg_1"}],
                "permissions": [{"id": "per_1"}],
                "forms": [form],
            },
        ),
        ("event", delta),
        ("event", created),
        ("reconnecting", {"detail": "Поток событий opencode закрылся", "delay": 0.0}),
        ("reconnecting", {"detail": "сбой", "delay": 0.0}),
        ("reconnecting", {"detail": "не спеши", "delay": 0.0}),
        ("error", {"detail": "удалён", "status": 404, "tag": None}),
    ]


def test_events_unknown_chat(api: TestClient, opencode: Opencode) -> None:
    opencode.on("GET", SESSION, {"_tag": "SessionNotFoundError"}, status=404)
    response = api.get("/api/chats/ses_1/events")
    assert response.status_code == 404
    assert all(r.url.path != "/api/event" for r in opencode.seen)


@pytest.mark.anyio
async def test_snapshot_prefers_fatal_error(opencode: Opencode) -> None:
    """Из ошибок параллельного снимка берётся фатальная: поток заканчивается."""
    opencode.on("GET", f"{SESSION}/message", {"message": "сбой"}, status=500)
    opencode.on("GET", f"{SESSION}/permission", {"message": "удалён"}, status=404)
    opencode.on("GET", f"{SESSION}/form", [])
    transport = httpx2.MockTransport(opencode.handle)
    async with OpencodeClient("http://opencode", "x", transport=transport) as client:
        with pytest.raises(OpencodeError) as info:
            await chat.snapshot(client, "ses_1")
    assert info.value.status == 404
