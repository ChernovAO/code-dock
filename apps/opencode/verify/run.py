"""Проверка образа opencode (apps/opencode) на нативном opencode в песочнице bwrap.

    python3 apps/opencode/verify/run.py [--profile main] [--only boundary] [--keep]

Код выхода: 0 — все требования выполнены, 1 — есть нарушения, 2 — окружение не готово.
Подробности и устройство — в README.md рядом.
"""

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import checks
import fixtures
import jsonc
import sandbox
from client import Api
from mock_llm import MockLLM
from report import Reporter

COMPOSE = sandbox.IMAGE_DIR.parents[1] / "compose.yaml"
PROJECT_CONFIG_FLAG = "OPENCODE_DISABLE_PROJECT_CONFIG"


@dataclass(frozen=True)
class Profile:
    name: str
    title: str
    config: Callable[[dict[str, Any]], dict[str, Any]]
    env: Callable[[dict[str, str]], dict[str, str]]
    run: Callable[[checks.Ctx, Reporter], None]


def _same_config(config: dict[str, Any]) -> dict[str, Any]:
    return config


def _same_env(env: dict[str, str]) -> dict[str, str]:
    return env


def _without_flag(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k != PROJECT_CONFIG_FLAG}


def _without_integration_policies(config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    kept = [p for p in result["policies"] if p["action"] != "integration.use"]
    result["policies"] = kept
    return result


PROFILES = [
    Profile(
        "main",
        "образ как есть: флаг, права и политики включены",
        _same_config,
        _same_env,
        checks.main_checks,
    ),
    Profile(
        "policy-only",
        "флаг отключён, остались права и политики: что закрывают политики и зачем нужен флаг",
        _same_config,
        _without_flag,
        checks.policy_only_checks,
    ),
    Profile(
        "control",
        "контроль: нет ни флага, ни политик на плагины и MCP, атака обязана сработать",
        _without_integration_policies,
        _without_flag,
        checks.control_checks,
    ),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--profile",
        action="append",
        choices=[p.name for p in PROFILES] + ["static"],
        help="какие профили выполнить (можно повторять), по умолчанию все",
    )
    parser.add_argument(
        "--only", help="выполнять проверки, в id которых есть эта подстрока"
    )
    parser.add_argument(
        "--keep", action="store_true", help="не удалять временный каталог"
    )
    parser.add_argument(
        "--any-version",
        action="store_true",
        help="не требовать, чтобы нативный opencode был той же версии, что в Dockerfile",
    )
    parser.add_argument("--config", type=Path, default=sandbox.SYSTEM_CONFIG)
    parser.add_argument("--dockerfile", type=Path, default=sandbox.DOCKERFILE)
    parser.add_argument(
        "--binary", type=Path, help="нативный opencode (или OPENCODE_BIN)"
    )
    return parser.parse_args()


def find_binary(explicit: Path | None) -> Path | None:
    candidate = explicit or os.environ.get("OPENCODE_BIN") or shutil.which("opencode")
    return Path(candidate).resolve() if candidate else None


def environment_problem(binary: Path | None) -> str | None:
    if sys.platform != "linux":
        return "нужен Linux: песочница строится на bwrap"
    if not shutil.which("bwrap"):
        return "не найден bwrap (пакет bubblewrap)"
    if not shutil.which("git"):
        return "не найден git: он нужен для тестовых репозиториев и самого opencode"
    if binary is None or not binary.is_file():
        return "не найден нативный opencode: укажите --binary или OPENCODE_BIN"
    return None


def run_profile(
    profile: Profile,
    *,
    rep: Reporter,
    scratch: Path,
    binary: Path,
    spec: sandbox.ImageSpec,
    config: dict[str, Any],
    mock: MockLLM,
) -> None:
    rep.section(f"Профиль {profile.name}: {profile.title}")
    root = scratch / profile.name
    root.mkdir()
    profile_config = profile.config(config)
    system_config = root / "system.jsonc"
    system_config.write_text(json.dumps(profile_config, ensure_ascii=False, indent=2))
    server = sandbox.Server(
        root=root,
        binary=binary,
        system_config=system_config,
        image_env=profile.env(spec.env),
        mock_url=mock.base_url,
    )
    fixtures.make_repos(server.repos)
    try:
        server.start()
    except RuntimeError as error:
        rep.failure(f"{profile.name}.start", "сервер opencode запускается", str(error))
        return
    try:
        ctx = checks.Ctx(
            api=Api(server.url, server.password),
            mock=mock,
            server=server,
            config=profile_config,
            pinned_version=spec.version,
            scratch=scratch,
        )
        profile.run(ctx, rep)
    finally:
        server.stop()


def main() -> int:
    args = parse_args()
    binary = find_binary(args.binary)
    problem = environment_problem(binary)
    if problem or binary is None:
        print(f"Окружение не готово: {problem}", file=sys.stderr)
        return 2
    spec = sandbox.parse_dockerfile(args.dockerfile.read_text(encoding="utf-8"))
    config = jsonc.loads(args.config.read_text(encoding="utf-8"))
    scratch = Path(tempfile.mkdtemp(prefix="opencode-verify-"))
    try:
        native = sandbox.opencode_version(binary, scratch)
        if native != spec.version and not args.any_version:
            print(
                f"Нативный opencode {native or '?'} не совпадает с версией образа {spec.version}"
                " (Dockerfile). Проверка чужой версии ничего не доказывает. Укажите нужный"
                " бинарник через --binary или добавьте --any-version.",
                file=sys.stderr,
            )
            return 2
        print(f"opencode {native}, образ {spec.version}, временный каталог {scratch}")
        selected = args.profile or [p.name for p in PROFILES] + ["static"]
        rep = Reporter(only=args.only)
        if "static" in selected:
            checks.static_checks(rep, spec.version, spec.env, COMPOSE)
        mock = MockLLM([fixtures.ROOT_MARKER, fixtures.PROJECT_MARKER])
        mock.start()
        try:
            for profile in PROFILES:
                if profile.name in selected:
                    run_profile(
                        profile,
                        rep=rep,
                        scratch=scratch,
                        binary=binary,
                        spec=spec,
                        config=config,
                        mock=mock,
                    )
        finally:
            mock.stop()
        print(f"\n{rep.summary()}")
        return 1 if rep.failed else 0
    finally:
        if args.keep:
            print(f"Временный каталог сохранён: {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
