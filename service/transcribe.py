"""Расшифровка голосовых замечаний (Whisper).

Зачем. Во вкладке замечаний инженер надиктовывает, что не так с листом, вместо
того чтобы набирать текст руками на стройке. Фронтенд пишет короткий webm и
шлёт его сюда; наружу отдаётся одна строка.

Как это устроено. Сам Whisper здесь не крутится — бэкенд к нему только ходит.
Способ выбирается по окружению, потому что разворачивать его будут на сервере
и не факт, что тем способом, который мы угадаем:

  PTO_WHISPER_URL   HTTP-эндпоинт, совместимый с OpenAI Audio API
                    (`/v1/audio/transcriptions`). Так отдают наружу и
                    whisper.cpp server, и faster-whisper-server, и сам OpenAI —
                    поэтому это основной путь.
  (пакет в системе)  если URL не задан, но установлен `faster_whisper` —
                    считаем прямо в процессе. Медленно на CPU, но работает без
                    отдельной службы.
  mock              заглушка для отладки склейки с фронтом: возвращает
                    помеченный текст, модель не вызывается.

Явно НЕ делаем: не пытаемся угадать язык (проектная документация русская —
язык задаётся и по умолчанию `ru`) и не храним аудио. Файл живёт в памяти
столько, сколько идёт запрос: это голос сотрудника, ему незачем оседать на
диске сервера.
"""
from __future__ import annotations

import io

from service import config


class TranscribeError(RuntimeError):
    """Расшифровать не удалось. Текст сообщения уходит во фронтенд как есть."""


# Больше этого не принимаем: замечание — это фраза, а не совещание. Заодно
# защита от того, что вкладка запишет часовой файл и положит сервис.
MAX_AUDIO_BYTES = 25 * 1024 * 1024

# Сколько ждём Whisper. Минута на короткую фразу — с большим запасом даже для
# CPU-сборки; дольше ждать бессмысленно, инженер уже ушёл со страницы.
HTTP_TIMEOUT = 60.0


def _via_http(data: bytes, filename: str) -> str:
    """Whisper за HTTP, протокол OpenAI Audio API."""
    import httpx

    headers = {}
    if config.WHISPER_TOKEN:
        headers["Authorization"] = f"Bearer {config.WHISPER_TOKEN}"
    files = {"file": (filename or "note.webm", io.BytesIO(data), "audio/webm")}
    payload = {"model": config.WHISPER_MODEL, "language": config.WHISPER_LANGUAGE}
    try:
        # trust_env=False: Whisper — наша же служба, адресуемая напрямую
        # (localhost или соседний контейнер). Системный прокси на такой адрес
        # ходить не должен: на машине с прокси в настройках Windows запрос к
        # 127.0.0.1 уходил в прокси и возвращался 502.
        with httpx.Client(timeout=HTTP_TIMEOUT, trust_env=False) as client:
            resp = client.post(
                config.WHISPER_URL, headers=headers, files=files, data=payload
            )
    except Exception as e:
        raise TranscribeError(f"Whisper недоступен: {e}") from e
    if resp.status_code >= 400:
        raise TranscribeError(
            f"Whisper ответил {resp.status_code}: {resp.text[:300]}"
        )
    try:
        body = resp.json()
    except ValueError:
        # whisper.cpp в режиме response_format=text отдаёт голую строку
        return resp.text.strip()
    if isinstance(body, dict):
        return str(body.get("text", "")).strip()
    return str(body).strip()


def _via_local(data: bytes, filename: str) -> str:
    """faster-whisper прямо в процессе сервиса."""
    import tempfile
    from pathlib import Path

    from faster_whisper import WhisperModel

    global _LOCAL_MODEL
    if _LOCAL_MODEL is None:
        _LOCAL_MODEL = WhisperModel(config.WHISPER_MODEL, device="auto")
    suffix = Path(filename or "note.webm").suffix or ".webm"
    # faster-whisper читает файл, а не поток: кладём во временный и удаляем.
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        segments, _info = _LOCAL_MODEL.transcribe(
            str(tmp_path), language=config.WHISPER_LANGUAGE or None
        )
        return " ".join(s.text.strip() for s in segments).strip()
    finally:
        tmp_path.unlink(missing_ok=True)


_LOCAL_MODEL = None


def _local_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


def describe() -> dict:
    """Что сейчас настроено — для /health, чтобы не гадать по логам."""
    return {
        "mode": resolve_mode(),
        "url": config.WHISPER_URL or None,
        "model": config.WHISPER_MODEL,
        "language": config.WHISPER_LANGUAGE,
    }


def resolve_mode() -> str:
    """Каким способом будем расшифровывать. `off` — не настроено ничем."""
    mode = (config.WHISPER_MODE or "auto").lower()
    if mode != "auto":
        return mode
    if config.WHISPER_URL:
        return "http"
    if _local_available():
        return "local"
    if config.MODE == "mock":
        return "mock"
    return "off"


def transcribe(data: bytes, filename: str = "note.webm") -> str:
    """Аудио → строка. Бросает TranscribeError, если расшифровать нечем."""
    if not data:
        raise TranscribeError("Пустой аудиофайл")
    if len(data) > MAX_AUDIO_BYTES:
        raise TranscribeError(
            f"Запись больше {MAX_AUDIO_BYTES // (1024 * 1024)} МБ — "
            "замечание должно быть короткой фразой"
        )
    mode = resolve_mode()
    if mode == "mock":
        return (
            "[MOCK] Расшифровка не выполнялась: сервис запущен без Whisper "
            f"(файл {filename}, {len(data)} байт)."
        )
    if mode == "http":
        return _via_http(data, filename)
    if mode == "local":
        return _via_local(data, filename)
    raise TranscribeError(
        "Расшифровка не настроена: задайте PTO_WHISPER_URL (эндпоинт Whisper, "
        "совместимый с OpenAI Audio API) или установите faster-whisper."
    )
