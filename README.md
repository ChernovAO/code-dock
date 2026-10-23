# Code Dock
Этот проект — моя попытка сделать удобный для меня инструмент для работы с ИИ-агентом с телефона. Обёртка над связкой из opencode и git.

## Идея
Я хочу иметь возможность работать с ИИ со своего телефона. Для этого мне нужен Web UI, похожий на Open WebUI, но с расширенными возможностями в плане сохранения файлов и ведения проектов. Также я хочу вести версионирование через git и синхронизироваться с GitHub. Сейчас вся работа с ИИ у меня ведётся или через бесплатные веб-чаты, или через opencode на ноутбуке. Поэтому моё решение будет обёрткой над opencode и git.

## Документация
- [Обзор проекта: зачем он нужен](docs/overview.md)
- [Функциональные требования](docs/functional-requirements.md)
- [Интеграция с opencode](docs/opencode-integration.md)
- [План обсуждения: открытые вопросы](docs/discussion-plan.md)
- [План прототипа](docs/prototype-plan.md)

## Архитектура
- Фронтенд — React + TypeScript, [`apps/frontend`](apps/frontend).
- Бэкенд — FastAPI (Python), [`apps/core`](apps/core).
- Агент — opencode в отдельном контейнере, [`apps/opencode`](apps/opencode).

Запуск через docker compose.

## Запуск
Нужны Docker и Docker Compose.

```
cp .env.example .env      # задайте OPENCODE_SERVER_PASSWORD и HOST_REPOS_DIR; HOST_UID и HOST_GID — вывод `id -u` и `id -g`
mkdir -p data/repos       # создайте папку из HOST_REPOS_DIR; здесь пример
docker compose up --build
```

Приложение на http://localhost:8080, пока это страница-заглушка со статусом бэкенда. Репозитории клонируйте в папку из `HOST_REPOS_DIR` (в примере — `data/repos`), клонирования из интерфейса пока нет.

Чтобы агент отвечал, подключите провайдера модели:
```
docker compose exec opencode opencode auth login anthropic --method key --server http://127.0.0.1:4096
```
Вместо `anthropic` укажите ID своего провайдера, например `openai` или `openrouter`.
Ключ хранится в томе `opencode-data`. Команда `docker compose down -v` удалит том вместе с чатами и ключом.

## Структура репозитория
- `apps/core` — бэкенд, участник uv workspace.
- `apps/opencode` — образ opencode: версия, пользователь, системный конфиг с правами. В `verify/` — стенд проверки образа и прав.
- `apps/frontend` — фронтенд.
- `docs/` — документация.
- `.env.example` — шаблон настроек запуска.
- `data/` — папка репозиториев из шаблона (`HOST_REPOS_DIR`), не коммитится.

## Разработка
Нужны [uv](https://docs.astral.sh/uv/) и Python 3.14.

```
uv sync
```

Проверка образа opencode и прав агента без docker: [`apps/opencode/verify`](apps/opencode/verify/README.md). Нужны Linux, `bwrap` и нативный opencode той же версии, что в образе.
```
python3 apps/opencode/verify/run.py
```
