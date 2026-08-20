"""Самопроверка развёрнутого сервиса: одна команда вместо ручного тыканья.

    python -m service.smoke_test                          # локальный сервис
    python -m service.smoke_test --url http://сервер:8000 --token СЕКРЕТ
    python -m service.smoke_test --pdf "новый.pdf" --pages 1-3

Гоняет полный цикл по настоящему HTTP: ставит документ в очередь, ждёт листы,
читает их и убирает за собой. Проверяет ровно то, что ломалось на практике:

  - шапка листа содержит «**Файл:**» — без неё storage.ts во фронтенде
    перегенерирует markdown заглушкой и молча затирает результат прогона;
  - листов посчитано столько же, сколько заказано, и ни один не упал;
  - маршрут /raw отдаёт сырой вывод конвейера;
  - собранный /markdown не пустой.

Запускать в режиме mock: лист считается ~1,5 секунды, модель не вызывается.
В режиме real скрипт отработает тоже, но это настоящие деньги и часы, поэтому
он об этом предупреждает и требует --yes-real.

Зависимостей нет, кроме тех, что уже нужны бэкенду.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# Прокси из окружения ломает обращение к 127.0.0.1: запрос уходит наружу и
# возвращается 502. Для самопроверки прокси не нужен никогда.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

OK = "  [ ок ] "
FAIL = "  [ХУДО] "


class SmokeError(RuntimeError):
    pass


def _request(url: str, token: str, *, data=None, headers=None, method=None):
    head = dict(headers or {})
    if token:
        head["X-PTO-Token"] = token
    req = urllib.request.Request(url, data=data, headers=head, method=method)
    try:
        with _OPENER.open(req, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except OSError as error:
        raise SmokeError(f"не достучался до {url}: {error}") from error


def get_json(base: str, path: str, token: str) -> dict:
    status, body = _request(base + path, token)
    if status != 200:
        raise SmokeError(f"GET {path} вернул {status}: {body[:200]!r}")
    return json.loads(body.decode("utf-8"))


def get_text(base: str, path: str, token: str) -> str:
    status, body = _request(base + path, token)
    if status != 200:
        raise SmokeError(f"GET {path} вернул {status}: {body[:200]!r}")
    return body.decode("utf-8")


def upload(base: str, token: str, pdf: Path, pages: str, document_id: str) -> dict:
    """Кладёт PDF через multipart — работает и когда сервис не видит наш диск."""
    boundary = uuid.uuid4().hex
    buf = io.BytesIO()

    def part(disposition: str, payload: bytes) -> None:
        buf.write(f"--{boundary}\r\n".encode())
        buf.write(disposition.encode("utf-8"))
        buf.write(b"\r\n\r\n")
        buf.write(payload)
        buf.write(b"\r\n")

    part(
        f'Content-Disposition: form-data; name="file"; filename="{pdf.name}"\r\n'
        f"Content-Type: application/pdf",
        pdf.read_bytes(),
    )
    part('Content-Disposition: form-data; name="originalName"',
         pdf.name.encode("utf-8"))
    part('Content-Disposition: form-data; name="documentId"',
         document_id.encode("utf-8"))
    part('Content-Disposition: form-data; name="pages"', pages.encode("utf-8"))
    buf.write(f"--{boundary}--\r\n".encode())

    status, body = _request(
        base + "/jobs",
        token,
        data=buf.getvalue(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    if status != 201:
        raise SmokeError(f"POST /jobs вернул {status}: {body[:300]!r}")
    return json.loads(body.decode("utf-8"))


def make_test_pdf(target: Path) -> Path:
    """Свой одностраничный PDF — чтобы проверка не зависела от чужих файлов."""
    import fitz  # уже нужен конвейеру

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 96), "PTO smoke test", fontsize=22)
    page.insert_text((72, 130), "Проверка развёрнутого сервиса", fontsize=13)
    doc.save(target)
    doc.close()
    return target


def wait_for_job(base: str, token: str, job_id: str, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last_line = ""
    while time.monotonic() < deadline:
        job = get_json(base, f"/jobs/{job_id}", token)
        line = (
            f"    {job['status']}: {job['pagesDoneCount']}/{job['pagesTotal']}"
            f" ({job['percent']}%)"
        )
        if job["processingPage"]:
            line += f", считается лист {job['processingPage']}"
        if line != last_line:
            print(line, flush=True)
            last_line = line
        if job["status"] in {"done", "error", "canceled"}:
            return job
        time.sleep(2)
    raise SmokeError(f"задача не закончилась за {timeout:.0f} с")


def run(args) -> int:
    base = args.url.rstrip("/")
    token = args.token or ""
    checks: list[tuple[bool, str]] = []

    def check(passed: bool, message: str) -> None:
        checks.append((passed, message))
        print((OK if passed else FAIL) + message, flush=True)

    print(f"\nСервис: {base}\n")

    # 1. Живость и режим
    health = get_json(base, "/health", token)
    check(health.get("ok") is True, "сервис отвечает на /health")
    mode = health.get("mode")
    profile = health.get("profile", {})
    print(f"    режим={mode} модель={profile.get('model')} "
          f"потоков={profile.get('pageConcurrency')}")
    if mode == "real" and not args.yes_real:
        print(
            "\n  Сервис поднят в режиме real: проверка потратит деньги и время\n"
            "  (лист чертежа считается до 16 минут). Если это осознанно —\n"
            "  повторите с --yes-real. Для проверки развёртывания поднимите\n"
            "  сервис профилем mock.\n"
        )
        return 2

    # 2. Токен: если сервис закрыт, без заголовка он обязан отвечать 401
    if token:
        status, _ = _request(base + "/jobs", "")
        check(status == 401, "без токена сервис отвечает 401")

    # 3. Полный цикл по документу
    pdf = Path(args.pdf) if args.pdf else make_test_pdf(
        Path(args.workdir or ".") / "pto_smoke.pdf"
    )
    if not pdf.exists():
        raise SmokeError(f"нет файла {pdf}")
    document_id = f"smoke-{uuid.uuid4().hex[:8]}"
    print(f"    файл: {pdf.name}, листы: {args.pages}")

    job = upload(base, token, pdf, args.pages, document_id)
    check(bool(job.get("id")), "документ принят в очередь")

    # Задача обязана находиться по documentId — на этом держится подхват
    # прогона фронтендом после перезапуска Next.
    found = get_json(base, f"/jobs?documentId={document_id}", token)["jobs"]
    check(any(item["id"] == job["id"] for item in found),
          "задача находится по documentId")

    job = wait_for_job(base, token, job["id"], args.timeout)
    check(job["status"] == "done", f"прогон завершился (статус {job['status']})")
    check(not job["pageErrors"],
          f"ни один лист не упал (ошибок: {len(job['pageErrors'])})")
    check(job["pagesDoneCount"] == job["pagesTotal"],
          f"посчитаны все листы ({job['pagesDoneCount']}/{job['pagesTotal']})")

    # 4. Содержимое листов
    if job["pagesDone"]:
        number = job["pagesDone"][0]
        page = get_json(base, f"/jobs/{job['id']}/pages/{number}", token)
        check("**Файл:**" in page["markdown"],
              "в шапке листа есть «**Файл:**» (иначе фронт затрёт markdown)")
        check(page.get("kind") in {"drawing", "text", "table", "mixed"},
              f"тип листа распознан ({page.get('kind')})")
        check("[Error" not in page["markdown"],
              "в листе нет строк с ошибками конвейера")

        raw = get_text(base, f"/jobs/{job['id']}/pages/{number}/raw", token)
        check(bool(raw.strip()), "/raw отдаёт сырой вывод конвейера")

        whole = get_text(base, f"/jobs/{job['id']}/markdown", token)
        check(len(whole) > 50, "документ собирается целиком в /markdown")

    # 5. Убираем за собой
    status, _ = _request(base + f"/jobs/{job['id']}", token, method="DELETE")
    check(status in (200, 204), "задача удаляется из очереди")
    if not args.pdf and pdf.exists():
        pdf.unlink()

    failed = [message for passed, message in checks if not passed]
    print()
    if failed:
        print(f"ПРОВАЛЕНО {len(failed)} из {len(checks)}:")
        for message in failed:
            print("  - " + message)
        return 1
    print(f"Все проверки пройдены ({len(checks)}).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Самопроверка развёрнутого сервиса ПТО",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000",
                        help="адрес сервиса (по умолчанию http://127.0.0.1:8000)")
    parser.add_argument("--token", default="",
                        help="значение PTO_API_TOKEN, если сервис закрыт")
    parser.add_argument("--pdf", default="",
                        help="свой PDF; по умолчанию скрипт делает одностраничный")
    parser.add_argument("--pages", default="1",
                        help="какие листы считать: 1, 1-3, all")
    parser.add_argument("--timeout", type=float, default=600,
                        help="сколько ждать окончания прогона, сек")
    parser.add_argument("--workdir", default="",
                        help="куда положить временный PDF")
    parser.add_argument("--yes-real", action="store_true",
                        help="разрешить проверку на сервисе в режиме real")
    args = parser.parse_args()

    try:
        return run(args)
    except SmokeError as error:
        print(f"\n{FAIL}{error}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
