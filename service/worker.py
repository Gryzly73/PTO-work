"""Фоновый воркер: разбирает очередь и считает документы лист за листом.

Воркера два, у каждого своя полоса очереди:

- модельный (`lane="model"`) — PDF. Одна задача за раз: конвейер синхронный
  и упирается в лимиты провайдера. Внутри задачи листы можно считать
  параллельно (PTO_PAGE_CONCURRENCY), но по умолчанию тоже по одному;
- чертёжный (`lane="vector"`) — DWG и DXF. Модель им не нужна, лист читается
  из данных за секунды, поэтому ждать за PDF без текстового слоя (2–15 минут
  на лист) им незачем: на проде DWG на 7 листов считался 3,8 с, а в очереди
  стоял минуты.

Полосы не пересекаются: задачу берёт только воркер её полосы, и захват идёт
под замком хранилища (`JobStore.claim_next`).

Дисциплина ошибок повторяет CLI: keep-going. Упавший лист попадает в
pageErrors и не роняет весь документ — на 1000 страниц потерять всё из-за
одного 413-го было бы дорого.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from service import config, convert
from service.jobs import (
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PROCESSING,
    Job,
    JobStore,
    merge_usage,
    now_iso,
)
from service.pipeline import Pipeline, is_vector, page_file, rebuild_out_md

# Полосы очереди: какие задачи берёт воркер.
LANE_MODEL = "model"    # PDF — через модель, по одной задаче
LANE_VECTOR = "vector"  # DWG/DXF — без модели, отдельным потоком
LANES = (LANE_MODEL, LANE_VECTOR)


def lane_of(job: Job) -> str:
    return LANE_VECTOR if is_vector(Path(job.pdfPath)) else LANE_MODEL

# Шаг, который показывает фронт (ProcessingStep в types.ts). Текстовые листы
# идут как «Текст и таблицы», чертежи и схемы — как «Описание чертежа».
STEP_BY_KIND = {
    "drawing": "drawings",
    "mixed": "drawings",
    "table": "text",
    "text": "text",
}


def page_json_path(run_dir: Path, page_number: int) -> Path:
    return run_dir / "frontend" / f"page_{page_number:04d}.json"


def store_page_json(job: Job, page_number: int, raw_md: str) -> dict:
    """Конвертирует лист в формат фронта и кладёт рядом с прогоном."""
    page = convert.page_to_frontend(
        page_number=page_number,
        file_name=job.originalName,
        raw_page_md=raw_md,
        pdf_path=Path(job.pdfPath),
    )
    target = page_json_path(Path(job.runDir), page_number)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
    return page


def load_page_json(job: Job, page_number: int) -> dict | None:
    """Готовый лист: из кэша, иначе из page_NNNN.md прогона."""
    cached = page_json_path(Path(job.runDir), page_number)
    if cached.exists():
        try:
            page = json.loads(cached.read_text(encoding="utf-8"))
            # Кэш, собранный прежней версией конвертера, пересобираем: иначе
            # на уже посчитанных документах остался бы старый состав страницы.
            if page.get("schema") == convert.PAGE_SCHEMA:
                return page
        except (OSError, json.JSONDecodeError):
            pass
    raw = page_file(Path(job.runDir), page_number)
    if not raw.exists():
        return None
    return store_page_json(job, page_number, raw.read_text(encoding="utf-8"))


class Worker(threading.Thread):
    def __init__(
        self, store: JobStore, pipeline: Pipeline, lane: str = LANE_MODEL
    ) -> None:
        if lane not in LANES:
            raise ValueError(f"Неизвестная полоса воркера: {lane!r}")
        super().__init__(name=f"pto-worker-{lane}", daemon=True)
        self._store = store
        self._pipeline = pipeline
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.lane = lane
        self.current_job_id: str | None = None

    def _accepts(self, job: Job) -> bool:
        return lane_of(job) == self.lane

    # --- управление ---------------------------------------------------------
    def wake(self) -> None:
        self._wake.set()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            job = self._store.claim_next(self._accepts)
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            try:
                self._run_job(job)
            except Exception:
                message = traceback.format_exc(limit=3)
                print(
                    f"[worker:{self.lane}] задача {job.id} упала:\n{message}",
                    flush=True,
                )
                self._store.patch(
                    job.id,
                    status=STATUS_ERROR,
                    processingStep=None,
                    processingPage=None,
                    errorMessage=message.strip().splitlines()[-1],
                    finishedAt=now_iso(),
                )
            finally:
                self.current_job_id = None

    # --- обработка одной задачи --------------------------------------------
    def _run_job(self, job: Job) -> None:
        self.current_job_id = job.id
        started = time.time()
        self._store.patch(
            job.id,
            status=STATUS_PROCESSING,
            processingStep="text",
            errorMessage=None,
            startedAt=job.startedAt or now_iso(),
        )

        run_dir = Path(job.runDir)
        run_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = Path(job.pdfPath)

        # Возобновление: то, что уже посчитано, не считаем заново.
        done = {n for n in job.pagesRequested if page_file(run_dir, n).exists()}
        if done:
            print(f"[worker] {job.id}: продолжаю, готово листов {len(done)}", flush=True)
            self._store.patch(job.id, pagesDone=sorted(done))
        pending = [n for n in job.pagesRequested if n not in done]

        canceled = False
        if config.PAGE_CONCURRENCY > 1 and len(pending) > 1:
            canceled = self._run_pages_parallel(job, pdf_path, run_dir, pending)
        else:
            for page_number in pending:
                if self._cancel_requested(job.id):
                    canceled = True
                    break
                self._run_single_page(job, pdf_path, run_dir, page_number)

        rebuild_out_md(run_dir)
        fresh = self._store.get(job.id)
        elapsed = round(time.time() - started, 1)

        if canceled or (fresh and fresh.cancelRequested):
            self._store.patch(
                job.id,
                status=STATUS_CANCELED,
                processingStep=None,
                processingPage=None,
                finishedAt=now_iso(),
                elapsedSec=elapsed,
                errorMessage="Прогон остановлен вручную",
            )
            return

        failed_all = bool(fresh and fresh.pageErrors and not fresh.pagesDone)
        self._store.patch(
            job.id,
            status=STATUS_ERROR if failed_all else STATUS_DONE,
            processingStep=None if failed_all else "done",
            processingPage=None,
            finishedAt=now_iso(),
            elapsedSec=elapsed,
            errorMessage=("Ни один лист не удалось обработать" if failed_all else None),
        )
        print(f"[worker] {job.id}: завершено за {elapsed}s", flush=True)

    def _run_pages_parallel(
        self, job: Job, pdf_path: Path, run_dir: Path, pending: list[int]
    ) -> bool:
        remaining = list(pending)
        in_flight: dict = {}
        with ThreadPoolExecutor(max_workers=config.PAGE_CONCURRENCY) as pool:
            while remaining or in_flight:
                if self._cancel_requested(job.id):
                    for future in list(in_flight):
                        future.cancel()
                    for future in list(in_flight):
                        try:
                            future.result()
                        except Exception:
                            pass
                    return True

                while remaining and len(in_flight) < config.PAGE_CONCURRENCY:
                    page_number = remaining.pop(0)
                    future = pool.submit(
                        self._run_single_page, job, pdf_path, run_dir, page_number
                    )
                    in_flight[future] = page_number

                if not in_flight:
                    break

                done, _ = wait(in_flight.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    in_flight.pop(future, None)
                    future.result()

        return self._cancel_requested(job.id)

    def _run_single_page(
        self, job: Job, pdf_path: Path, run_dir: Path, page_number: int
    ) -> None:
        if self._cancel_requested(job.id):
            return
        kind = convert.kind_from_page(pdf_path, page_number)
        self._store.patch(
            job.id,
            processingPage=page_number,
            processingStep=STEP_BY_KIND.get(kind, "text"),
        )
        if self._cancel_requested(job.id):
            return
        try:
            result = self._pipeline.run_page(pdf_path, page_number, run_dir)
        except Exception as error:  # keep-going: лист падает, документ живёт
            message = f"{type(error).__name__}: {error}"
            print(f"[worker] {job.id} лист {page_number}: {message}", flush=True)

            def mark_error(item: Job) -> None:
                item.pageErrors[str(page_number)] = message

            self._store.update(job.id, mark_error)
            return

        if self._cancel_requested(job.id):
            # Лист уже посчитан — сохраняем, но дальше цикл остановится.
            pass

        store_page_json(job, page_number, result["markdown"])

        def mark_done(item: Job) -> None:
            if page_number not in item.pagesDone:
                item.pagesDone = sorted(item.pagesDone + [page_number])
            item.pageErrors.pop(str(page_number), None)
            item.usage = merge_usage(item.usage, [result["usage"]])

        self._store.update(job.id, mark_done)

    def _cancel_requested(self, job_id: str) -> bool:
        job = self._store.get(job_id)
        return bool(job and job.cancelRequested)
