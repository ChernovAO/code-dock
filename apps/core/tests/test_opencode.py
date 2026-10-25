import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from core.opencode import (
    EVENT_READ_TIMEOUT,
    PAGE_SIZE,
    Event,
    OpencodeClient,
    OpencodeError,
    Rule,
)

type Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def client(handler: Handler) -> OpencodeClient:
    return OpencodeClient(
        "http://opencode:4096", "secret", transport=httpx2.MockTransport(handler)
    )


def body(request: httpx2.Request) -> Any:
    return json.loads(request.content)


@pytest.mark.anyio
async def test_basic_auth() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"data": {"id": "ses_1"}})

    async with client(handler) as api:
        assert await api.get_session("ses_1") == {"id": "ses_1"}
    request = seen[0]
    assert request.url == "http://opencode:4096/api/session/ses_1"
    expected = httpx2.BasicAuth("opencode", "secret")._auth_header
    assert request.headers["authorization"] == expected


@pytest.mark.anyio
async def test_create_session() -> None:
    seen: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(body(request))
        return httpx2.Response(200, json={"data": {"id": "ses_1"}})

    async with client(handler) as api:
        await api.create_session("/repos/notes/research")
        await api.create_session(
            "/repos/notes",
            title="t",
            agent="plan",
            model={"providerID": "anthropic", "id": "m"},
            permissions=[Rule(action="question", resource="*", effect="deny")],
        )
    assert seen == [
        {"location": {"directory": "/repos/notes/research"}},
        {
            "location": {"directory": "/repos/notes"},
            "title": "t",
            "agent": "plan",
            "model": {"providerID": "anthropic", "id": "m"},
            "permissions": [{"action": "question", "resource": "*", "effect": "deny"}],
        },
    ]


@pytest.mark.anyio
async def test_list_sessions_follows_cursor() -> None:
    seen: list[httpx2.Request] = []
    pages = [
        {"data": [{"id": "ses_2"}], "cursor": {"previous": "p", "next": "c1"}},
        {"data": [{"id": "ses_1"}], "cursor": {"previous": "p", "next": "c2"}},
        {"data": [], "cursor": {"previous": "p", "next": "c3"}},
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=pages[len(seen) - 1])

    async with client(handler) as api:
        found = await api.list_sessions(directory="/repos/notes", subpath="research")
    assert [s["id"] for s in found] == ["ses_2", "ses_1"]
    first, second, _ = (dict(r.url.params) for r in seen)
    assert first == {
        "directory": "/repos/notes",
        "subpath": "research",
        "order": "desc",
        "limit": str(PAGE_SIZE),
    }
    assert second == {"cursor": "c1", "limit": str(PAGE_SIZE)}


@pytest.mark.anyio
async def test_pages_repeated_cursor() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        page = {"data": [{"id": "msg_1"}], "cursor": {"next": "same"}}
        return httpx2.Response(200, json=page)

    async with client(handler) as api:
        with pytest.raises(OpencodeError, match="повторился") as info:
            await api.messages("ses_1")
    assert info.value.status == 0
    assert len(seen) == 2


@pytest.mark.anyio
async def test_messages_stop_without_cursor() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        page = {"data": [{"id": "msg_1"}], "cursor": {"previous": None, "next": None}}
        return httpx2.Response(200, json=page)

    async with client(handler) as api:
        assert await api.messages("ses_1") == [{"id": "msg_1"}]
    assert len(seen) == 1
    assert seen[0].url.path == "/api/session/ses_1/message"
    assert seen[0].url.params["order"] == "asc"


