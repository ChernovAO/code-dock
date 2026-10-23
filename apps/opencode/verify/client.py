"""Клиент API opencode для проверок: чаты с мок-моделью и разбор исхода вызова инструмента."""

import base64
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

BLOCKED = "Blocked by configuration policy"


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)

    def text(self) -> str:
        return self.body.decode(errors="replace")


@dataclass(frozen=True)
class ToolCall:
    name: str
    input: dict[str, Any]
    status: str
    text: str


@dataclass(frozen=True)
class Ask:
    id: str
    action: str
    resources: tuple[str, ...]
    save: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    session_id: str
    calls: tuple[ToolCall, ...]
    asked: tuple[Ask, ...]
    idle: bool

    @property
    def kind(self) -> str:
        """ok — вызов выполнен, blocked — запрещён политикой, ask — ждёт ответа,
        nocall — инструмента у модели нет, error — любая другая ошибка."""
        if not self.calls and not self.asked:
            return "nocall"
        last = self.calls[-1] if self.calls else None
        if last and last.status == "completed":
            return "ok"
        if last and last.status == "error":
            return "blocked" if BLOCKED in last.text else "error"
        return "ask" if self.asked else "error"

    @property
    def text(self) -> str:
        return self.calls[-1].text if self.calls else ""

    def summary(self) -> str:
        if self.kind == "ask":
            ask = self.asked[0]
            return f"{ask.action} {list(ask.resources)} save={list(ask.save)}"
        return " ".join(self.text.split())[:110]


def location(directory: str) -> dict[str, str]:
    return {"location[directory]": directory}


def call(tool: str, **args: Any) -> str:
    present = {key: value for key, value in args.items() if value is not None}
    return f"CALL {tool} {json.dumps(present, ensure_ascii=False)}"


def read(path: str) -> str:
    return call("read", path=path)


def write(path: str, content: str = "x\n") -> str:
    return call("write", path=path, content=content)


def edit(path: str, old: str, new: str) -> str:
    return call("edit", path=path, oldString=old, newString=new)


def shell(command: str, workdir: str | None = None) -> str:
    return call("shell", command=command, workdir=workdir)


def grep(pattern: str, path: str | None = None) -> str:
    return call("grep", pattern=pattern, path=path)


def glob(pattern: str, path: str | None = None) -> str:
    return call("glob", pattern=pattern, path=path)


