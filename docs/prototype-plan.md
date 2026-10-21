# План прототипа

Рабочий план первого прототипа. Контекст: [overview.md](overview.md), [discussion-plan.md](discussion-plan.md) (раздел 8), [opencode-integration.md](opencode-integration.md). Составлен 2026-10-21 по opencode V2 версии 2.1.2.

Решения из этого плана после реализации переносятся в [overview.md](overview.md) и [functional-requirements.md](functional-requirements.md). Потом этот файл можно удалить.

## Принятые решения

| № | Решение | Следствие |
|---|---|---|
| 1 | Свой файловый слой | FastAPI работает с файлами напрямую на общем томе. `fs/*` opencode не используем и не проксируем. |
| 2 | Ручной клиент | `httpx2` переезжает из dev-зависимостей `core` в основные. Свои модели для ~25 событий. |
| 3 | Отдельный контейнер | `apps/opencode`, образ на базе `ghcr.io/anomalyco/opencode:2.1.2`. |
| 4 | Докачка событий | Меняет правило в `AGENTS.md` («полностью перечитать историю»). Буфера в FastAPI по-прежнему нет: журнал хранит opencode, клиент помнит последний `seq`. |
| 5 | Граница доступа агента — репозиторий | Политика `external_directory:*` deny. Соседние проекты одного репозитория агенту доступны. |
| 6 | Репозитории на хосте | Папка хоста монтируется в `core` и `opencode`. Клоны переживают рестарт контейнеров. |

### Оговорка по докачке

В `session.message.list` нет `seq`. Он есть только в параметре `after` у `GET /api/experimental/session/{id}/log`. Схема:
- Первое открытие чата: открыть поток, прочитать снимок истории, идемпотентно слить. Большинство долговечных событий несут полное состояние (`text.ended`, `tool.success`, `message.content.updated`), поэтому повторы безвредны.
- Переподключение: `session.log?after=<seq>`. Фронтенд получает `id: <seq>` в SSE, браузер сам шлёт `Last-Event-ID`.
- Дельты текста, рассуждений и ввода tool эфемерны и не попадают в журнал. Недостающее досылает `text.ended`.
- Если проверка выявит проблему, запасной путь — полное перечитывание через `session.message.list`.

## Конфигурация: репозитории на хосте

Папка `HOST_REPOS_DIR` монтируется в `core` и `opencode` по одному пути `/repos`. Путь должен совпадать, потому что `location.directory` в opencode абсолютный.

`.env.example`:
```
HOST_UID=1000            # id -u
HOST_GID=1000            # id -g
HOST_REPOS_DIR=./data/repos
OPENCODE_SERVER_PASSWORD=
```

`compose.yaml` (фронтенд не меняется):
```yaml
services:
  opencode:
    build:
      context: apps/opencode
      args: { HOST_UID: "${HOST_UID:-1000}", HOST_GID: "${HOST_GID:-1000}" }
    restart: unless-stopped
    command: ["serve", "--hostname", "0.0.0.0", "--port", "4096"]
    environment:
      OPENCODE_SERVER_PASSWORD: "${OPENCODE_SERVER_PASSWORD:?задайте пароль в .env}"
    volumes:
      - "${HOST_REPOS_DIR:-./data/repos}:/repos"
      - opencode-data:/opencode   # БД, снапшоты, учётные данные
  core:
    # build как сейчас + те же args
    environment:
      OPENCODE_URL: http://opencode:4096
      OPENCODE_SERVER_PASSWORD: "${OPENCODE_SERVER_PASSWORD:?...}"
      REPOS_DIR: /repos
    volumes:
      - "${HOST_REPOS_DIR:-./data/repos}:/repos"
    depends_on: [opencode]
volumes:
  opencode-data:
```

`apps/opencode/Dockerfile`:
```dockerfile
FROM ghcr.io/anomalyco/opencode:2.1.2
ARG HOST_UID=1000
ARG HOST_GID=1000
RUN apk add --no-cache git bash \
 && addgroup -g ${HOST_GID} app \
 && adduser -D -u ${HOST_UID} -G app -h /opencode app
COPY opencode.jsonc /etc/opencode/opencode.jsonc
USER app
ENV HOME=/opencode
```

