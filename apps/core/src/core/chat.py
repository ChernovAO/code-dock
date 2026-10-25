"""SSE чата: общий поток opencode, отфильтрованный по чату, со снимком истории.

opencode не хранит события и не досылает их после обрыва. Поэтому порядок такой:
подписаться на `/api/event`, дождаться первого кадра (подписка открыта), прочитать
снимок (история, запросы разрешений, формы) и дальше пересылать события чата.
События, которые пришли, пока читался снимок, идут после него: клиент сливает
их со снимком по ключам, повторы безвредны. После обрыва всё повторяется,
клиент получает новый снимок и заменяет им своё состояние. Буфера событий нет.

Ключи слияния (проверено на 2.1.2): сообщения — `id`; текст — `assistantMessageID`
и `ordinal`; вызов инструмента — `assistantMessageID` и `id`; запрос разрешения —
`id`, закрывает его `permission.replied` с `requestID`; форма — `id`, закрывают
её `form.replied` и `form.cancelled`.
"""

import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing

import anyio
from fastapi.sse import ServerSentEvent

from core.opencode import Json, OpencodeClient, OpencodeError

log = logging.getLogger(__name__)

# Паузы перед переподпиской, секунды. Последняя повторяется, пока opencode лежит.
RETRY_DELAYS: Sequence[float] = (1.0, 2.0, 5.0, 10.0, 30.0)
# Ошибки, которые переподписка не исправит: пароль неверен, чат удалён.
FATAL_STATUSES = {401, 403, 404}


async def snapshot(opencode: OpencodeClient, chat_id: str) -> Json:
    """Состояние чата целиком. Запросы разрешений эфемерны, в истории их нет.

    Три запроса идут параллельно: пока читается снимок, события копятся.
    """
    reads = {
        "messages": opencode.messages,
        "permissions": opencode.permissions,
        "forms": opencode.forms,
    }
    found: dict[str, list[Json]] = {}
    errors: list[OpencodeError] = []

    async def read(key: str) -> None:
        # Ошибка не выходит из группы задач: иначе придёт `ExceptionGroup`.
        try:
            found[key] = await reads[key](chat_id)
        except OpencodeError as e:
            errors.append(e)

    async with anyio.create_task_group() as group:
        for key in reads:
            group.start_soon(read, key)
    if errors:
        # Переподписку решает одна ошибка, фатальная важнее.
        raise min(errors, key=lambda e: e.status not in FATAL_STATUSES)
    return {key: found[key] for key in reads}


async def stream(
    opencode: OpencodeClient, chat_id: str
) -> AsyncIterator[ServerSentEvent]:
    """События SSE для клиента: `snapshot`, `event`, `reconnecting`, `error`.

    Бесконечен, пока клиент не отключится. Заканчивается только на ошибке,
    которую переподписка не исправит (`error`, `FATAL_STATUSES`): чат удалён,
    пароль неверен. Остальные коды, в том числе 408 и 429, ведут к переподписке.
    """
    failures = 0
    while True:
        try:
            synced = False
            # Без `aclosing` соединение с opencode живёт до сборки мусора,
            # когда клиент отключается посреди потока.
            async with aclosing(opencode.events()) as events:
                async for event in events:
                    if not synced:
                        data = await snapshot(opencode, chat_id)
                        yield ServerSentEvent(event="snapshot", data=data)
                        synced = True
                        failures = 0
                    if event.session_id == chat_id:
                        data = event.model_dump(mode="json", exclude_none=True)
                        yield ServerSentEvent(event="event", data=data)
            reason = "Поток событий opencode закрылся"
        except OpencodeError as e:
            if e.status in FATAL_STATUSES:
                yield ServerSentEvent(
                    event="error",
                    data={"detail": e.message, "status": e.status, "tag": e.tag},
                )
                return
            reason = e.message
        delay = RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)]
        failures += 1
        log.info(
            "Переподписка на события чата %s через %s с: %s", chat_id, delay, reason
        )
        yield ServerSentEvent(
            event="reconnecting", data={"detail": reason, "delay": delay}
        )
        await anyio.sleep(delay)
