"""Очередь в две полосы: DWG/DXF не ждут за PDF, задачу берёт один воркер."""
from __future__ import annotations

from pathlib import Path

from service.jobs import STATUS_PROCESSING, STATUS_QUEUED, JobStore
from service.worker import LANE_MODEL, LANE_VECTOR, lane_of


def _store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.json")


def _job(store: JobStore, name: str) -> object:
    return store.create(
        originalName=name, pdfPath=f"/x/{name}", runDir="/runs/x",
        pageCount=1, pagesRequested=[1],
    )


def test_lane_by_suffix(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert lane_of(_job(store, "a.pdf")) == LANE_MODEL
    assert lane_of(_job(store, "b.dwg")) == LANE_VECTOR
    assert lane_of(_job(store, "c.DXF")) == LANE_VECTOR


def test_claim_next_respects_lane_and_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    pdf1 = _job(store, "1.pdf")
    dwg = _job(store, "2.dwg")
    pdf2 = _job(store, "3.pdf")
    vector = store.claim_next(lambda j: lane_of(j) == LANE_VECTOR)
    assert vector is not None and vector.id == dwg.id
    assert vector.status == STATUS_PROCESSING
    model = store.claim_next(lambda j: lane_of(j) == LANE_MODEL)
    assert model is not None and model.id == pdf1.id
    # Второй PDF ждёт: модельная полоса берёт по одной задаче.
    assert store.get(pdf2.id).status == STATUS_QUEUED
    # Захваченное второй раз не отдаётся.
    assert store.claim_next(lambda j: lane_of(j) == LANE_VECTOR) is None


def test_claim_skips_canceled(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job = _job(store, "1.dwg")
    store.patch(job.id, cancelRequested=True)
    assert store.claim_next(lambda j: True) is None


def test_reconcile_returns_processing_to_queue(tmp_path: Path) -> None:
    store = _store(tmp_path)
    job = _job(store, "1.pdf")
    store.claim_next(lambda j: True)
    # Перезапуск сервиса: то же хранилище читается заново.
    again = JobStore(tmp_path / "jobs.json")
    revived = again.reconcile()
    assert revived == [job.id]
    assert again.get(job.id).status == STATUS_QUEUED
    assert "pageWarnings" in again.get(job.id).to_dict()
