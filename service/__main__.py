"""Запуск сервиса: python -m service

Windows-гоча из корневого CLAUDE.md: в конвейере много кириллицы, консоль по
умолчанию cp1251 и падает с UnicodeEncodeError. Ставим UTF-8 явно, чтобы
сервис не зависел от того, как его запустили.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime


class _Tee:
    """Пишет и в консоль, и в файл.

    Прогон идёт часами, а окно с логом легко закрыть или потерять. Разбираться
    потом, почему лист не вышел, нужно по файлу, а не по памяти.
    """

    def __init__(self, stream, sink) -> None:
        self._stream = stream
        self._sink = sink

    def write(self, data: str) -> int:
        try:
            self._stream.write(data)
        except Exception:
            pass
        try:
            self._sink.write(data)
            self._sink.flush()
        except Exception:
            pass
        return len(data)

    def flush(self) -> None:
        for target in (self._stream, self._sink):
            try:
                target.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return getattr(self._stream, "isatty", lambda: False)()


def main() -> int:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    import uvicorn

    from service import config

    config.ensure_dirs()
    log_path = config.STATE_DIR / "service.log"
    log_file = open(log_path, "a", encoding="utf-8", errors="replace")
    log_file.write(f"\n{'=' * 70}\nзапуск {datetime.now().isoformat(timespec='seconds')}\n")
    sys.stdout = _Tee(sys.stdout, log_file)
    sys.stderr = _Tee(sys.stderr, log_file)
    print(f"[service] лог пишется в {log_path}", flush=True)

    uvicorn.run(
        "service.app:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
