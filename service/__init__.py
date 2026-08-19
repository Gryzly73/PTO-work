"""HTTP-сервис поверх конвейера PDF → Markdown.

Модули конвейера (hf_api_bench, sheet_aware, build_ios2_md) лежат уровнем
выше, поэтому корень бэкенда добавляется в sys.path — сервис можно запускать
из любой рабочей папки.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
