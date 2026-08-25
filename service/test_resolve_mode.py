"""Проверка выбора mock/real без поднятия сервиса.

Запуск из корня PTO-work:
  python -m service.test_resolve_mode
"""
from __future__ import annotations

import os

from service.config import resolve_pipeline_mode

KEYS = (
    "PTO_PIPELINE_MODE",
    "USE_MOCK_PROCESSOR",
    "PTO_ENV",
    "APP_ENV",
    "NODE_ENV",
)


def _with_env(env: dict[str, str | None]):
    saved = {k: os.environ.get(k) for k in KEYS}
    try:
        for key in KEYS:
            os.environ.pop(key, None)
        for key, value in env.items():
            if value is not None:
                os.environ[key] = value
        return resolve_pipeline_mode()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> int:
    cases: list[tuple[str, dict[str, str | None], str, str]] = [
        (
            "явный mock",
            {"PTO_PIPELINE_MODE": "mock"},
            "mock",
            "PTO_PIPELINE_MODE",
        ),
        (
            "явный real важнее USE_MOCK",
            {"PTO_PIPELINE_MODE": "real", "USE_MOCK_PROCESSOR": "1"},
            "real",
            "PTO_PIPELINE_MODE",
        ),
        (
            "USE_MOCK_PROCESSOR=1",
            {"USE_MOCK_PROCESSOR": "1", "PTO_ENV": "production"},
            "mock",
            "USE_MOCK_PROCESSOR",
        ),
        (
            "USE_MOCK_PROCESSOR=0",
            {"USE_MOCK_PROCESSOR": "0"},
            "real",
            "USE_MOCK_PROCESSOR",
        ),
        (
            "prod через PTO_ENV",
            {"PTO_ENV": "production"},
            "real",
            "env:production",
        ),
        (
            "localhost по умолчанию",
            {"NODE_ENV": "development"},
            "mock",
            "default-local",
        ),
    ]

    failed = 0
    for title, env, want_mode, want_src in cases:
        mode, source = _with_env(env)
        ok = mode == want_mode and source == want_src
        mark = "OK" if ok else "FAIL"
        print(f"[{mark}] {title}: mode={mode} source={source}")
        if not ok:
            print(f"       ожидалось mode={want_mode} source={want_src}")
            failed += 1

    if failed:
        print(f"\nПровалено: {failed}")
        return 1
    print("\nВсе проверки прошли.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
