"""Скриптовые тесты, написанные до pytest: сведение источников, слияние
листов, выбор режима. Запускаются как есть — отдельным процессом."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "args",
    [
        ["test_bundle.py"],
        ["test_fuse.py"],
        ["-m", "service.test_resolve_mode"],
    ],
)
def test_legacy_script(args: list[str]) -> None:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
