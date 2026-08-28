"""Скан: лист без текстового слоя с картинкой на весь лист.

Печать DWG в PDF на проде 28.08 держала очередь часами: 8 страниц по одной
картинке и ноль знаков слоя шли через модель как обычные чертежи. Теперь
такой лист опознаётся паспортом, помечается в надёжности, а при политике
skip не считается вовсе.
"""
from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from service import config
from service.convert import page_to_frontend, passport_for_page, scan_from_passport_md
from service.jobs import JobStore
from service.pipeline import Pipeline
from service.worker import Worker, load_page_json
from sheet_aware import build_passport, passport_markdown


def _scan_pdf(path: Path, *, with_text: bool = False) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 280), 0)
    pix.clear_with(200)
    page.insert_image(fitz.Rect(20, 20, 575, 822), pixmap=pix)
    if with_text:
        page.insert_text((60, 60), "Текст на листе, слой исправен, длинная строка.", fontname="china-s", fontsize=11)
    doc.save(path)
    doc.close()


def test_passport_detects_scan(tmp_path: Path) -> None:
    pdf = tmp_path / "scan.pdf"
    _scan_pdf(pdf)
    with fitz.open(pdf) as doc:
        passport = build_passport(doc[0], 1)
    assert passport.scan
    assert passport.image_share >= 0.6
    assert any(r.startswith("scan:") for r in passport.reasons)
    assert scan_from_passport_md(passport_markdown(passport))


def test_vector_page_without_text_is_no_layer(tmp_path: Path) -> None:
    """Печать DWG в PDF: линии есть, картинки нет, текста нет."""
    pdf = tmp_path / "outlines.pdf"
    doc = fitz.open()
    page = doc.new_page(width=1190, height=842)
    for i in range(20):
        page.draw_line((50, 50 + i * 30), (1100, 60 + i * 30), width=0.5)
    doc.save(pdf)
    doc.close()
    with fitz.open(pdf) as d:
        passport = build_passport(d[0], 1)
    assert passport.no_layer and not passport.scan
    assert passport.needs_model
    md = passport_markdown(passport)
    assert "слой: нет" in md
    page = page_to_frontend(page_number=1, file_name="o.pdf", raw_page_md="", pdf_path=pdf)
    assert any("текстового слоя нет" in w for w in page["warnings"])


def test_image_with_text_layer_is_not_scan(tmp_path: Path) -> None:
    pdf = tmp_path / "photo.pdf"
    _scan_pdf(pdf, with_text=True)
    with fitz.open(pdf) as doc:
        passport = build_passport(doc[0], 1)
    assert not passport.scan
    assert not scan_from_passport_md(passport_markdown(passport))


def test_scan_page_is_marked_in_trust(tmp_path: Path) -> None:
    pdf = tmp_path / "scan.pdf"
    _scan_pdf(pdf)
    assert passport_for_page(pdf, 1).scan
    page = page_to_frontend(page_number=1, file_name="scan.pdf", raw_page_md="", pdf_path=pdf)
    assert any(w.startswith("скан") for w in page["warnings"])
    assert "скан" in page["markdown"]
    assert page["numbers"] is None


def test_worker_skips_scan_when_policy_is_skip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "scan.pdf"
    _scan_pdf(pdf)
    monkeypatch.setattr(config, "SCAN_POLICY", "skip")
    store = JobStore(tmp_path / "jobs.json")
    run_dir = tmp_path / "run"
    job = store.create(
        originalName="scan.pdf", pdfPath=str(pdf), runDir=str(run_dir),
        pageCount=1, pagesRequested=[1],
    )
    worker = Worker(store, Pipeline("mock"))
    worker._run_single_page(job, pdf, run_dir, 1)
    fresh = store.get(job.id)
    assert fresh.pagesDone == [1]
    assert "скан" in fresh.pageWarnings["1"]
    page = load_page_json(fresh, 1)
    assert page is not None
    assert "не обрабатывался" in page["markdown"]
    assert page["trust"]["level"] == "none"


def test_worker_counts_model_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = tmp_path / "scan.pdf"
    _scan_pdf(pdf)
    monkeypatch.setattr(config, "SCAN_POLICY", "model")
    monkeypatch.setattr(config, "MAX_MODEL_PAGES", 1)
    store = JobStore(tmp_path / "jobs.json")
    run_dir = tmp_path / "run"
    job = store.create(
        originalName="scan.pdf", pdfPath=str(pdf), runDir=str(run_dir),
        pageCount=2, pagesRequested=[1, 2],
    )
    pipeline = Pipeline("mock")
    # В mock модель не зовётся и бюджет не тратится; для проверки учёта
    # считаем, что лист нуждается в модели.
    monkeypatch.setattr(pipeline, "page_needs_model", lambda *a, **k: True)
    worker = Worker(store, pipeline)
    worker._run_single_page(job, pdf, run_dir, 1)
    assert store.get(job.id).modelPages == 1
    worker._run_single_page(job, pdf, run_dir, 1)  # второй лист того же файла
    fresh = store.get(job.id)
    assert fresh.modelPages == 1
    assert "бюджет" in fresh.pageErrors.get("1", "")