Официальный образ: Alpine, `ENTRYPOINT ["opencode"]`, без CMD и порта, работает от root, git в нём нет.

Пользователь `app` создаётся с UID и GID хоста, чтобы файлы в `/repos` принадлежали владельцу репозиториев. Тот же приём применяется в `apps/core/Dockerfile` вместо `useradd --system`. Запись в `passwd` нужна и git.

`apps/opencode/opencode.jsonc` — системный конфиг (`/etc/opencode`). Он перекрывает любой `opencode.json` из репозитория, а политики перекрывают всё, кроме Console и MDM. Черновик, проверить вживую:
```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "update": "disable",
  "shell": "/bin/bash",
  "permissions": [
    { "action": "*", "resource": "*", "effect": "ask" },
    { "action": "read", "resource": "*", "effect": "allow" },
    { "action": "glob", "resource": "*", "effect": "allow" },
    { "action": "grep", "resource": "*", "effect": "allow" },
    { "action": "edit", "resource": "*", "effect": "allow" },
    { "action": "read", "resource": "*.env", "effect": "ask" }
  ],
  "policies": [
    { "action": "tool.use", "resource": "external_directory:*", "effect": "deny" },
    { "action": "tool.use", "resource": "edit:*/.git/*", "effect": "deny" },
    { "action": "integration.use", "resource": "plugin:*", "effect": "deny" },
    { "action": "integration.use", "resource": "mcp:*", "effect": "deny" }
  ]
}
```

Первая политика реализует решение 5. Остальные три — дополнение: закрывают правку `.git` и подключение плагинов и MCP из `.opencode/` репозитория. Нужны ли они, подтвердить.

Прочие файлы:
- `.gitignore` и `.dockerignore`: добавить `data/`. Контекст сборки `core` — корень репозитория, без этого в сборку уйдут все клоны.
- `README.md` и `AGENTS.md`: первый запуск — `cp .env.example .env`, пароль, `mkdir -p data/repos`, `docker compose up --build`.

## Что выяснено про opencode V2 2.1.2

Проверено вживую:
- `fs/read` защищён: абсолютные пути и symlink наружу дают 404.
- `fs/write` и `fs/list` не ограничены: принимают `../`, абсолютные пути и symlink. Через `fs/write` перезаписан файл вне репозитория.
- Сервер требует Basic-пароль (пользователь `opencode`). Если пароль не задан, генерируется при старте.
- Живая спецификация совпадает с опубликованной: 144 операции.

По спецификации и документации (не проверено):
- Базовая политика прав — `* * allow`. Без конфига `shell` выполняется без вопросов, `external_directory` тоже разрешён.
- `reject` отклоняет все ожидающие запросы сессии сразу. Автоотклонения по таймеру в opencode нет.
- `always` сохраняется на уровне репозитория, то есть действует на все проекты в нём.
- Агент в папке проекта без вопросов видит соседние проекты того же репозитория.
- Снапшоты лежат в отдельной git-базе в каталоге данных opencode и не трогают коммиты и индекс. Откат совместим с ручными коммитами, но перезаписывает текущие правки файлов, которые трогал агент. Откат запрещён, пока чат выполняется.
- В OpenAPI полезная нагрузка SSE — непрозрачная JSON-строка. Типы событий берутся из `@opencode/client` (около 130).
- События бывают долговечные (с `durable.seq`) и эфемерные (дельты, `session.status`, `session.idle`, `session.tool.progress`).
- `/api/event` без параметров и, судя по документации клиента, общий на весь сервер. События несут `location`.
- `session.list` имеет параметры `search`, `directory`, `project`, `subpath`.
- В бинарнике есть `OPENCODE_SERVER_PASSWORD` и `OPENCODE_DISABLE_PROJECT_CONFIG`.

