"""Песочница: нативный opencode в bwrap с раскладкой контейнера из `apps/opencode`.

Внутри `/repos` (репозитории), `/opencode` (HOME, том `opencode-data`), системный конфиг
в `/etc/opencode`, пользователь с UID 1000 и чистое окружение. Каталог `/tmp/opencode`
доступен только для чтения: в образе его создаёт root при сборке. Сеть не изолируется,
`/etc/resolv.conf` и `rg` (в образе стоит из пакета) пробрасываются с хоста.
"""

import base64
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

IMAGE_DIR = Path(__file__).resolve().parents[1]
DOCKERFILE = IMAGE_DIR / "Dockerfile"
SYSTEM_CONFIG = IMAGE_DIR / "opencode.jsonc"


@dataclass(frozen=True)
class ImageSpec:
    version: str
    env: dict[str, str]


def parse_dockerfile(text: str) -> ImageSpec:
    """Версия из `FROM …/opencode:<версия>` и все переменные `ENV`."""
    instructions: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        instructions.append(pending + line)
        pending = ""
    version = ""
    env: dict[str, str] = {}
    for instruction in instructions:
        keyword, _, rest = instruction.partition(" ")
        if keyword.upper() == "FROM":
            image = rest.split()[0]
            name, _, tag = image.rpartition(":")
            version = tag if name and "/" not in tag else ""
        elif keyword.upper() == "ENV":
            tokens = shlex.split(rest)
            if tokens and "=" not in tokens[0]:
                env[tokens[0]] = " ".join(tokens[1:])
                continue
            for token in tokens:
                key, _, value = token.partition("=")
                env[key] = value
    return ImageSpec(version, env)


