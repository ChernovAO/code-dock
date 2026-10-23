"""Проверка работающего контейнера opencode: права и политики действуют так, как задано в образе.

    python3 apps/opencode/verify/live.py --directory /repos/<репозиторий>

Нужна после подключения внешних источников конфига, например OpenCode Console: по документации
opencode Console может переопределять системные политики. Запросы идут из контейнера core
(`docker compose exec`), пароль берётся из его окружения. Модель не вызывается.

Код выхода: 0 — требования выполнены, 1 — есть нарушения, 2 — окружение не готово.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

import jsonc
import sandbox
from client import Api, Response
from report import Reporter

COMPOSE = sandbox.IMAGE_DIR.parents[1] / "compose.yaml"
SYSTEM_DOCUMENT = "/etc/opencode/opencode.jsonc"
# Источники конфига, которые есть в образе: системный каталог и HOME из тома opencode-data.
KNOWN_SOURCES = ("/etc/opencode", "/opencode/.config/opencode")
# Поля, которыми документ конфига меняет права агента или запускает код.
GUARDED_KEYS = (
    "permissions",
    "policies",
    "agents",
    "plugins",
    "mcp",
    "formatter",
    "lsp",
    "enterprise",
)
CHECK_TITLE = "code-dock: проверка прав (live.py)"
SETTLE_SECONDS = 20.0

# Выполняется в контейнере core: адрес и пароль opencode берутся из его окружения.
FETCH = """
import base64, os, sys, urllib.error, urllib.request
method, path, body = sys.argv[1], sys.argv[2], sys.argv[3]
token = base64.b64encode(("opencode:" + os.environ["OPENCODE_SERVER_PASSWORD"]).encode()).decode()
headers = {"authorization": "Basic " + token, "content-type": "application/json"}
data = body.encode() if body else None
req = urllib.request.Request(os.environ["OPENCODE_URL"] + path, data, headers, method=method)
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        status, payload = resp.status, resp.read()
except urllib.error.HTTPError as error:
    status, payload = error.code, error.read()