class Api:
    def __init__(self, base_url: str, password: str, user: str = "opencode") -> None:
        self.base_url = base_url
        self._token = base64.b64encode(f"{user}:{password}".encode()).decode()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, str] | None = None,
        auth: bool = True,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> Response:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["content-type"] = "application/json"
        if auth:
            headers["authorization"] = "Basic " + (token or self._token)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, resp.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, e.read())

    def data(self, method: str, path: str, **kwargs: Any) -> Any:
        """Запрос, который обязан удаться. Возвращает поле `data` ответа."""
        resp = self.request(method, path, **kwargs)
        if resp.status >= 300:
            raise RuntimeError(f"{method} {path} -> {resp.status}: {resp.text()[:300]}")
        if not resp.body:
            return None
        payload = resp.json()
        return (
            payload["data"]
            if isinstance(payload, dict) and "data" in payload
            else payload
        )

    def create_session(
        self,
        directory: str,
        *,
        permissions: Sequence[dict[str, str]] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "location": {"directory": directory},
            "model": {"providerID": "mock", "id": "m1"},
        }
        if permissions is not None:
            body["permissions"] = list(permissions)
        if agent is not None:
            body["agent"] = agent
        session: dict[str, Any] = self.data("POST", "/api/session", body=body)
        return session

    def prompt(self, session_id: str, text: str) -> None:
        self.data("POST", f"/api/session/{session_id}/prompt", body={"text": text})

    def messages(
        self, session_id: str, kind: str | None = None
    ) -> list[dict[str, Any]]:
        params = {"order": "asc", "limit": "200"}
        if kind:
            params["type"] = kind
        found: list[dict[str, Any]] = self.data(
            "GET", f"/api/session/{session_id}/message", params=params
        )
        return found

    def idle_count(self, session_id: str) -> int:
        return len(self.messages(session_id, "idle"))

    def pending(self, session_id: str) -> list[Ask]:
        found = self.data("GET", f"/api/session/{session_id}/permission")
        return [
            Ask(p["id"], p["action"], tuple(p["resources"]), tuple(p.get("save") or ()))
            for p in found
        ]

    def reply(
        self,
        session_id: str,
        request_id: str,
        decision: str,
        message: str | None = None,
    ) -> None:
        body: dict[str, Any] = {"decision": decision}
        if message:
            body["message"] = message
        path = f"/api/session/{session_id}/permission/{request_id}/reply"
        self.data("POST", path, body=body)

    def interrupt(self, session_id: str) -> None:
        self.request("POST", f"/api/session/{session_id}/interrupt")

    def tool_calls(self, session_id: str) -> tuple[ToolCall, ...]:
        calls = []
        for message in self.messages(session_id, "assistant"):
            for part in message.get("content", []):
                if part.get("type") != "tool":
                    continue
                state = part["state"]
                if state["status"] == "error":
                    text = state["error"].get("message", "")
                elif state["status"] == "completed":
                    contents = state.get("content", [])
                    text = " ".join(c.get("text", c.get("uri", "")) for c in contents)
                else:
                    text = ""
                calls.append(
                    ToolCall(
                        part["name"], state.get("input") or {}, state["status"], text
                    )
                )
        return tuple(calls)

    def run(
        self,
        directory: str,
        calls: Sequence[str],
        *,
        replies: Sequence[str | tuple[str, str]] = (),
        agent: str | None = None,
        permissions: Sequence[dict[str, str]] | None = None,
        session: dict[str, Any] | None = None,
        timeout: float = 45.0,
    ) -> Outcome:
        """Отправляет промпт с вызовами инструментов и ждёт конца хода.

        Запросы разрешений получают ответы из `replies` по порядку. Если ответов нет,
        ожидание заканчивается, как только запрос появился.
        """
        session = session or self.create_session(
            directory, permissions=permissions, agent=agent
        )
        sid = str(session["id"])
        before = self.idle_count(sid)
        self.prompt(sid, "\n".join(calls))
        remaining = list(replies)
        asked: list[Ask] = []
        seen: set[str] = set()
        idle = False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.25)
            for ask in self.pending(sid):
                if ask.id in seen:
                    continue
                seen.add(ask.id)
                asked.append(ask)
                if remaining:
                    reply = remaining.pop(0)
                    decision, message = (
                        (reply, None) if isinstance(reply, str) else reply
                    )
                    self.reply(sid, ask.id, decision, message)
            if self.idle_count(sid) > before:
                idle = True
                break
            if asked and not remaining and self.pending(sid):
                time.sleep(0.4)
                break
        outcome = Outcome(sid, self.tool_calls(sid), tuple(asked), idle)
        if not idle:
            self.interrupt(sid)
        return outcome

    def settle(self, session_id: str, seconds: float = 3.0) -> None:
        """Ждёт, пока чат закончит ход (или истекут секунды)."""
        before = self.idle_count(session_id)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and self.idle_count(session_id) <= before:
            time.sleep(0.25)


class EventTap:
    """Читает общий поток `/api/event` в фоне, пока идёт сценарий."""

    def __init__(self, api: Api) -> None:
        self._buffer = bytearray()
        self._lock = threading.Lock()
        headers = {"authorization": "Basic " + api._token}
        req = urllib.request.Request(api.base_url + "/api/event", headers=headers)
        self._response = urllib.request.urlopen(req, timeout=30)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        response = self._response
        try:
            while chunk := response.read1(65536):
                with self._lock:
                    self._buffer.extend(chunk)
        except OSError, ValueError, AttributeError:
            pass

    def text(self) -> str:
        with self._lock:
            return bytes(self._buffer).decode(errors="replace")

    def close(self) -> None:
        self._response.close()
        self._thread.join(timeout=2)


def parse_frames(text: str) -> tuple[list[dict[str, Any]], list[str]]:
    """JSON из строк `data:` и все остальные непустые строки (id:, event:, комментарии)."""
    events: list[dict[str, Any]] = []
    other: list[str] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
        elif line.strip():
            other.append(line)
    return events, other
