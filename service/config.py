"""Настройки HTTP-сервиса конвейера.

Всё читается из окружения (или из backend/.env — его разбирает самописный
load_dotenv из hf_api_bench). Значения по умолчанию рассчитаны на локальный
запуск рядом с фронтендом: сервис на 8000, Next.js на 8080.
"""
from __future__ import annotations

import os
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent
BACKEND_ROOT = SERVICE_ROOT.parent
ENV_FILE = Path(os.environ.get("PTO_ENV_FILE") or (BACKEND_ROOT / ".env"))


def _load_env_file(path: Path = ENV_FILE) -> None:
    """Подтягивает backend/.env в окружение.

    Один и тот же файл читают локальный запуск и docker-compose (там он
    подключается через env_file), поэтому настройки не расходятся между
    способами запуска. Уже заданные переменные окружения не трогаем — так же
    ведёт себя docker: значение из environment перекрывает env_file.
    """
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env_file()


def _env_str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def _env_dir(name: str, default: Path) -> Path:
    raw = _env_str(name)
    return Path(raw).expanduser().resolve() if raw else default


# --- пути -------------------------------------------------------------------
# Состояние очереди. Переживает перезапуск сервиса.
STATE_DIR = _env_dir("PTO_STATE_DIR", SERVICE_ROOT / "state")
JOBS_PATH = STATE_DIR / "jobs.json"

# Куда конвейер кладёт прогоны. Та же папка, что у CLI, — прогон, запущенный
# через сервис, остаётся совместим с compare_to_etalon.py и build_ios2_md.py.
RUNS_DIR = _env_dir("PTO_RUNS_DIR", BACKEND_ROOT / "hf_runs")

# Куда сохранять PDF, пришедшие по multipart. Если сервис стоит рядом с
# фронтом, сюда можно указать его uploads/ и не копировать файлы дважды.
UPLOADS_DIR = _env_dir("PTO_UPLOADS_DIR", STATE_DIR / "uploads")

# Белый список каталогов, из которых разрешено брать PDF по пути (POST /jobs
# с полем path). Пустой список = разрешены только UPLOADS_DIR и её потомки.
_extra_roots = _env_str("PTO_ALLOWED_PDF_ROOTS", "") or ""
ALLOWED_PDF_ROOTS = [UPLOADS_DIR] + [
    Path(part.strip()).expanduser().resolve()
    for part in _extra_roots.split(os.pathsep)
    if part.strip()
]

# --- режим работы -----------------------------------------------------------
# real — настоящий VLM через HF Inference Providers (деньги и часы).
# mock — быстрая имитация из текстового слоя PDF: нужна, чтобы проверять
#        склейку с фронтом (очередь, прогресс, перезагрузка страницы), не
#        тратя ~2 минуты и токены на каждый лист.
MODE = (_env_str("PTO_PIPELINE_MODE", "real") or "real").lower()
MOCK_PAGE_SECONDS = _env_float("PTO_MOCK_PAGE_SECONDS", 1.5)

# --- профиль прогона --------------------------------------------------------
MODEL = _env_str("PTO_MODEL", "qwen3vl-32b")
PROVIDER = _env_str("PTO_PROVIDER")  # None → resolve_provider подставит preferred

SHEET_AWARE = _env_bool("PTO_SHEET_AWARE", True)
TWO_PASS = _env_bool("PTO_TWO_PASS", True)
LAYER_AWARE = _env_bool("PTO_LAYER_AWARE", True)
HIGH_DPI = _env_bool("PTO_HIGH_DPI", False)
STAMP_CROP = _env_bool("PTO_STAMP_CROP", False)
ZONE_CROP = _env_bool("PTO_ZONE_CROP", False)
RUNS = _env_int("PTO_RUNS", 1)
RETRIES = _env_int("PTO_RETRIES", 3)
RETRY_DELAY = _env_float("PTO_RETRY_DELAY", 2.0)

# Тест-сетовые подсказки по номеру страницы. Для произвольных PDF заказчика
# они вредны (подсказывают то, чего на листе нет), поэтому по умолчанию off.
ZONE_HINTS = _env_bool("PTO_ZONE_HINTS", False)

# Сколько листов считать одновременно. Конвейер синхронный, поэтому
# параллелизм даёт линейное ускорение, но упирается в лимиты провайдера.
PAGE_CONCURRENCY = max(1, _env_int("PTO_PAGE_CONCURRENCY", 1))

# --- сеть -------------------------------------------------------------------
HOST = _env_str("PTO_SERVICE_HOST", "127.0.0.1")
PORT = _env_int("PTO_SERVICE_PORT", 8000)
# Фронт ходит сюда из серверных маршрутов Next.js, но CORS нужен, если кто-то
# захочет дёргать сервис из браузера напрямую.
CORS_ORIGINS = [
    part.strip()
    for part in (_env_str("PTO_CORS_ORIGINS", "*") or "*").split(",")
    if part.strip()
]

MAX_UPLOAD_BYTES = _env_int("PTO_MAX_UPLOAD_MB", 400) * 1024 * 1024


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


def profile_dict() -> dict:
    """Профиль прогона — уходит в ответ /health и в meta.json прогона."""
    return {
        "mode": MODE,
        "model": MODEL,
        "provider": PROVIDER,
        "sheetAware": SHEET_AWARE,
        "twoPass": TWO_PASS,
        "layerAware": LAYER_AWARE,
        "highDpi": HIGH_DPI,
        "stampCrop": STAMP_CROP,
        "zoneCrop": ZONE_CROP,
        "zoneHints": ZONE_HINTS,
        "runs": RUNS,
        "retries": RETRIES,
        "pageConcurrency": PAGE_CONCURRENCY,
    }