def opencode_version(binary: Path, scratch: Path) -> str:
    result = subprocess.run(
        [str(binary), "--version"],
        check=False,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(scratch)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    found = re.search(r"(\d+\.\d+\.\d+\S*)", result.stdout + result.stderr)
    return found.group(1) if found else ""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_ripgrep() -> Path | None:
    """`rg`, который в образе стоит из пакета, а на хосте может лежать в кэше opencode."""
    found = shutil.which("rg")
    if found:
        return Path(found)
    candidate = Path.home() / ".cache/opencode/bin/rg"
    return candidate if candidate.is_file() else None


class Server:
    """Один сервер opencode со своими репозиториями и данными в каталоге `root`."""

    def __init__(
        self,
        *,
        root: Path,
        binary: Path,
        system_config: Path,
        image_env: Mapping[str, str],
        mock_url: str,
    ) -> None:
        self.root = root
        self.repos = root / "repos"
        self.home = root / "home"
        self.password = secrets.token_urlsafe(18)
        self.port = _free_port()
        self._binary = binary
        self._system_config = system_config
        self._image_env = dict(image_env)
        self._mock_url = mock_url
        self._proc: subprocess.Popen[bytes] | None = None
        self._out = root / "server.out"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def log_file(self) -> Path:
        return self.home / ".local/share/opencode/log/opencode.log"

    def start(self, timeout: float = 90.0) -> None:
        self._prepare()
        with self._out.open("wb") as out:
            self._proc = subprocess.Popen(
                self._argv(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(f"opencode завершился при старте:\n{self._tail()}")
            if self._ready():
                return
            time.sleep(0.3)
        raise RuntimeError(f"opencode не ответил за {timeout:.0f} с:\n{self._tail()}")

    def stop(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def log_issues(self) -> list[str]:
        """Строки WARN и ERROR из лога сервера (так opencode сообщает о битом конфиге)."""
        if not self.log_file.exists():
            return []
        lines = self.log_file.read_text(errors="replace").splitlines()
        return [ln[:300] for ln in lines if "level=WARN" in ln or "level=ERROR" in ln]

    def _tail(self) -> str:
        return "\n".join(self._out.read_text(errors="replace").splitlines()[-15:])

    def _ready(self) -> bool:
        token = base64.b64encode(f"opencode:{self.password}".encode()).decode()
        req = urllib.request.Request(
            self.url + "/api/info", headers={"authorization": "Basic " + token}
        )
        try:
            with urllib.request.urlopen(req, timeout=2):
                return True
        except urllib.error.URLError, OSError:
            return False

    def _prepare(self) -> None:
        for path in (self.repos, self.home / ".config/opencode", self.root / "etc"):
            path.mkdir(parents=True, exist_ok=True)
        (self.root / "empty").mkdir(exist_ok=True)
        (self.root / "etc/passwd").write_text(
            "root:x:0:0:root:/root:/bin/sh\napp:x:1000:1000:app:/opencode:/bin/sh\n"
            "nobody:x:65534:65534:nobody:/:/bin/false\n"
        )
        (self.root / "etc/group").write_text(
            "root:x:0:\napp:x:1000:\nnogroup:x:65534:\n"
        )
        provider = {
            "name": "Mock",
            "env": ["MOCK_API_KEY"],
            "package": "@opencode/ai/providers/openai-compatible",
            "settings": {"baseURL": self._mock_url},
            "models": {
                "m1": {
                    "name": "Mock 1",
                    "capabilities": {"tools": True},
                    "limit": {"context": 100000, "output": 8000},
                }
            },
        }
        user_config = {"model": "mock/m1", "providers": {"mock": provider}}
        config_file = self.home / ".config/opencode/opencode.jsonc"
        config_file.write_text(json.dumps(user_config, indent=2))

    def _env(self) -> dict[str, str]:
        return {
            "PATH": "/opt/oc:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            "OPENCODE_SERVER_PASSWORD": self.password,
            "MOCK_API_KEY": "mock",
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            **self._image_env,
        }

    def run(
        self,
        argv: list[str],
        *,
        cwd: str = "/",
        env: Mapping[str, str] | None = None,
        timeout: float = 120.0,
    ) -> subprocess.CompletedProcess[str]:
        """Запускает команду внутри той же песочницы, что и сервер (как `docker compose exec`)."""
        return subprocess.run(
            self._bwrap(argv, cwd=cwd, env=self._env() if env is None else env),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _argv(self) -> list[str]:
        return self._bwrap(
            [
                "/opt/oc/opencode",
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(self.port),
            ]
        )

    def _bwrap(
        self,
        command: list[str],
        *,
        cwd: str = "/",
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        argv = [
            "bwrap",
            "--unshare-user",
            "--uid",
            "1000",
            "--gid",
            "1000",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--die-with-parent",
            "--tmpfs",
            "/",
            "--ro-bind",
            "/usr",
            "/usr",
        ]
        for name in ("bin", "sbin", "lib", "lib32", "lib64"):
            path = Path("/") / name
            if path.is_symlink():
                argv += ["--symlink", os.readlink(path), f"/{name}"]
            elif path.is_dir():
                argv += ["--ro-bind", str(path), f"/{name}"]
        ripgrep = find_ripgrep()
        if ripgrep is not None:
            argv += ["--ro-bind", str(ripgrep), "/opt/oc/rg"]
        argv += [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--tmpfs",
            "/etc",
        ]
        argv += ["--ro-bind", str(self.root / "etc/passwd"), "/etc/passwd"]
        argv += ["--ro-bind", str(self.root / "etc/group"), "/etc/group"]
        for name in (
            "hosts",
            "resolv.conf",
            "nsswitch.conf",
            "ld.so.cache",
            "ssl",
            "pki",
            "localtime",
        ):
            path = Path("/etc") / name
            if path.exists():
                argv += ["--ro-bind", str(path), str(path)]
        argv += [
            "--ro-bind",
            str(self.root / "empty"),
            "/tmp/opencode",
            "--ro-bind",
            str(self._binary),
            "/opt/oc/opencode",
            "--ro-bind",
            str(self._system_config),
            "/etc/opencode/opencode.jsonc",
            "--bind",
            str(self.repos),
            "/repos",
            "--bind",
            str(self.home),
            "/opencode",
            "--chdir",
            cwd,
            "--clearenv",
        ]
        for key, value in (self._env() if env is None else env).items():
            argv += ["--setenv", key, value]
        return argv + command