sys.stdout.write(str(status) + "\\n" + payload.decode(errors="replace"))
"""


class ComposeApi(Api):
    """API opencode через `docker compose exec` в контейнер core."""

    def __init__(self, docker: str, compose: Path) -> None:
        super().__init__("compose:core", "")
        self._argv = [docker, "compose", "-f", str(compose), "exec", "-T", "core"]

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, str] | None = None,
        auth: bool = True,
        token: str | None = None,
        timeout: float = 60.0,
    ) -> Response:
        if params:
            path += "?" + urllib.parse.urlencode(params)
        payload = "" if body is None else json.dumps(body)
        argv = [*self._argv, "python", "-c", FETCH, method, path, payload]
        result = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=timeout
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or f"код {result.returncode}")
        status, _, text = result.stdout.partition("\n")
        return Response(int(status), text.encode())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--directory",
        required=True,
        help="папка в контейнере, для которой собирается конфиг, например /repos/notes",
    )
    parser.add_argument("--config", type=Path, default=sandbox.SYSTEM_CONFIG)
    parser.add_argument("--dockerfile", type=Path, default=sandbox.DOCKERFILE)
    parser.add_argument(
        "--url",
        help="адрес opencode напрямую, без docker (пароль из OPENCODE_SERVER_PASSWORD)",
    )
    return parser.parse_args()


def is_known(path: str | None) -> bool:
    return path is not None and any(
        path == root or path.startswith(root + "/") for root in KNOWN_SOURCES
    )


def describe(entry: dict[str, Any]) -> str:
    keys = sorted(entry.get("info", {}))
    return f"{entry['type']} {entry.get('path') or '(без пути)'} {keys}"


def check_server(api: Api, rep: Reporter, version: str) -> None:
    rep.section("Сервер")
    info = api.data("GET", "/api/info")
    running = str(info.get("version", "")) if isinstance(info, dict) else ""
    rep.requirement(
        "live.version",
        f"opencode той же версии, что в Dockerfile ({version})",
        running.lstrip("v") == version,
        f"сервер: {running or info!r}",
    )
    connected = [
        f"{i['id']} ({i['name']})"
        for i in api.data("GET", "/api/integration")
        if i.get("connections")
    ]
    print(f"  подключены интеграции: {', '.join(connected) or 'нет'}")


def check_config(
    api: Api, rep: Reporter, config: dict[str, Any], location: dict[str, str]
) -> None:
    rep.section("Документы конфига")
    entries: list[dict[str, Any]] = api.data("GET", "/api/config", params=location)
    for entry in entries:
        print(f"  {describe(entry)}")
    foreign = [e for e in entries if not is_known(e.get("path"))]
    rep.requirement(
        "live.config.sources",
        "конфиг собран только из системного каталога и HOME образа",
        not foreign,
        "; ".join(describe(e) for e in foreign),
    )
    system: dict[str, Any] = {}
    for entry in entries:
        if entry["type"] == "document" and entry.get("path") == SYSTEM_DOCUMENT:
            system.update(entry["info"])
    for key in ("permissions", "policies", "agents"):
        rep.requirement(
            f"live.config.system-{key}",
            f"{key} системного конфига загружены ровно как в образе",
            system.get(key) == config.get(key),
            f"загружено: {json.dumps(system.get(key), ensure_ascii=False)[:300]}",
        )
    guarded = [
        f"{e.get('path') or '(без пути)'}: {key}"
        for e in entries
        if e["type"] == "document" and e.get("path") != SYSTEM_DOCUMENT
        for key in GUARDED_KEYS
        if key in e.get("info", {})
    ]
    rep.requirement(
        "live.config.no-foreign-rules",
        "права, политики, агенты, плагины и MCP задаёт только системный конфиг",
        not guarded,
        "; ".join(guarded),
    )


def find_block(rules: list[Any], block: list[Any]) -> int:
    """Начало последнего вхождения `block` в `rules` подряд или -1."""
    for start in range(len(rules) - len(block), -1, -1):
        if rules[start : start + len(block)] == block:
            return start
    return -1


def settled_agents(api: Api, location: dict[str, str], block: list[Any]) -> list[Any]:
    """Сразу после загрузки папки агенты приходят только со встроенными правилами,
    правила конфига добавляются позже. Ждём, пока системные правила появятся у всех."""
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        agents: list[dict[str, Any]] = api.data("GET", "/api/agent", params=location)
        ready = all(find_block(a["permissions"], block) >= 0 for a in agents)
        if (agents and ready) or time.monotonic() > deadline:
            return agents
        time.sleep(0.5)


def load_agents(
    api: Api, rep: Reporter, directory: str, location: dict[str, str], block: list[Any]
) -> list[dict[str, Any]]:
    """Агенты появляются, когда для папки создан хотя бы один чат. Если их нет,
    создаётся временный чат и удаляется после чтения."""
    if api.data("GET", "/api/agent", params=location):
        return settled_agents(api, location, block)
    body = {"location": {"directory": directory}, "title": CHECK_TITLE}
    try:
        session_id = api.data("POST", "/api/session", body=body)["id"]
    except RuntimeError as error:
        rep.failure("live.agents.session", "временный чат создаётся", str(error)[:300])
        return []
    print(f"  создан временный чат {session_id}, после проверки он удаляется")
    try:
        return settled_agents(api, location, block)
    finally:
        api.request("DELETE", f"/api/session/{session_id}")


def check_agents(
    api: Api,
    rep: Reporter,
    config: dict[str, Any],
    directory: str,
    location: dict[str, str],
) -> None:
    rep.section("Итоговые правила агентов")
    block = config["permissions"]
    agents = load_agents(api, rep, directory, location, block)
    rep.requirement(
        "live.agents.loaded",
        "сервер отдаёт список агентов",
        bool(agents),
        "список пуст",
    )
    for agent in agents:
        rules = agent["permissions"]
        start = find_block(rules, block)
        tail = rules[start + len(block) :] if start >= 0 else []
        looser = [r for r in tail if r.get("effect") != "deny"]
        detail = (
            "правил системного конфига нет, последние: "
            + json.dumps(rules[-4:], ensure_ascii=False)[:300]
            if start < 0
            else f"после них: {json.dumps(looser, ensure_ascii=False)[:300]}"
        )
        rep.requirement(
            f"live.agents.{agent['id']}",
            f"агент {agent['id']}: после системных правил только запреты",
            start >= 0 and not looser,
            detail,
        )


def main() -> int:
    args = parse_args()
    config = jsonc.loads(args.config.read_text(encoding="utf-8"))
    version = sandbox.parse_dockerfile(
        args.dockerfile.read_text(encoding="utf-8")
    ).version
    api: Api
    if args.url:
        api = Api(args.url, os.environ.get("OPENCODE_SERVER_PASSWORD", ""))
    else:
        docker = shutil.which("docker")
        if not docker:
            print("Окружение не готово: не найден docker", file=sys.stderr)
            return 2
        api = ComposeApi(docker, COMPOSE)
    location = {"location[directory]": args.directory}
    rep = Reporter()
    try:
        check_server(api, rep, version)
        check_config(api, rep, config, location)
        check_agents(api, rep, config, args.directory, location)
    except RuntimeError as error:
        rep.failure("live.request", "запрос к opencode выполняется", str(error)[:300])
    print(f"\n{rep.summary()}")
    print(
        "Не проверено: политики, которые применяются вне конфига, и поведение модели."
        " Это проверяет стенд run.py на нативном opencode."
    )
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(main())
