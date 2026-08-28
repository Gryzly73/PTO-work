"""Общее для тестов: корень репозитория в sys.path, пути к фикстурам.

Тесты запускаются из корня `backend/`:

    python -m pytest -q tests

Модель не вызывается нигде: всё, что здесь проверяется, — путь текста и
таблиц, сборка листа, очередь и разбор чертежа — работает на данных.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Сервис читает режим из окружения при импорте config: тестам нужен mock,
# иначе в real он потребует HF_TOKEN.
os.environ.setdefault("PTO_PIPELINE_MODE", "mock")

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def ios2_pdf() -> Path:
    """Страницы 3–20 ИОС2: «Содержание» и «Состав проекта» с таблицами на
    сломанном шрифте (ISOCPEUR) плюс текстовые страницы записки. На этих
    страницах 28.08.2026 нашлись три дефекта пути таблиц."""
    path = FIXTURES / "ios2_p3-20.pdf"
    if not path.exists():
        pytest.skip("нет фикстуры ios2_p3-20.pdf")
    return path
