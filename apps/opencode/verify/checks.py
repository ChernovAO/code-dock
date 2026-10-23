"""Проверки образа opencode.

Требования (`Reporter.requirement`) при нарушении дают ошибку. Известные ограничения
(`Reporter.limitation`) описаны в docs/opencode-integration.md: проверка только сообщает,
воспроизводятся ли они, чтобы при смене версии вовремя обновить документацию.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fixtures
from client import (
    Api,
    EventTap,
    call,
    edit,
    glob,
    grep,
    location,
    parse_frames,
    read,
    shell,
    write,
)
from fixtures import (
    ALPHA,
    BETA,
    HOSTILE,
    LINKS_P,
    LOOSE,
    PROJ_X,
    PROJ_Y,
)
from mock_llm import MockLLM
from report import Reporter
from sandbox import Server

# Операции API, на которые опирается docs/opencode-integration.md.
REQUIRED_OPERATIONS = frozenset(
    {
        "agent.get",
        "agent.list",
        "config.get",
        "event.subscribe",
        "experimental.session.instructions.entry.list",
        "experimental.session.instructions.entry.put",
        "integration.connect.key",
        "model.list",
        "permission.request.list",
        "permission.saved.list",
        "permission.saved.remove",
        "provider.list",
        "server.info",
        "session.active",
        "session.create",
        "session.form.list",
        "session.form.reply",
        "session.get",
        "session.interrupt",
        "session.list",
        "session.log",
        "session.message.get",
        "session.message.list",
        "session.permission.list",
        "session.permission.reply",
        "session.prompt",
        "session.remove",
        "session.switchAgent",
        "session.switchModel",
        "session.update",
    }
)

QUESTION = call(
    "question",
    questions=[
        {
            "question": "Какой формат?",
            "header": "Формат",
            "options": [{"label": "A", "description": "первый"}],
        }
    ],
)


@dataclass
class Ctx:
    api: Api
    mock: MockLLM
    server: Server
    config: dict[str, Any]
    pinned_version: str
    scratch: Path


@dataclass(frozen=True)
class Case:
    id: str
    title: str
    location: str
    calls: tuple[str, ...]
    expect: str
    agent: str | None = None


def _case(
    check_id: str,
    title: str,
    where: str,
    call_text: str,
    expect: str,
    agent: str | None = None,
) -> Case:
    return Case(check_id, title, where, (call_text,), expect, agent)


def run_table(ctx: Ctx, rep: Reporter, cases: list[Case]) -> None:
    selected = [c for c in cases if rep.matches(c.id)]
    if not selected:
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(
            pool.map(
                lambda c: ctx.api.run(c.location, c.calls, agent=c.agent), selected
            )
        )
    for case, outcome in zip(selected, outcomes, strict=True):
        detail = (
            f"ожидалось {case.expect}, получено {outcome.kind}: {outcome.summary()}"
        )
        rep.requirement(case.id, case.title, outcome.kind == case.expect, detail)


def wait_for[T](
    fetch: Callable[[], T], done: Callable[[T], bool], timeout: float = 10.0
) -> T:
    deadline = time.monotonic() + timeout
    while True:
        value = fetch()
        if done(value) or time.monotonic() >= deadline:
            return value
        time.sleep(0.25)


def offered_tools(ctx: Ctx, where: str, agent: str | None = None) -> set[str]:
    """Инструменты, которые opencode отдал модели в чате этого проекта."""
    ctx.mock.clear()
    ctx.api.run(where, ["привет"], agent=agent)
    return ctx.mock.offered_tools()


def markers_in_prompt(ctx: Ctx, where: str) -> set[str]:
    ctx.mock.clear()
    ctx.api.run(where, ["привет"])
    return ctx.mock.markers_seen()


def file_markers(ctx: Ctx, names: tuple[str, ...]) -> set[str]:
    hostile = ctx.server.repos / "hostile"
    return {name for name in names if (hostile / name).exists()}


def system_permissions(ctx: Ctx) -> list[dict[str, str]]:
    permissions: list[dict[str, str]] = ctx.config["permissions"]
    return permissions


# --- Статические проверки -------------------------------------------------------------


def static_checks(
    rep: Reporter, version: str, env: dict[str, str], compose: Path
) -> None:
    rep.section("Статические проверки")
    pinned = re.fullmatch(r"\d+\.\d+\.\d+", version) is not None
    rep.requirement(
        "static.version-pinned",
        "версия opencode в Dockerfile зафиксирована точным тегом",
        pinned,
        f"тег: {version!r}",
    )
    rep.requirement(
        "static.project-config-off",
        "образ отключает конфиг репозитория (OPENCODE_DISABLE_PROJECT_CONFIG=1)",
        env.get("OPENCODE_DISABLE_PROJECT_CONFIG") == "1",
        f"значение: {env.get('OPENCODE_DISABLE_PROJECT_CONFIG')!r}",
    )
    compose_checks(rep, compose)


def compose_checks(rep: Reporter, compose: Path) -> None:
    docker = shutil.which("docker")
    if (
        not docker
        or subprocess.run(
            [docker, "compose", "version"], check=False, capture_output=True
        ).returncode
    ):
        rep.skip("static.compose", "проверка compose.yaml", "нет docker compose")
        return
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "/")}

    def config(env_file: str) -> subprocess.CompletedProcess[str]:
        argv = [docker, "compose", "-f", str(compose), "--env-file", env_file]
        argv += ["config", "--format", "json"]
        return subprocess.run(
            argv, check=False, env=env, capture_output=True, text=True, timeout=60
        )

    bare = config(os.devnull)
    rep.requirement(
        "static.compose.password-required",
        "без OPENCODE_SERVER_PASSWORD compose не запускается",
        bare.returncode != 0 and "OPENCODE_SERVER_PASSWORD" in bare.stderr,
        bare.stderr.strip()[:150],
    )
    with tempfile.NamedTemporaryFile("w", suffix=".env") as env_file:
        env_file.write("OPENCODE_SERVER_PASSWORD=x\n")
        env_file.flush()
        without_repos = config(env_file.name)
    rep.requirement(
        "static.compose.repos-dir-required",
        "без HOST_REPOS_DIR compose не запускается: путь репозиториев задаётся на старте",
        without_repos.returncode != 0 and "HOST_REPOS_DIR" in without_repos.stderr,
        without_repos.stderr.strip()[:150],
    )
    with tempfile.NamedTemporaryFile("w", suffix=".env") as env_file:
        env_file.write("OPENCODE_SERVER_PASSWORD=x\nHOST_REPOS_DIR=./data/repos\n")
        env_file.flush()
        full = config(env_file.name)
    if full.returncode:
        rep.requirement(
            "static.compose.valid", "compose.yaml разбирается", False, full.stderr[:200]
        )
        return
    services = json.loads(full.stdout)["services"]
    for name in ("opencode", "core"):
        rep.requirement(
            f"static.compose.{name}-not-published",
            f"{name} не публикует порты наружу",
            not services[name].get("ports"),
            str(services[name].get("ports")),
        )
    published = [
        (p.get("published"), p.get("target")) for p in services["frontend"]["ports"]
    ]
    rep.requirement(
        "static.compose.frontend-only",
        "наружу опубликован только фронтенд (8080 -> 80)",
        published == [("8080", 80)],
        str(published),
    )
    mounts = {v["target"] for v in services["opencode"]["volumes"]}
    rep.requirement(
        "static.compose.mounts",
        "opencode получает /repos и /opencode",
        {"/repos", "/opencode"} <= mounts,
        str(sorted(mounts)),
    )


# --- Профиль main: образ как есть -----------------------------------------------------


def main_checks(ctx: Ctx, rep: Reporter) -> None:
    check_auth(ctx, rep)
    check_api_contract(ctx, rep)
    check_config(ctx, rep)
    check_boundary(ctx, rep)
    check_env_files(ctx, rep)
    check_asks(ctx, rep)
    check_agents(ctx, rep)
    check_hostile_repo(ctx, rep)
    check_instructions(ctx, rep)
    check_permission_flow(ctx, rep)
    check_events(ctx, rep)
    check_limitations(ctx, rep)
    check_provider_and_cli(ctx, rep)
    rep.requirement(
        "config.no-warnings",
        "в логе сервера нет WARN и ERROR (так opencode сообщает о битом конфиге)",
        not ctx.server.log_issues(),
        "; ".join(ctx.server.log_issues()[:2]),
    )


def check_auth(ctx: Ctx, rep: Reporter) -> None:
    if not rep.wants("auth"):
        return
    api = ctx.api
    anonymous = api.request("GET", "/api/info", auth=False).status
    rep.requirement(
        "auth.none", "без авторизации — 401", anonymous == 401, str(anonymous)
    )
    wrong = Api(api.base_url, "wrong-password").request("GET", "/api/info").status
    rep.requirement(
        "auth.wrong-password", "неверный пароль — 401", wrong == 401, str(wrong)
    )
    other = Api(api.base_url, ctx.server.password, user="admin")
    status = other.request("GET", "/api/info").status
    rep.requirement(
        "auth.wrong-user", "другое имя пользователя — 401", status == 401, str(status)
    )
    spec = api.request("GET", "/openapi.json", auth=False).status
    rep.requirement(
        "auth.openapi", "/openapi.json без авторизации — 401", spec == 401, str(spec)
    )
    info = api.request("GET", "/api/info")
    version = info.json().get("version") if info.status == 200 else None
    rep.requirement(
        "auth.ok",
        "верный пароль пускает, OPENCODE_SERVER_PASSWORD его фиксирует",
        info.status == 200,
        str(info.status),
    )
    rep.requirement(
        "server.version",
        "сервер сообщает версию из Dockerfile",
        version == ctx.pinned_version,
        f"сервер: {version}, Dockerfile: {ctx.pinned_version}",
    )


def check_api_contract(ctx: Ctx, rep: Reporter) -> None:
    if not rep.wants("api"):
        return
    spec = ctx.api.request("GET", "/openapi.json").json()
    operations = {
        op["operationId"]
        for path in spec["paths"].values()
        for op in path.values()
        if isinstance(op, dict) and "operationId" in op
    }
    missing = sorted(REQUIRED_OPERATIONS - operations)
    rep.requirement(
        "api.operations",
        f"в API есть все {len(REQUIRED_OPERATIONS)} операций, на которые опирается документация",
        not missing,
        f"нет: {missing}",
    )


def check_config(ctx: Ctx, rep: Reporter) -> None:
    if not rep.wants("config"):
        return
    documents = ctx.api.data("GET", "/api/config", params=location(PROJ_X))
    system = [
        d["info"]
        for d in documents
        if d["type"] == "document" and d.get("path") == "/etc/opencode/opencode.jsonc"
    ]
    for key in ("policies", "permissions"):
        loaded = [info[key] for info in system if key in info]
        rep.requirement(
            f"config.{key}",
            f"{key} системного конфига загружены ровно как написаны",
            loaded == [ctx.config[key]],
            f"загружено документов: {len(loaded)}",
        )
    out = ctx.api.run(
        PROJ_X, [shell('echo "BASH_VERSION=[$BASH_VERSION]"')], replies=["once"]
    )
    rep.requirement(
        "config.shell-bash",
        "shell запускается под bash, как задано в конфиге",
        out.kind == "ok" and re.search(r"BASH_VERSION=\[\d", out.text) is not None,
        out.summary(),
    )


def check_boundary(ctx: Ctx, rep: Reporter) -> None:
    if not rep.wants("boundary") and not rep.wants("git"):
        return
    # fmt: off
    cases = [
        _case("boundary.read.project-abs", "чтение файла проекта по абсолютному пути",
              PROJ_X, read(f"{PROJ_X}/notes.md"), "ok"),
        _case("boundary.read.project-rel", "чтение файла проекта по относительному пути",
              PROJ_X, read("notes.md"), "ok"),
        _case("boundary.read.neighbor-abs", "чтение соседнего проекта того же репозитория",
              PROJ_X, read(f"{PROJ_Y}/neighbor.md"), "ok"),
        _case("boundary.read.neighbor-rel", "чтение соседнего проекта по ../",
              PROJ_X, read("../y/neighbor.md"), "ok"),
        _case("boundary.read.repo-root", "чтение файла в корне репозитория",
              PROJ_X, read(f"{ALPHA}/README.md"), "ok"),
        _case("boundary.read.symlink-inside", "чтение symlink на соседнюю папку внутри репозитория",
              LINKS_P, read(f"{LINKS_P}/neighbor.md"), "ok"),
        _case("boundary.read.other-repo", "чтение чужого репозитория запрещено",
              PROJ_X, read(f"{BETA}/other.md"), "blocked"),
        _case("boundary.read.escape-rel", "выход через ../../ запрещён",
              PROJ_X, read("../../beta/other.md"), "blocked"),
        _case("boundary.read.loose-file", "файл вне репозиториев на томе запрещён",
              PROJ_X, read(LOOSE), "blocked"),
        _case("boundary.read.etc", "чтение /etc/passwd запрещено",
              PROJ_X, read("/etc/passwd"), "blocked"),
        _case("boundary.glob.other-repo", "glob по чужому репозиторию запрещён",
              PROJ_X, glob("*", path=BETA), "blocked"),
        _case("boundary.grep.other-repo", "grep по чужому репозиторию запрещён",
              PROJ_X, grep("beta", path=BETA), "blocked"),
        _case("boundary.grep.neighbor", "grep по соседнему проекту разрешён",
              PROJ_X, grep("соседний", path=PROJ_Y), "ok"),
        _case("boundary.write.project-abs", "запись в проект по абсолютному пути",
              PROJ_X, write(f"{PROJ_X}/new-abs.md"), "ok"),
        _case("boundary.write.project-rel", "запись в проект по относительному пути",
              PROJ_X, write("new-rel.md"), "ok"),
        _case("boundary.write.neighbor", "запись в соседний проект того же репозитория",
              PROJ_X, write("../y/new.md"), "ok"),
        _case("boundary.write.dot-github", "запись в .github не путается с .git",
              PROJ_X, write(f"{PROJ_X}/.github/x"), "ok"),
        _case("boundary.write.repo-gitignore", "запись .gitignore в корне репозитория",
              PROJ_X, write(f"{ALPHA}/.gitignore"), "ok"),
        _case("boundary.write.other-repo", "запись в чужой репозиторий запрещена",
              PROJ_X, write(f"{BETA}/x.md"), "blocked"),
        _case("boundary.write.escape-rel", "запись через ../../ запрещена",
              PROJ_X, write("../../beta/x.md"), "blocked"),
        _case("boundary.write.etc", "запись в /etc запрещена",
              PROJ_X, write("/etc/x"), "blocked"),
        _case("boundary.shell.workdir", "shell с workdir в чужом репозитории запрещён",
              PROJ_X, shell("ls", workdir=BETA), "blocked"),
        _case("git.nested.config-abs", "проект во вложенной папке: .git/config по абсолютному пути",
              PROJ_X, write(f"{ALPHA}/.git/config"), "blocked"),
        _case("git.nested.hook-rel", "проект во вложенной папке: хук по ../.git",
              PROJ_X, write("../.git/hooks/pre-commit"), "blocked"),
        _case("git.nested.hook-abs", "проект во вложенной папке: хук по абсолютному пути",
              PROJ_X, write(f"{ALPHA}/.git/hooks/pre-commit"), "blocked"),
        _case("git.nested.edit-head", "проект во вложенной папке: edit .git/HEAD",
              PROJ_X, edit(f"{ALPHA}/.git/HEAD", "main", "evil"), "blocked"),
        _case("git.root.hook-rel", "проект в корне репозитория: хук по .git/",
              ALPHA, write(".git/hooks/pre-commit"), "blocked"),
        _case("git.root.hook-abs", "проект в корне репозитория: хук по абсолютному пути",
              ALPHA, write(f"{ALPHA}/.git/hooks/post-commit"), "blocked"),
        _case("git.root.config-rel", "проект в корне репозитория: .git/config",
              ALPHA, write(".git/config"), "blocked"),
        _case("git.root.regular", "проект в корне репозитория: обычный файл пишется",
              ALPHA, write("x/regular.md"), "ok"),
    ]
    # fmt: on
    run_table(ctx, rep, cases)
    git_dir = ctx.server.repos / "alpha" / ".git"
    hooks = [
        p.name for p in (git_dir / "hooks").iterdir() if not p.name.endswith(".sample")
    ]
    intact = "repositoryformatversion" in (git_dir / "config").read_text()
    rep.requirement(
        "git.disk-intact",
        "на диске .git не тронут: хуков нет, config цел",
        not hooks and intact,
        f"хуки: {hooks}, config цел: {intact}",
    )


def check_env_files(ctx: Ctx, rep: Reporter) -> None:
    if not rep.wants("env"):
        return
    # fmt: off
    cases = [
        _case("env.project", ".env проекта читается только с подтверждением",
              PROJ_X, read(f"{PROJ_X}/.env"), "ask"),
        _case("env.repo-root", ".env в корне репозитория — с подтверждением",
              PROJ_X, read(f"{ALPHA}/.env"), "ask"),
        _case("env.local", ".env.local — с подтверждением",
              PROJ_X, read(f"{ALPHA}/.env.local"), "ask"),
        _case("env.example", ".env.example читается без вопроса",
              PROJ_X, read(f"{ALPHA}/.env.example"), "ok"),
    ]
    # fmt: on
    run_table(ctx, rep, cases)


def check_asks(ctx: Ctx, rep: Reporter) -> None:
    if not rep.wants("ask"):
        return
    subagent = call("subagent", agent="explore", description="d", prompt="p")
    # fmt: off
    cases = [
        _case("ask.shell", "shell спрашивает разрешение", PROJ_X, shell("echo hi"), "ask"),
        _case("ask.webfetch", "webfetch спрашивает разрешение",
              PROJ_X, call("webfetch", url="https://example.com"), "ask"),
        _case("ask.websearch", "websearch спрашивает разрешение",
              PROJ_X, call("websearch", query="test"), "ask"),
        _case("ask.subagent", "subagent спрашивает разрешение", PROJ_X, subagent, "ask"),
        _case("ask.question", "question спрашивает разрешение", PROJ_X, QUESTION, "ask"),
    ]
    # fmt: on
    run_table(ctx, rep, cases)


def check_agents(ctx: Ctx, rep: Reporter) -> None:
    if not rep.wants("agents") and not rep.wants("tools"):
        return
    tools = offered_tools(ctx, PROJ_X)
    rep.requirement(
        "tools.no-execute",
        "Code Mode (execute) не предлагается модели",
        "execute" not in tools,
        f"инструменты: {sorted(tools)}",
    )
    rep.requirement(
        "tools.baseline",
        "обычные инструменты (read, write, edit, shell) предлагаются",
        {"read", "write", "edit", "shell"} <= tools,
        f"инструменты: {sorted(tools)}",
    )
    plan_tools = offered_tools(ctx, PROJ_X, agent="plan")
    rep.requirement(
        "agents.plan-readonly",
        "у режима plan нет инструментов записи",
        not plan_tools & {"write", "edit"} and "read" in plan_tools,
        f"инструменты: {sorted(plan_tools)}",
    )
    # fmt: off
    cases = [
        _case("agents.plan-write", "режим plan не может записать файл",
              PROJ_X, write("plan.md"), "nocall", agent="plan"),
        _case("agents.plan-shell", "режим plan спрашивает про shell",
              PROJ_X, shell("echo hi"), "ask", agent="plan"),
        _case("agents.build-write", "режим build пишет без вопросов",
              PROJ_X, write("build.md"), "ok", agent="build"),
    ]
    # fmt: on
    run_table(ctx, rep, cases)


def check_hostile_repo(ctx: Ctx, rep: Reporter) -> None:
    """Репозиторий со своим конфигом не должен исполнять код и снимать ограничения."""
    if not rep.wants("hostile"):
        return
    ctx.api.run(HOSTILE, [write("trigger.md", "# x\n")])
    agents = [
        a["id"] for a in ctx.api.data("GET", "/api/agent", params=location(HOSTILE))
    ]
    tools = offered_tools(ctx, HOSTILE)
    time.sleep(3)
    found = file_markers(
        ctx, (*fixtures.PLUGIN_MARKERS, fixtures.MCP_MARKER, fixtures.FORMATTER_MARKER)
    )
    rep.requirement(
        "hostile.no-code-execution",
        "плагин, MCP и formatter из репозитория не выполняются",
        not found,
        f"сработали: {sorted(found)}",
    )
    rep.requirement(
        "hostile.no-free-agent",
        "агент из .opencode/ репозитория не появился",
        "free" not in agents,
        f"агенты: {agents}",
    )
    rep.requirement(
        "hostile.no-execute",
        "репозиторий не вернул Code Mode",
        "execute" not in tools,
        f"инструменты: {sorted(tools)}",
    )
    # fmt: off
    cases = [
        _case("hostile.shell-asks", "shell спрашивает, хотя репозиторий разрешил всё",
              HOSTILE, shell("echo hi"), "ask"),
        _case("hostile.boundary", "чужой репозиторий закрыт, хотя репозиторий снял политику",
              HOSTILE, read(f"{BETA}/other.md"), "blocked"),
        _case("hostile.git", ".git закрыт, хотя репозиторий снял политику",
              HOSTILE, write(".git/config"), "blocked"),
    ]
    # fmt: on
    run_table(ctx, rep, cases)


def check_instructions(ctx: Ctx, rep: Reporter) -> None:
    if not rep.wants("instructions"):
        return
    seen = markers_in_prompt(ctx, PROJ_X)
    rep.requirement(
        "instructions.project-ignored",
        "AGENTS.md проекта и корня не подхватываются (флаг отключает конфиг репозитория)",
        not seen,
        f"в промпте: {sorted(seen)}",
    )
    session = ctx.api.create_session(PROJ_X)
    sid = str(session["id"])
    body = {"value": f"{fixtures.PROJECT_MARKER}: передано через API"}
    path = f"/api/experimental/session/{sid}/instructions/entries/agents-md"
    ctx.api.data("PUT", path, body=body)
    ctx.mock.clear()
    ctx.api.run(PROJ_X, ["привет"], session=session)
    rep.requirement(
        "instructions.entries",
        "инструкции, переданные через API записей, попадают в промпт модели",
        fixtures.PROJECT_MARKER in ctx.mock.markers_seen(),
        f"в промпте: {sorted(ctx.mock.markers_seen())}",
    )


def check_permission_flow(ctx: Ctx, rep: Reporter) -> None:
    if not rep.wants("permissions"):
        return
    api = ctx.api
    once = api.run(PROJ_X, [shell("echo ask-once")], replies=["once"])
    rep.requirement(
        "permissions.once",
        "ответ once выполняет команду",
        once.kind == "ok" and "ask-once" in once.text and len(once.asked) == 1,
        once.summary(),
    )
    rejected = api.run(
        PROJ_X,
        [shell("echo one"), shell("echo two")],
        replies=[("reject", "не надо")],
    )
    first = rejected.calls[0] if rejected.calls else None
    declined = first is not None and (first.status, first.text) == ("error", "не надо")
    rep.requirement(
        "permissions.reject",
        "reject отдаёт модели текст отказа, а цикл агента продолжается новым запросом",
        declined and len(rejected.asked) == 2,
        f"вызовы: {[(c.status, c.text) for c in rejected.calls]}, запросов: {len(rejected.asked)}",
    )
    check_always(ctx, rep)
    check_question_form(ctx, rep)


def check_always(ctx: Ctx, rep: Reporter) -> None:
    api = ctx.api
    command = shell("uname -s")
    first = api.run(PROJ_X, [command], replies=["always"])
    project_id = api.data("GET", f"/api/session/{first.session_id}")["projectID"]
    same = api.run(PROJ_X, [command])
    neighbor = api.run(PROJ_Y, [command])
    other = api.run(BETA, [command])
    saved = api.data("GET", "/api/permission/saved", params={"projectID": project_id})
    rep.requirement(
        "permissions.always.executes",
        "ответ always выполняет команду и предлагает шаблон для сохранения",
        first.kind == "ok" and first.asked[0].save == ("uname *",),
        first.summary(),
    )
    rep.requirement(
        "permissions.always.same-project",
        "always действует в новом чате того же проекта",
        same.kind == "ok" and not same.asked,
        same.summary(),
    )
    rep.requirement(
        "permissions.always.neighbor-project",
        "always действует во всём репозитории (соседний проект)",
        neighbor.kind == "ok" and not neighbor.asked,
        neighbor.summary(),
    )
    rep.requirement(
        "permissions.always.other-repo",
        "в другом репозитории always не действует",
        other.kind == "ask",
        other.summary(),
    )
    rep.requirement(
        "permissions.always.saved-list",
        "сохранённое решение видно в списке по projectID",
        any(s["action"] == "shell" and s["resource"] == "uname *" for s in saved),
        str(saved)[:150],
    )


def check_question_form(ctx: Ctx, rep: Reporter) -> None:
    api = ctx.api
    session = api.create_session(PROJ_X)
    sid = str(session["id"])
    api.prompt(sid, QUESTION)
    asks = wait_for(lambda: api.pending(sid), bool)
    if asks:
        api.reply(sid, asks[0].id, "once")
    forms = wait_for(lambda: api.data("GET", f"/api/session/{sid}/form"), bool)
    kinds = [f.get("metadata", {}).get("kind") for f in forms]
    api.interrupt(sid)
    rep.requirement(
        "permissions.question-form",
        "question: сначала запрос разрешения, затем форма, на которую нужен ответ",
        bool(asks) and asks[0].action == "question" and kinds == ["question"],
        f"запросов: {len(asks)}, формы: {kinds}",
    )


def check_events(ctx: Ctx, rep: Reporter) -> None:
    if not rep.wants("events"):
        return
    tap = EventTap(ctx.api)
    time.sleep(0.5)
    out = ctx.api.run(
        PROJ_X, [shell("echo events"), read("notes.md")], replies=["once"]
    )
    time.sleep(1.5)
    text = tap.text()
    tap.close()
    events, other = parse_frames(text)
    ours = [e for e in events if e.get("data", {}).get("sessionID") == out.session_id]
    durable = [e["durable"]["seq"] for e in ours if "durable" in e]
    ephemeral = {e["type"] for e in ours if "durable" not in e}
    created = next((e for e in ours if e["type"] == "session.created"), None)
    rep.requirement(
        "events.no-sse-fields",
        "кадры /api/event содержат только data: (полей id: и event: нет)",
        bool(events) and not [ln for ln in other if ln.startswith(("id:", "event:"))],
        f"прочие строки: {other[:3]}",
    )
    rep.requirement(
        "events.first-event",
        "первое событие потока — server.connected",
        bool(events) and events[0].get("type") == "server.connected",
        str(events[0].get("type") if events else None),
    )
    rep.requirement(
        "events.durable-seq",
        "долговечные события чата несут durable.seq, нумерация идёт с нуля по возрастанию",
        bool(created)
        and created is not None
        and created["durable"]["seq"] == 0
        and durable == sorted(durable)
        and len(durable) > 5,
        f"seq: {durable[:8]}",
    )
    rep.requirement(
        "events.ephemeral",
        "permission.asked, permission.replied и дельты текста приходят без durable",
        {"permission.asked", "permission.replied", "session.text.delta"} <= ephemeral,
        f"эфемерные: {sorted(ephemeral)}",
    )
    asked = next((e for e in ours if e["type"] == "permission.asked"), None)
    replied = next((e for e in ours if e["type"] == "permission.replied"), None)
    asked_keys = {"id", "sessionID", "action", "resources", "save", "source"}
    rep.requirement(
        "events.permission-payload",
        "permission.asked и permission.replied несут поля, на которые рассчитан интерфейс",
        bool(asked)
        and asked is not None
        and asked_keys <= set(asked["data"])
        and bool(replied)
        and replied is not None
        and replied["data"].get("reply") == "once",
        f"ключи asked: {sorted(asked['data']) if asked else None}",
    )
    log = ctx.api.request(
        "GET", f"/api/experimental/session/{out.session_id}/log", params={"after": "0"}
    )
    log_types = [e.get("type") for e in parse_frames(log.text())[0]]
    rep.limitation(
        "limit.log-replay",
        "журнал сессии не отдаёт события, докачки по seq нет",
        log.status == 200 and log_types == ["log.synced"],
        f"типы: {log_types[:5]}. Если журнал заработал, пересмотрите решение 4 плана",
    )


def check_limitations(ctx: Ctx, rep: Reporter) -> None:
    """Известные ограничения: см. «Что не защищено» в docs/opencode-integration.md."""
    if not rep.wants("limit"):
        return
    api = ctx.api
    links_git = ctx.server.repos / "links" / ".git" / "config"
    victim = ctx.server.repos / "victim" / "data.md"

    out = api.run(LINKS_P, [read(f"{LINKS_P}/etc")])
    rep.limitation(
        "limit.symlink-read-system",
        "symlink внутри проекта на /etc/passwd читается",
        out.kind == "ok" and "root:" in out.text,
        out.summary(),
    )
    out = api.run(LINKS_P, [read(f"{LINKS_P}/out.md")])
    rep.limitation(
        "limit.symlink-read-other-repo",
        "symlink на файл другого репозитория читается",
        out.kind == "ok" and "жертвы" in out.text,
        out.summary(),
    )
    out = api.run(LINKS_P, [write(f"{LINKS_P}/out.md", "ПЕРЕЗАПИСАНО\n")])
    rep.limitation(
        "limit.symlink-write-other-repo",
        "запись через symlink меняет файл в другом репозитории",
        out.kind == "ok" and victim.read_text() == "ПЕРЕЗАПИСАНО\n",
        out.summary(),
    )
    out = api.run(LINKS_P, [write(f"{LINKS_P}/gitconfig", "[core]\n")])
    rep.limitation(
        "limit.symlink-write-git-config",
        "запись через symlink меняет .git/config, политика edit:.git его не видит",
        out.kind == "ok" and links_git.read_text() == "[core]\n",
        out.summary(),
    )
    out = api.run(PROJ_X, [shell("cat /etc/passwd")])
    rep.limitation(
        "limit.shell-outside-path",
        "shell с путём вне репозитория в аргументах только спрашивает разрешение",
        out.kind == "ask",
        out.summary(),
    )
    out = api.run(PROJ_X, [grep("SECRET", path=f"{PROJ_X}/.env")])
    rep.limitation(
        "limit.grep-env",
        "grep читает .env без вопроса",
        out.kind == "ok" and "SECRET" in out.text,
        out.summary(),
    )
    out = api.run(PROJ_X, [shell("seq 1 100000")], replies=["once"])
    saved = re.search(r"full output saved to ([^\s\]]+)", out.text)
    if saved is None:
        rep.skip(
            "limit.spill-output",
            "полный вывод команды недоступен агенту",
            out.summary(),
        )
        return
    out = api.run(PROJ_X, [read(saved.group(1))])
    rep.limitation(
        "limit.spill-output",
        "полный вывод длинной команды сохраняется вне репозитория и агенту недоступен",
        out.kind == "blocked",
        out.summary(),
    )


def check_provider_and_cli(ctx: Ctx, rep: Reporter) -> None:
    if not rep.wants("provider") and not rep.wants("cli"):
        return
    api = ctx.api
    where = location(PROJ_X)
    # Интеграции появляются после того, как location загрузил каталог провайдеров при первом ходе.
    api.run(PROJ_X, ["привет"])

    def providers() -> set[str]:
        return {p["id"] for p in api.data("GET", "/api/provider", params=where)}

    before = providers()
    key = {"key": "sk-ant-verify-fake-key"}
    resp = api.request(
        "POST", "/api/integration/anthropic/connect/key", body=key, params=where
    )
    after = providers()
    rep.requirement(
        "provider.connect-key",
        "ключ провайдера сохраняется через API, провайдер появляется без перезапуска",
        resp.status == 204 and "anthropic" not in before and "anthropic" in after,
        f"{resp.status}, провайдеры: {sorted(after)}",
    )
    env = {"PATH": "/opt/oc:/usr/local/bin:/usr/bin:/bin", "HOME": "/opencode"}
    argv = ["/opt/oc/opencode", "auth", "list", "--server", api.base_url]
    bare = ctx.server.run(argv, env=env)
    env["OPENCODE_SERVER_PASSWORD"] = ctx.server.password
    authed = ctx.server.run(argv, env=env)

    rep.requirement(
        "cli.password-required",
        "CLI без пароля отказывается работать с сервером",
        bare.returncode != 0 and "requires a password" in bare.stderr,
        bare.stderr.strip()[:150],
    )
    rep.requirement(
        "cli.auth-list",
        "CLI с OPENCODE_SERVER_PASSWORD видит провайдеров (команда из README)",
        authed.returncode == 0
        and "Anthropic" in authed.stdout
        and "MOCK_API_KEY" in authed.stdout,
        authed.stdout.strip()[:150] or authed.stderr.strip()[:150],
    )


# --- Профиль policy-only: флаг отключён, остаются политики ------------------------------


def policy_only_checks(ctx: Ctx, rep: Reporter) -> None:
    ctx.api.run(HOSTILE, [write("trigger.md", "# x\n")])
    agents = [
        a["id"] for a in ctx.api.data("GET", "/api/agent", params=location(HOSTILE))
    ]
    hostile_tools = offered_tools(ctx, HOSTILE)
    shell_outcome = ctx.api.run(HOSTILE, [shell("echo hi")])
    time.sleep(3)
    found = file_markers(ctx, (*fixtures.PLUGIN_MARKERS, fixtures.MCP_MARKER))
    rep.requirement(
        "policy.no-plugin-mcp",
        "без флага политики всё равно не пускают плагин и MCP из репозитория",
        not found,
        f"сработали: {sorted(found)}",
    )
    # fmt: off
    cases = [
        _case("policy.boundary", "репозиторий не может снять запрет на чужой репозиторий",
              HOSTILE, read(f"{BETA}/other.md"), "blocked"),
        _case("policy.git", "репозиторий не может снять запрет на .git",
              HOSTILE, write(".git/config"), "blocked"),
    ]
    # fmt: on
    run_table(ctx, rep, cases)

    permissions = system_permissions(ctx)
    session = ctx.api.create_session(HOSTILE, permissions=permissions)
    chat_shell = ctx.api.run(HOSTILE, [shell("echo hi")], session=session)
    chat_tools = offered_tools_for(ctx, session)
    free = ctx.api.run(
        HOSTILE, [shell("echo hi")], permissions=permissions, agent="free"
    )
    rep.requirement(
        "policy.session-permissions",
        "permissions чата перекрывают правила репозитория: shell спрашивает, execute нет",
        chat_shell.kind == "ask" and "execute" not in chat_tools and free.kind == "ask",
        f"shell: {chat_shell.kind}, execute: {'execute' in chat_tools}, free: {free.kind}",
    )

    rep.limitation(
        "limit.formatter",
        "formatter из репозитория запускает команду (ради этого нужен флаг)",
        fixtures.FORMATTER_MARKER in file_markers(ctx, (fixtures.FORMATTER_MARKER,)),
        "маркер formatter не появился",
    )
    rep.limitation(
        "limit.repo-agent-override",
        "правила агента из репозитория снимают вопрос про shell и возвращают execute",
        shell_outcome.kind == "ok" and "execute" in hostile_tools and "free" in agents,
        f"shell: {shell_outcome.kind}, execute: {'execute' in hostile_tools}, агенты: {agents}",
    )
    seen = markers_in_prompt(ctx, PROJ_X)
    rep.limitation(
        "limit.agents-md-needs-config",
        "без флага AGENTS.md проекта и корня репозитория подхватываются (цена флага)",
        {fixtures.ROOT_MARKER, fixtures.PROJECT_MARKER} <= seen,
        f"в промпте: {sorted(seen)}",
    )


def offered_tools_for(ctx: Ctx, session: dict[str, Any]) -> set[str]:
    ctx.mock.clear()
    ctx.api.run(str(session["location"]["directory"]), ["привет"], session=session)
    return ctx.mock.offered_tools()


# --- Профиль control: ничего не включено, детекторы обязаны сработать --------------------


def control_checks(ctx: Ctx, rep: Reporter) -> None:
    ctx.api.run(HOSTILE, [write("trigger.md", "# x\n")])
    wanted = (*fixtures.PLUGIN_MARKERS, fixtures.MCP_MARKER)
    found = wait_for(lambda: file_markers(ctx, wanted), lambda f: f == set(wanted), 15)
    detail = (
        f"сработали: {sorted(found)}. Если уязвимость закрыта в новой версии, обновите"
        " проверки и документацию, иначе проверки профиля main ничего не доказывают"
    )
    rep.requirement(
        "control.plugin",
        "контроль: без защиты плагин из репозитория исполняется",
        set(fixtures.PLUGIN_MARKERS) <= found,
        detail,
    )
    rep.requirement(
        "control.mcp",
        "контроль: без защиты MCP-сервер из репозитория запускается",
        fixtures.MCP_MARKER in found,
        detail,
    )
