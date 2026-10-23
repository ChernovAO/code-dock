"""Учёт и вывод результатов проверок."""

from dataclasses import dataclass


@dataclass
class Reporter:
    only: str | None = None
    passed: int = 0
    failed: int = 0
    reproduced: int = 0
    gone: int = 0
    skipped: int = 0

    def matches(self, check_id: str) -> bool:
        return self.only is None or self.only in check_id

    def wants(self, group: str) -> bool:
        """Нужно ли вообще выполнять группу проверок при заданном `--only`."""
        return self.only is None or self.only in group or group in self.only

    def section(self, title: str) -> None:
        print(f"\n== {title} ==", flush=True)

    def requirement(
        self, check_id: str, title: str, ok: bool, detail: str = ""
    ) -> None:
        """Требование: нарушение даёт ненулевой код выхода."""
        if not self.matches(check_id):
            return
        if ok:
            self.passed += 1
            self._line("ок", check_id, title)
        else:
            self.failed += 1
            self._line("ОШИБКА", check_id, title, detail)

    def failure(self, check_id: str, title: str, detail: str = "") -> None:
        """Сбой инфраструктуры: считается всегда, `--only` его не скрывает."""
        self.failed += 1
        self._line("ОШИБКА", check_id, title, detail)

    def limitation(
        self, check_id: str, title: str, reproduced: bool, detail: str = ""
    ) -> None:
        """Известное ограничение из документации: только сообщаем, воспроизводится ли оно."""
        if not self.matches(check_id):
            return
        if reproduced:
            self.reproduced += 1
            self._line("огран.", check_id, title)
        else:
            self.gone += 1
            self._line("ушло", check_id, f"{title}: больше не воспроизводится", detail)

    def skip(self, check_id: str, title: str, reason: str) -> None:
        if self.matches(check_id):
            self.skipped += 1
            self._line("пропуск", check_id, title, reason)

    def summary(self) -> str:
        parts = [f"{self.passed} ок", f"{self.failed} ошибок"]
        limits = f"ограничения: {self.reproduced} воспроизводятся, {self.gone} ушли"
        parts += [limits, f"{self.skipped} пропущено"]
        return "Итого: " + ", ".join(parts)

    def _line(self, status: str, check_id: str, title: str, detail: str = "") -> None:
        text = f"  {status:<8}{check_id:<34}{title}"
        print(f"{text} [{detail}]" if detail else text, flush=True)