## Ограничения и риски

- **Общий том.** Через `shell` (после одобрения) агент достанет до всех репозиториев в `HOST_REPOS_DIR`. Для read, edit, glob и grep границу держит политика, для `shell` она «best effort». Использовать выделенную папку, а не `~/code`. Жёсткая изоляция потребовала бы монтировать один репозиторий, в прототип это не входит.
- **GitHub PAT** живёт только в `core`. В `remote`-URL и `.git/config` его писать нельзя: он окажется на общем томе и будет виден агенту. Передавать на время команды через `GIT_ASKPASS` или `http.extraheader`.
- **Хуки.** Агент мог бы подложить `.git/hooks/*`, которые выполнит `core` при commit или push. Закрываем политикой `edit:*/.git/*` и вызовом git с `-c core.hooksPath=/dev/null`.
- **Сеть.** Не ставить `internal: true`: иначе opencode не достучится до провайдеров. Порт opencode наружу не публикуется.
- **Каталог `data/`.** Если его нет, Docker создаст его от root, и запись упадёт. Поэтому `mkdir -p` в README.
- **Данные чатов.** `docker compose down -v` удалит том `opencode-data` вместе с чатами. Репозитории на хосте он не затронет.
- **Docker из среды opencode недоступен.** Сборку и `compose up` проверяет пользователь. Конфиг opencode, политики и код проверяются нативным бинарником V2.

## Порядок работ

### Фаза A: документы и конфигурация
- [ ] Документы: `AGENTS.md` (правило про события, структура каталогов, переменные окружения, версия 2.1.2), `opencode-integration.md`, `functional-requirements.md`, `overview.md`, `discussion-plan.md`.
- [ ] `apps/opencode` (Dockerfile и конфиг), `compose.yaml`, `apps/core/Dockerfile`, `.env.example`, `.gitignore`, `.dockerignore`.
- [ ] Проверка нативным V2 с теми же переменными и конфигом: пароль, политики, формат ресурсов.

### Фаза B: прототип FastAPI
- [ ] Слой путей: нормализация, запрет выхода за пределы репозитория, `realpath` для symlink.
- [ ] Тонкий клиент opencode на `httpx2`.
- [ ] Эндпоинты: список репозиториев и проектов, создать чат (`location` и `permissions`), отправить сообщение, SSE чата с `id: <seq>` и докачкой, ответ на разрешение.
- [ ] Тесты. Порог покрытия 70% уже включён в `pytest`.

nginx уже готов под SSE (`proxy_buffering off`, таймаут 1 час).

## Живые проверки

- [ ] Формат SSE-кадра и поле `id` в `/api/event` и `session.log`. Поведение `after` и `follow`.
- [ ] `OPENCODE_SERVER_PASSWORD` фиксирует пароль.
- [ ] Формат ресурсов в политиках (`edit:` с абсолютным или относительным путём). Приоритет системного конфига над конфигом в репозитории.
- [ ] Цикл `permission.asked` → `reply` и каскад `reject`.
- [ ] `session.create` в подпапке репозитория: `projectID`, `subpath`, фильтры `session.list`, включая `search`. Отдельно — пустой репозиторий без коммитов.
- [ ] Откат: `revert/stage` → ручной коммит → `commit`.
- [ ] Запись в `/repos` под UID хоста и работа git в контейнерах без `safe.directory` (проверяет пользователь через docker).

## Принято по умолчанию, подтвердить

1. Папка на хосте `./data/repos` (внутри проекта, в `.gitignore`). Другой путь задаётся абсолютным значением `HOST_REPOS_DIR` в `.env`. `~` Compose не раскрывает.
2. Пароль opencode обязателен в `.env`. Без него `compose` не стартует. Это один лишний шаг перед первым запуском, зато нет пароля по умолчанию.
3. Данные opencode в именованном томе `opencode-data`. Если нужны на хосте и для бэкапа, вместо него `./data/opencode`.
4. Дополнительные политики на `.git`, плагины и MCP.