@pytest.mark.anyio
async def test_prompt_interrupt_and_permissions() -> None:
    seen: list[tuple[str, str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = body(request) if request.content else None
        seen.append((request.method, request.url.path, payload))
        path = request.url.path
        if path.endswith("/prompt"):
            return httpx2.Response(200, json={"data": {"id": "msg_1", "type": "user"}})
        if path.endswith("/interrupt"):
            return httpx2.Response(200, json={"interrupted": False})
        if path.endswith("/reply"):
            return httpx2.Response(204)
        return httpx2.Response(200, json={"data": [{"id": "per_1"}]})

    async with client(handler) as api:
        assert (await api.prompt("ses_1", "привет"))["id"] == "msg_1"
        assert await api.interrupt("ses_1") is False
        assert await api.permissions("ses_1") == [{"id": "per_1"}]
        await api.reply_permission("ses_1", "per_1", "once")
        await api.reply_permission("ses_1", "per_1", "reject", "нельзя")
    assert seen == [
        ("POST", "/api/session/ses_1/prompt", {"text": "привет"}),
        ("POST", "/api/session/ses_1/interrupt", None),
        ("GET", "/api/session/ses_1/permission", None),
        ("POST", "/api/session/ses_1/permission/per_1/reply", {"decision": "once"}),
        (
            "POST",
            "/api/session/ses_1/permission/per_1/reply",
            {"decision": "reject", "message": "нельзя"},
        ),
    ]


@pytest.mark.anyio
async def test_forms() -> None:
    seen: list[tuple[str, str, Any, dict[str, str]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        payload = body(request) if request.content else None
        seen.append(
            (request.method, request.url.path, payload, dict(request.url.params))
        )
        if request.method == "GET":
            return httpx2.Response(200, json={"data": [{"id": "frm_1"}]})
        return httpx2.Response(204)

    async with client(handler) as api:
        assert await api.forms("ses_1") == [{"id": "frm_1"}]
        await api.reply_form("ses_1", "frm_1", {"q0": "A", "q1": ["x", "y"]})
        await api.cancel_form("ses_1", "frm_1")
        await api.cancel_form("ses_1", "frm_1", "не сейчас")
        with pytest.raises(ValueError, match="ID"):
            await api.reply_form("ses_1", "../frm", {})
    form = "/api/session/ses_1/form/frm_1"
    assert seen == [
        ("GET", "/api/session/ses_1/form", None, {}),
        ("POST", f"{form}/reply", {"answer": {"q0": "A", "q1": ["x", "y"]}}, {}),
        ("DELETE", form, None, {}),
        ("DELETE", form, None, {"message": "не сейчас"}),
    ]


@pytest.mark.anyio
async def test_error_with_tag() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        error = {
            "_tag": "SessionNotFoundError",
            "sessionID": "ses_x",
            "message": "Session not found: ses_x",
        }
        return httpx2.Response(404, json=error)

    async with client(handler) as api:
        with pytest.raises(OpencodeError) as info:
            await api.get_session("ses_x")
    assert info.value.status == 404
    assert info.value.tag == "SessionNotFoundError"
    assert info.value.message == "Session not found: ses_x"


@pytest.mark.anyio
async def test_error_fields_not_strings() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, json={"_tag": 1, "message": {"text": "x"}})

    async with client(handler) as api:
        with pytest.raises(OpencodeError) as info:
            await api.get_session("ses_1")
    assert info.value.tag is None
    assert info.value.message == '{"_tag":1,"message":{"text":"x"}}'


@pytest.mark.anyio
async def test_error_without_json() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, text="Unauthorized")

    async with client(handler) as api:
        with pytest.raises(OpencodeError) as info:
            await api.get_session("ses_1")
    assert (info.value.status, info.value.tag, info.value.message) == (
        401,
        None,
        "Unauthorized",
    )


@pytest.mark.anyio
async def test_unreachable() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    async with client(handler) as api:
        with pytest.raises(OpencodeError) as info:
            await api.get_session("ses_1")
    assert info.value.status == 0


@pytest.mark.parametrize(
    "bad", ["", "..", "x/../../fs/write", "../permission/saved", "ses_1?x=1", "a%2Fb"]
)
@pytest.mark.anyio
async def test_rejects_bad_ids(bad: str) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError(f"Запрос ушёл: {request.url}")

    async with client(handler) as api:
        with pytest.raises(ValueError, match="Недопустимый ID"):
            await api.get_session(bad)
        with pytest.raises(ValueError, match="Недопустимый ID"):
            await api.prompt(bad, "text")
        with pytest.raises(ValueError, match="Недопустимый ID"):
            await api.reply_permission("ses_1", bad, "once")


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (httpx2.Response(200, text="<html>"), "не JSON"),
        (httpx2.Response(200, json=[1]), "не JSON-объект"),
        (httpx2.Response(200, json={}), "Нет объекта `data`"),
        (httpx2.Response(204), "Нет объекта `data`"),
    ],
)
@pytest.mark.anyio
async def test_malformed_object(response: httpx2.Response, match: str) -> None:
    async with client(lambda request: response) as api:
        with pytest.raises(OpencodeError, match=match) as info:
            await api.get_session("ses_1")
    assert info.value.status == 0


