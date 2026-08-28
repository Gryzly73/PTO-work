"""Очередь задач и её хранение на диске.

Одна задача = один PDF. Состояние держим в памяти (его читает polling фронта
раз в 900 мс) и синхронно сбрасываем в jobs.json атомарной заменой файла,
чтобы перезапуск сервиса не терял очередь.

Сам markdown здесь не хранится: страницы лежат файлами в папке прогона.
Это осознанно — на 1000-страничном документе класть тексты в один JSON
значит переписывать десятки мегабайт после каждого листа.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from service import config

# Статусы совпадают с DocumentStatus фронтенда, плюс canceled — его фронт
# показывает как error, но по смыслу это не сбой.
STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_ERROR = "error"
STATUS_CANCELED = "canceled"

ACTIVE_STATUSES = {STATUS_QUEUED, STATUS_PROCESSING}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    originalName: str
    pdfPath: str
    runDir: str
    pageCount: int
    pagesRequested: list[int]
    projectId: str | None = None
    documentId: str | None = None
    pagesDone: list[int] = field(default_factory=list)
    status: str = STATUS_QUEUED
    processingStep: str | None = STATUS_QUEUED
    processingPage: int | None = None
    errorMessage: str | None = None
    pageErrors: dict[str, str] = field(default_factory=dict)
    # Лист посчитан, но с дырами: часть фрагментов модель не прочитала, слоя
    # нет и всё читалось по картинке, и т. п. Отдельно от pageErrors: там
    # листы, которых нет вовсе, здесь — те, которым нельзя верить целиком.
    pageWarnings: dict[str, str] = field(default_factory=dict)
    # Сколько листов задания ушло в модель (нет пригодного слоя или скан) —
    # против бюджета PTO_MAX_MODEL_PAGES.
    modelPages: int = 0
    usage: dict = field(default_factory=dict)
    profile: dict = field(default_factory=dict)
    cancelRequested: bool = False
    createdAt: str = field(default_factory=now_iso)
    startedAt: str | None = None
    finishedAt: str | None = None
    elapsedSec: float | None = None

    @property
    def pages_total(self) -> int:
        return len(self.pagesRequested)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["pagesTotal"] = self.pages_total
        data["pagesDoneCount"] = len(self.pagesDone)
        data["percent"] = (
            round(100 * len(self.pagesDone) / max(1, self.pages_total))
            if self.pages_total
            else 0
        )
        return data


class JobStore:
    """Потокобезопасное хранилище очереди."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or config.JOBS_PATH
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._load()

    # --- диск ---------------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        known = {f.name for f in Job.__dataclass_fields__.values()}
        for item in raw.get("jobs", []):
            try:
                job = Job(**{k: v for k, v in item.items() if k in known})
            except TypeError:
                continue
            self._jobs[job.id] = job
            self._order.append(job.id)

    def _flush_locked(self) -> None:
        payload = {
            "version": 1,
            "savedAt": now_iso(),
            "jobs": [asdict(self._jobs[jid]) for jid in self._order if jid in self._jobs],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self._path)

    # --- операции -----------------------------------------------------------
    def create(self, **kwargs) -> Job:
        with self._lock:
            job = Job(id=str(uuid.uuid4()), **kwargs)
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._flush_locked()
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, project_id: str | None = None) -> list[Job]:
        with self._lock:
            jobs = [self._jobs[jid] for jid in self._order if jid in self._jobs]
        if project_id:
            jobs = [job for job in jobs if job.projectId == project_id]
        return jobs

    def update(self, job_id: str, mutate: Callable[[Job], None]) -> Job | None:
        """Меняет задачу под замком и сразу пишет на диск."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            mutate(job)
            self._flush_locked()
            return job

    def patch(self, job_id: str, **fields) -> Job | None:
        def mutate(job: Job) -> None:
            for key, value in fields.items():
                setattr(job, key, value)

        return self.update(job_id, mutate)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            if job_id not in self._jobs:
                return False
            del self._jobs[job_id]
            self._order = [jid for jid in self._order if jid != job_id]
            self._flush_locked()
            return True

    def next_queued(self, accept: Callable[[Job], bool] | None = None) -> Job | None:
        with self._lock:
            for jid in self._order:
                job = self._jobs.get(jid)
                if not job or job.status != STATUS_QUEUED or job.cancelRequested:
                    continue
                if accept is not None and not accept(job):
                    continue
                return job
            return None

    def claim_next(self, accept: Callable[[Job], bool] | None = None) -> Job | None:
        """Берёт первую подходящую задачу из очереди и сразу помечает её
        взятой — под тем же замком. Воркеров два (модельный и чертёжный), и
        без атомарного захвата оба могли бы подхватить одну задачу между
        «посмотрел» и «пометил»."""
        with self._lock:
            job = self.next_queued(accept)
            if job is None:
                return None
            job.status = STATUS_PROCESSING
            job.startedAt = job.startedAt or now_iso()
            self._flush_locked()
            return job

    def active(self) -> list[Job]:
        return [job for job in self.list() if job.status in ACTIVE_STATUSES]

    def reconcile(self) -> list[str]:
        """После перезапуска сервиса: задачи, помеченные processing, никто уже
        не считает. Возвращаем их в очередь — готовые листы не потеряются,
        воркер подберёт прогон с того места, где он оборвался."""
        revived: list[str] = []
        with self._lock:
            for job in self._jobs.values():
                if job.status == STATUS_PROCESSING:
                    job.status = STATUS_QUEUED
                    job.processingStep = STATUS_QUEUED
                    job.processingPage = None
                    job.cancelRequested = False
                    revived.append(job.id)
            if revived:
                self._flush_locked()
        return revived


def merge_usage(target: dict, extra: Iterable[dict]) -> dict:
    """Складывает usage нескольких вызовов конвейера в один словарь."""
    result = dict(target)
    for item in extra:
        for key, value in (item or {}).items():
            if isinstance(value, (int, float)):
                result[key] = result.get(key, 0) + value
    return result
