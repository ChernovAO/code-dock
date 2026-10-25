# Code Dock Core

Бэкенд Code Dock на FastAPI. Единственная точка входа для фронтенда.

## Зачем нужен
- Проксирует запросы и SSE-события в opencode. Opencode остаётся во внутренней сети docker.
- Выполняет git-операции: clone, commit, push, pull, история коммитов.
- Выполняет файловые операции: создание, переименование, удаление. Все пути проверяются: нормализация, запрет выхода за пределы проекта, учёт symlink.
- Хранит настройки, включая GitHub Personal Access Token. Токен не логируется и не отдаётся на фронтенд.

## Чего здесь нет
- Своего ИИ-агента. Вся логика ИИ живёт в opencode.
- Автокоммитов и автопуша. Синхронизация только ручная.
- Авторизации. Способ защиты доступа пока не выбран.

## Структура
`src/core/` — код приложения:
- `main.py` — приложение FastAPI.
- `settings.py` — настройки из переменных окружения `REPOS_DIR`, `OPENCODE_URL`, `OPENCODE_SERVER_PASSWORD`.
- `paths.py` — слой путей. Любой путь снаружи проходит через него: без `..`, абсолютных путей и `.git`, `realpath` остаётся внутри репозитория.
- `opencode.py` — тонкий клиент API opencode на `httpx2`: чаты, сообщения, разрешения, поток событий.
`tests/` — тесты на pytest.

## Запуск
Из корня репозитория:
```
uv sync
.venv/bin/uvicorn core.main:app --reload
```
API на `http://localhost:8000`, проверка: `GET /api/health`. Весь стек запускается через `docker compose up --build` из корня репозитория, приложение на `http://localhost:8080`.

## Проверки
Из `apps/core`:
```
uv run ruff check .   # линтер
uv run ruff format .  # форматтер
uv run mypy           # типы (strict, конфиг в pyproject.toml)
uv run pytest         # тесты + покрытие
```
Один тест: `uv run pytest tests/test_health.py::test_health`.

Покрытие включено всегда: `--cov=core` в `addopts`, учитываются ветвления (`branch`), отчёт печатается после тестов с непокрытыми строками. Порог — 70% (`fail_under` в `[tool.coverage.report]`): если общее покрытие ниже, прогон падает, даже когда все тесты прошли. Конфиг — в `[tool.coverage.*]` того же `pyproject.toml`.

Обычный `uv sync` из корня dev-зависимости не ставит — `uv run` из `apps/core` доставит их сам.
