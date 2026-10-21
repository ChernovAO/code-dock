# Интеграция с opencode

Документ фиксирует, что умеет API opencode V2 и как оно ложится на требования Code Dock. Требования: [functional-requirements.md](functional-requirements.md). Контекст: [overview.md](overview.md).

Источники:
- Клиент: https://opencode.ai/v2/docs/build/client
- OpenAPI: https://opencode.ai/v2/openapi.json (проверено 2026-10-21)

Официальный клиент `@opencode/client` сгенерирован из той же спецификации, что и HTTP API. Возможности у них совпадают. Заголовок спецификации: «Experimental HttpApi surface», версия `0.0.1`.

## Способы подключения

1. **Бэкенд на FastAPI ходит в opencode по HTTP.** Python-клиент генерируется из `/openapi.json`. Git и файлы остаются в FastAPI. Совпадает с принципом «свой агент не пишем».
2. **Фронтенд ходит в opencode напрямую через `@opencode/client`.** Меньше кода, но на публичном VPS сложнее защитить доступ к opencode.
3. **Node-сервис между ними.** Использует `@opencode/client` и `Service.ensure()`. Добавляет третий рантайм.

**Предварительная рекомендация:** вариант 1, где FastAPI проксирует запросы и SSE-события. Тогда фронтенд общается только с FastAPI, а авторизация решается в одном месте. Opencode остаётся во внутренней сети docker.

## Файлы

| Что нужно | Метод API | Замечания |
|---|---|---|
| Дерево файлов | `GET /api/fs/list` | Параметры `location`, `path`. |
| Чтение и скачивание | `GET /api/fs/read/*` | Поток с ETag, Last-Modified, Range. Ограничен `location`. |
| Поиск файлов | `GET /api/fs/find` | |
| Запись, создание, загрузка | `POST /api/experimental/fs/write` | Тело: сырые байты (`application/octet-stream`). Создаёт родительские папки. |

Особенности записи:
- Метод experimental, совместимость не гарантируется.
- В отличие от чтения, запись **не ограничена `location`** и принимает абсолютные пути. Пути нужно проверять в FastAPI.
- Удаления и переименования файлов в API нет. Их делает FastAPI.

## Чаты и агент

| Что нужно | Метод API |
|---|---|
| Список, создание, переименование, удаление чатов | `session.list`, `session.create`, `session.update` (PATCH), `session.remove` |
| Отправка сообщения | `session.prompt`: поля `text`, `files`, `agents`, `skills`. Вложения через `files[].uri`, упоминание файла через `mention`. |
| История сообщений | `session.message.list`, `session.message.get` |
| Стриминг и tool calls | `GET /api/event` |
| Прерывание | `session.interrupt` |
| Подтверждение действий | `session.permission.reply` (`once`, `always`, `reject`), `permission.request.list` |
| Модель и режим | `model.list`, `agent.list`, `session.switchModel`, `session.switchAgent` |
| Работа в фоне | `session.active`, `session.wait`, `session.inbox.*` |
| Форк чата | `session.fork` |
| Сжатие контекста | `session.compact` |

Решения `always` сохраняются и управляются через `permission.saved.*`.

## Git и откат

Есть в API:
- `vcs.status`, `vcs.diff`, `vcs.branch.list`, `vcs.get`, `vcs.init`.
- `session.diff`: изменения по ходам сессии.
- Откат правок агента: `session.revert.stage` (параметры `messageID`, `files`), затем `session.revert.commit` или `session.revert.clear`.
- `worktree.create`, `list`, `remove`: возможная основа для веток.

**Нет в API** (остаётся на FastAPI): `commit`, `push`, `pull`, `clone`, история коммитов. Для них нужен `git` через `subprocess` или pygit2/GitPython.

## Дополнительные возможности

- `session.generate`, `experimental.generate.text`: генерация текста, например сообщения коммита.
- `experimental.session.export` и `import`: экспорт и импорт сессии. Возможная основа для сохранения истории чатов в репозиторий.
- `experimental.voice.session`: возможная основа для голосового ввода. Не проверено.
- `POST /api/pair` и `GET /auth/connect/{code}`: сопряжение по коду. Возможный способ авторизации телефона. Не проверено.
- `provider.*`, `integration.*`, `credential.*`: настройка провайдеров, включая OAuth.
- `pty.*`, `persistent-pty`: терминал. Для проекта не нужен.

Поиска по истории чатов в API нет. Нужен перебор `session.list` и `message.list` или свой индекс.

## Ограничения и риски

- **События без replay.** Подписки `/api/event` работают только вживую, без повторной доставки и автопереподключения. После обрыва нужно подписаться заново и догрузить историю запросом.
- **Нестабильность API.** Спецификация названа экспериментальной. Версию opencode нужно зафиксировать в образе.
- **Запись вне проекта.** См. замечание про `fs/write`.
- **Безопасность агента.** Агент выполняет команды и правит файлы. Права задаются через `permissions` в конфиге opencode.
- **Хранение сессий.** Сессии лежат в БД opencode вне репозитория. Нужен том docker compose и бэкап.

## Что проверить на практике

1. Поведение `fs/read` и `fs/write` на путях за пределами проекта и на symlink.
2. Формат событий в `/api/event`: типы для стриминга текста, tool calls и запросов разрешений.
3. Совместимость `session.revert` с ручными git-коммитами.
4. Работа `experimental.voice.session` для голосового ввода.
5. Механизм pairing как вариант авторизации.
6. Привязка проекта (папки в репозитории) к `location` и изоляция сессий разных проектов.
