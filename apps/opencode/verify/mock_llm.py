"""Мок OpenAI-совместимого провайдера: вместо модели вызывает инструменты по тексту промпта.

Промпт содержит строки `CALL <инструмент> <json-аргументы>`. На i-м шаге цикла агента
(i — число сообщений role=tool после пользовательского сообщения с `CALL`) мок отвечает
i-м вызовом, после последнего вызова — текстом `done`. Если инструмента нет в `tools`
запроса (так у title-агента или у режима plan без записи), мок отвечает текстом.
Сообщения-напоминания режимов (synthetic) тоже приходят с role=user и могут оказаться
после промпта, поэтому промпт ищется по `CALL`, а не по последнему user.
"""

import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class ModelRequest:
    tools: tuple[str, ...]
    markers: tuple[str, ...]
    call: tuple[str, str] | None


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _parse_calls(text: str) -> list[tuple[str, str]]:
    calls = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("CALL "):
            _, name, args = (line.split(" ", 2) + ["{}"])[:3]
            calls.append((name, args))
    return calls


def _chunk(delta: dict[str, Any], finish: str | None = None) -> bytes:
    body = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "m1",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(body)}\n\n".encode()


class MockLLM:
    """Запросы к модели копятся в `requests`. `markers` — строки, которые ищутся в промпте."""

    def __init__(self, markers: Sequence[str] = ()) -> None:
        self.requests: list[ModelRequest] = []
        self._markers = tuple(markers)
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def clear(self) -> None:
        with self._lock:
            self.requests.clear()

    def offered_tools(self) -> set[str]:
        """Инструменты из самого полного запроса: у служебных агентов их нет."""
        with self._lock:
            tools = [r.tools for r in self.requests]
        longest = max(tools, key=len, default=())
        return set(longest)

    def markers_seen(self) -> set[str]:
        with self._lock:
            return {m for r in self.requests for m in r.markers}

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                body = json.dumps({"data": [{"id": "m1", "object": "model"}]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length", 0))
                request = json.loads(self.rfile.read(length) or b"{}")
                messages = request.get("messages", [])
                tools = tuple(
                    t["function"]["name"]
                    for t in request.get("tools", [])
                    if t.get("type") == "function"
                )
                users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
                # Plan и другие режимы добавляют synthetic-напоминание отдельным сообщением
                # пользователя, и оно может оказаться после промпта. Ищем промпт с вызовами.
                last_user = max(
                    (i for i in users if "CALL " in _text(messages[i].get("content"))),
                    default=max(users, default=-1),
                )
                step = sum(
                    1 for m in messages[last_user + 1 :] if m.get("role") == "tool"
                )
                calls = (
                    _parse_calls(_text(messages[last_user].get("content")))
                    if last_user >= 0
                    else []
                )
                call = calls[step] if step < len(calls) else None
                if call and call[0] not in tools:
                    call = None
                blob = json.dumps(messages, ensure_ascii=False)
                markers = tuple(m for m in owner._markers if m in blob)
                with owner._lock:
                    owner.requests.append(ModelRequest(tools, markers, call))
                try:
                    self._reply(call, step)
                except BrokenPipeError, ConnectionResetError:
                    pass

            def _reply(self, call: tuple[str, str] | None, step: int) -> None:
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.end_headers()
                write = self.wfile.write
                write(_chunk({"role": "assistant", "content": ""}))
                if call:
                    function = {"name": call[0], "arguments": call[1]}
                    tool_call = {
                        "index": 0,
                        "id": f"call_{step}",
                        "type": "function",
                        "function": function,
                    }
                    write(_chunk({"tool_calls": [tool_call]}))
                    write(_chunk({}, finish="tool_calls"))
                else:
                    write(_chunk({"content": "done"}))
                    write(_chunk({}, finish="stop"))
                usage = {"prompt_tokens": 10, "completion_tokens": 5}
                final = {"id": "chatcmpl-mock", "choices": [], "usage": usage}
                write(f"data: {json.dumps(final)}\n\n".encode())
                write(b"data: [DONE]\n\n")

        return Handler
