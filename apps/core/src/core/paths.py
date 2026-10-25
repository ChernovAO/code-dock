"""Слой путей: всё, что приходит снаружи, превращается в путь внутри репозитория.

Граница — корень репозитория. Путь проверяется дважды: лексически (без `..`
и абсолютных путей) и после разворота symlink: `realpath` обязан остаться внутри
репозитория и не проходить через `.git`. Каталоги `.git` закрыты на любой
глубине: с ними работает только git-слой.

Проверка `resolve` не атомарна: symlink, подложенный между ней и открытием
файла, уводит за границу. Читать и писать файлы нужно через `open_inside`.
"""

import errno
import os
from pathlib import Path, PurePosixPath

GIT_DIR = ".git"


class PathError(ValueError):
    """Путь недопустим: выходит за пределы репозитория или ведёт в `.git`."""


class PathNotFoundError(LookupError):
    """Путь допустим, но объекта нет."""


def repo_root(repos_dir: Path, name: str) -> Path:
    """Корень репозитория `name` в папке репозиториев, с развёрнутыми symlink.

    Имя — любая одна папка, кроме `.`, `..` и `.git`: репозитории клонируют
    вручную, и папка может называться как угодно (`.github`, кириллица).
    """
    if not name or "\0" in name or "/" in name or name in (".", "..") or _is_git(name):
        raise PathError(f"Недопустимое имя репозитория: {name!r}")
    base = repos_dir.resolve()
    root = (base / name).resolve()
    if root.parent != base:
        raise PathError(f"Репозиторий {name!r} ведёт за пределы папки репозиториев")
    if not root.is_dir():
        raise PathNotFoundError(f"Репозиторий {name!r} не найден")
    return root


def resolve(root: Path, relative: str, *, follow_last: bool = True) -> Path:
    """Путь `relative` от корня репозитория `root`.

    `root` должен быть уже развёрнут (`repo_root`). Пустая строка и `.` — сам
    корень. С `follow_last=False` последний компонент не разворачивается: так
    удаляют и переименовывают сам symlink, а не его цель. Существование пути
    не проверяется.
    """
    parts = _parts(relative)
    candidate = root.joinpath(*parts)
    if follow_last or not parts:
        real = candidate.resolve()
    else:
        real = candidate.parent.resolve() / candidate.name
    _check_inside(root, real, relative)
    return real


def open_inside(root: Path, relative: str, flags: int, mode: int = 0o644) -> int:
    """Открывает `relative` от корня `root` без гонки с подменой symlink.

    Путь проходит по компонентам через `dir_fd` с `O_NOFOLLOW`: symlink на
    любом шаге — ошибка, даже если ведёт внутрь репозитория. Так подмена
    каталога между проверкой и открытием не уводит за границу. Возвращает
    дескриптор, закрывает его вызывающий.
    """
    parts = _parts(relative)
    if any(_is_git(part) for part in parts):
        raise PathError(f"Путь ведёт в каталог .git: {relative!r}")
    *dirs, last = parts or ["."]
    nofollow = os.O_NOFOLLOW | os.O_CLOEXEC
    walk = os.O_RDONLY | os.O_DIRECTORY | nofollow
    try:
        fd = os.open(root, walk)
        try:
            for name in dirs:
                child = os.open(name, walk, dir_fd=fd)
                os.close(fd)
                fd = child
            return os.open(last, flags | nofollow, mode, dir_fd=fd)
        finally:
            os.close(fd)
    except FileNotFoundError as e:
        raise PathNotFoundError(f"Путь не найден: {relative!r}") from e
    except OSError as e:
        # Symlink в середине пути даёт ENOTDIR, в конце — ELOOP.
        if e.errno in (errno.ENOTDIR, errno.ELOOP):
            raise PathError(f"Путь проходит через symlink: {relative!r}") from e
        raise


def relative_to_root(root: Path, path: Path) -> str:
    """Путь от корня репозитория для ответов API: `notes/a.md`, корень — `""`."""
    rel = path.relative_to(root).as_posix()
    return "" if rel == "." else rel


def external_symlinks(root: Path) -> list[str]:
    """Symlink внутри репозитория, которые ведут за его пределы или в `.git`.

    Через такие ссылки агент читает и пишет мимо границы. Каталоги `.git`
    не обходятся, ссылки на каталоги не раскрываются. Каталоги, которые не
    удалось прочитать, тоже попадают в список: внутри может быть такая ссылка.
    """
    found: list[str] = []
    pending = [root]
    while pending:
        here = pending.pop()
        try:
            # Тип записи `DirEntry` берёт из readdir, без lstat на каждый файл.
            with os.scandir(here) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_symlink():
                        try:
                            _check_inside(root, path.resolve(), entry.name)
                        except PathError:
                            found.append(relative_to_root(root, path))
                    elif entry.is_dir() and not _is_git(entry.name):
                        pending.append(path)
        except OSError:
            found.append(relative_to_root(root, here))
    return sorted(found)


def _parts(relative: str) -> list[str]:
    if "\0" in relative:
        raise PathError("Путь содержит NUL")
    pure = PurePosixPath(relative)
    if pure.is_absolute():
        raise PathError(f"Путь должен быть относительным: {relative!r}")
    parts = [part for part in pure.parts if part != "."]
    if ".." in parts:
        raise PathError(f"Путь не может содержать '..': {relative!r}")
    return parts


def _check_inside(root: Path, real: Path, shown: str) -> None:
    if real != root and root not in real.parents:
        raise PathError(f"Путь ведёт за пределы репозитория: {shown!r}")
    if any(_is_git(part) for part in real.relative_to(root).parts):
        raise PathError(f"Путь ведёт в каталог .git: {shown!r}")


def _is_git(part: str) -> bool:
    return part.casefold() == GIT_DIR