@pytest.mark.anyio
async def test_malformed_permissions() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"data": {"id": "per_1"}})

    async with client(handler) as api:
        with pytest.raises(OpencodeError, match="Нет списка `data`"):
            await api.permissions("ses_1")


SSE = (
    'data: {"id":"evt_0","type":"server.connected","data":{}}\n\n'
    ": heartbeat\n\n"
    'data: {"id":"evt_1","created":1,"type":"session.text.delta",'
    '"location":{"directory":"/repos/notes"},"data":{"sessionID":"ses_1","delta":"при"}}\n\n'
    'data: {"id":"evt_2","created":2,"type":"session.text.ended",'
    '"data":{"sessionID":"ses_1","text":"привет"},'
    '"durable":{"aggregateID":"ses_1","seq":4,"version":1}}\n\n'
)


def sse(text: str, status: int = 200) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/event"
        assert request.extensions["timeout"]["read"] == EVENT_READ_TIMEOUT
        return httpx2.Response(
            status, headers={"content-type": "text/event-stream"}, text=text
        )

    return handler


@pytest.mark.anyio
async def test_events() -> None:
    async with client(sse(SSE)) as api:
        events = [event async for event in api.events()]
    assert [e.type for e in events] == [
        "server.connected",
        "session.text.delta",
        "session.text.ended",
    ]
    connected, delta, ended = events
    assert connected.session_id is None
    assert delta.session_id == "ses_1"
    assert delta.durable is None
    assert ended.durable is not None and ended.durable.seq == 4


def test_event_session_id_must_be_string() -> None:
    event = Event(id="evt_1", type="x", data={"sessionID": 1})
    assert event.session_id is None
    event = Event(id="evt_1", type="x", data={"form": "ses_1"})
    assert event.session_id is None


def test_form_event_session_id() -> None:
    # Так `form.created` приходит в 2.1.2 (проверено).
    form = {"id": "frm_1", "sessionID": "ses_1", "title": "Questions", "fields": []}
    event = Event(id="evt_1", type="form.created", data={"form": form})
    assert event.session_id == "ses_1"


@pytest.mark.anyio
async def test_events_skip_bad_frames(caplog: pytest.LogCaptureFixture) -> None:
    frames = 'data: {"type":1}\n\ndata:\n\ndata: {"id":"evt_1","type":"x"}\n\n'
    async with client(sse(frames)) as api:
        events = [event async for event in api.events()]
    assert [e.id for e in events] == ["evt_1"]
    skipped = [r for r in caplog.records if "Пропущен" in r.getMessage()]
    assert len(skipped) == 2


@pytest.mark.anyio
async def test_events_unauthorized() -> None:
    async with client(sse('{"_tag":"UnauthorizedError","message":"no"}', 401)) as api:
        with pytest.raises(OpencodeError) as info:
            [event async for event in api.events()]
    assert (info.value.status, info.value.tag) == (401, "UnauthorizedError")


@pytest.mark.anyio
async def test_events_broken() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadError("reset", request=request)

    async with client(handler) as api:
        with pytest.raises(OpencodeError, match="оборвался") as info:
            [event async for event in api.events()]
    assert info.value.status == 0
