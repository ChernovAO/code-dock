"""Репозитории и проекты в папке репозиториев.

Репозиторий — папка первого уровня в папке репозиториев. Проект — любая папка
внутри репозитория (принято 2026-10-26): `core` проекты не хранит, а список
проектов собирает из папок, где есть чаты.
"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from core import paths


class RepoError(RuntimeError):
    """С репозиторием нельзя работать: он не git или в нём опасные symlink."""

    def __init__(self, message: str, symlinks: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.symlinks = symlinks or []


@dataclass(frozen=True)
class Repo:
    name: str
    git: bool
    # Symlink за пределы репозитория и в `.git`: через них агент обходит границу.
    symlinks: list[str]


def list_repos(repos_dir: Path) -> list[Repo]:
    """Все папки первого уровня. Файлы и ссылки за пределы папки пропускаются.

    Поиск symlink обходит каждый репозиторий целиком на каждый вызов.
    """
    found: list[Repo] = []
    with os.scandir(repos_dir) as entries:
        names = sorted(entry.name for entry in entries)
    for name in names:
        try:
            root = paths.repo_root(repos_dir, name)
        except paths.PathError, paths.PathNotFoundError:
            continue
        found.append(Repo(name, is_git(root), paths.external_symlinks(root)))
    return found


def is_git(root: Path) -> bool:
    """В корне есть `.git`: каталог или файл-ссылка (worktree, submodule).

    Сам `.git`-symlink не считается: его цель не проверить.
    """
    try:
        mode = (root / paths.GIT_DIR).lstat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(mode) or stat.S_ISREG(mode)


def open_repo(repos_dir: Path, name: str) -> Path:
    """Корень репозитория, в котором можно работать агенту.

    Отказывает, если папка не git-репозиторий или в ней есть symlink за
    пределы репозитория (принято 2026-10-26: отказывать, а не предупреждать).
    """
    root = paths.repo_root(repos_dir, name)
    if not is_git(root):
        raise RepoError(f"Папка {name!r} не git-репозиторий")
    symlinks = paths.external_symlinks(root)
    if symlinks:
        raise RepoError(
            f"В репозитории {name!r} есть symlink за его пределы или в .git."
            " Удалите их, чтобы работать с агентом",
            symlinks,
        )
    return root


def project_dir(root: Path, relative: str) -> Path:
    """Папка проекта: существует и путь к ней не проходит через symlink."""
    path = paths.resolve(root, relative)
    if not path.is_dir():
        raise paths.PathNotFoundError(f"Папка не найдена: {relative!r}")
    # Проверка по компонентам с O_NOFOLLOW: `resolve` развернул бы symlink
    # внутрь репозитория, а opencode получил бы путь с ним.
    os.close(paths.open_inside(root, relative, os.O_RDONLY | os.O_DIRECTORY))
    return path


def list_folders(root: Path, relative: str) -> list[str]:
    """Вложенные папки, из которых можно выбрать проект. Без `.git` и symlink."""
    fd = paths.open_inside(root, relative, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with os.scandir(fd) as entries:
            return sorted(
                entry.name
                for entry in entries
                if entry.is_dir(follow_symlinks=False)
                and not paths.is_git_name(entry.name)
            )
    finally:
        os.close(fd)


def project_path(root: Path, directory: str) -> str | None:
    """Путь проекта от корня `root` по абсолютной папке чата.

    `None` — папка вне репозитория. Путь берётся лексически, как в `locate`.
    """
    path = Path(directory)
    if not path.is_relative_to(root):
        return None
    return paths.relative_to_root(root, path)


def locate(repos_dir: Path, directory: str) -> tuple[str, str] | None:
    """Репозиторий и путь проекта по абсолютной папке чата.

    `None` — папка вне папки репозиториев. Путь берётся лексически: чаты
    создаёт `core`, и папка в них уже проверена.
    """
    base = repos_dir.resolve()
    path = Path(directory)
    if not path.is_relative_to(base) or path == base:
        return None
    name, *rest = path.relative_to(base).parts
    return name, "/".join(rest)
