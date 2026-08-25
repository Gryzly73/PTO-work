"""HTTP-лицо конвейера.

Задача сервиса — дать фронтенду ровно те данные, которые он уже умеет
показывать: статус документа, текущий лист, шаг обработки и готовые страницы
в формате DocumentPage. Всё тяжёлое (markdown листов) лежит файлами в папке
прогона, наружу отдаётся по запросу.

Запуск:
    cd backend
    python -m service            # или: uvicorn service.app:app --port 8000
"""
from __future__ import annotations

import secrets
import shutil
import uuid
from html import escape
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)

from service import config
from service.jobs import (
    ACTIVE_STATUSES,
    STATUS_CANCELED,
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_PROCESSING,
    STATUS_QUEUED,
    JobStore,
    now_iso,
)
from service.pipeline import (
    Pipeline,
    PipelineError,
    document_sheets,
    is_vector,
)
from service.transcribe import TranscribeError, transcribe
from service.transcribe import describe as whisper_describe
from service.worker import Worker, load_page_json

store = JobStore()
pipeline = Pipeline()
worker = Worker(store, pipeline)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    revived = store.reconcile()
    if revived:
        print(
            f"[service] после перезапуска вернул в очередь задач: {len(revived)}",
            flush=True,
        )
    worker.start()
    print(
        f"[service] режим={config.MODE} (источник={config.MODE_SOURCE}) "
        f"модель={config.MODEL} прогоны={config.RUNS_DIR}",
        flush=True,
    )
    yield
    worker.shutdown()


app = FastAPI(title="ПТО: конвейер PDF → Markdown", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Открыты без токена. /health дёргает healthcheck контейнера, которому секрет
# передавать некуда; предполётный OPTIONS браузер шлёт без заголовков.
# /transcribe открыт по требованию фронтенда: страница замечаний шлёт запись
# напрямую из браузера и токен туда не передаёт. Расшифровка ничего не читает
# и ничего не запускает, так что цена открытого маршрута — только чужая
# нагрузка на Whisper.
_OPEN_PATHS = {"/health", "/transcribe"}


@app.middleware("http")
async def require_api_token(request: Request, call_next):
    """Общий секрет на все маршруты, если задан PTO_API_TOKEN.

    По умолчанию токена нет и сервис ведёт себя как раньше — на localhost это
    нормально. На сервере без этого любой, кто дотянется до порта, поставит
    документ в очередь и потратит деньги на провайдера.
    """
    if config.API_TOKEN and request.method != "OPTIONS":
        if request.url.path not in _OPEN_PATHS:
            header = request.headers.get("X-PTO-Token") or ""
            if not header:
                auth = request.headers.get("Authorization") or ""
                if auth.lower().startswith("bearer "):
                    header = auth[7:].strip()
            # compare_digest, а не ==, чтобы время ответа не подсказывало,
            # сколько символов угадано.
            # Сравниваем БАЙТЫ: compare_digest на строках с не-ASCII
            # бросает TypeError, и токен с кириллицей ронял маршрут в 500
            # вместо честного 401.
            if not secrets.compare_digest(
                header.encode("utf-8"), config.API_TOKEN.encode("utf-8")
            ):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Нужен токен: заголовок X-PTO-Token"},
                )
    return await call_next(request)


# --- вспомогательное --------------------------------------------------------
def _job_or_404(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    return job


def _resolve_pdf_path(raw_path: str) -> Path:
    """PDF по пути принимаем только из разрешённых каталогов."""
    candidate = Path(raw_path).expanduser().resolve()
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=400, detail=f"Файл не найден: {candidate}")
    for root in config.ALLOWED_PDF_ROOTS:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    raise HTTPException(
        status_code=403,
        detail=(
            "Путь вне разрешённых каталогов. Добавьте его в PTO_ALLOWED_PDF_ROOTS "
            "или загрузите файл через multipart."
        ),
    )


def _parse_pages(spec: str | None, total: int) -> list[int]:
    if not spec or spec.strip().lower() in {"all", "все", "*"}:
        return list(range(1, total + 1))
    from hf_api_bench import parse_pages

    pages = parse_pages(spec, total)
    if not pages:
        raise HTTPException(status_code=400, detail=f"Не разобрал страницы: {spec}")
    return pages


def _new_run_dir(job_hint: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"{stamp}_{config.MODEL}_svc-{job_hint[:8]}"
    path = config.RUNS_DIR / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _create_job(
    *,
    pdf_path: Path,
    original_name: str,
    project_id: str | None,
    document_id: str | None,
    pages_spec: str | None,
):
    try:
        total = document_sheets(pdf_path)
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Не удалось прочитать PDF: {error}")
    if total == 0:
        raise HTTPException(status_code=400, detail="В PDF нет страниц")

    pages = _parse_pages(pages_spec, total)
    run_dir = _new_run_dir(uuid.uuid4().hex)
    job = store.create(
        originalName=original_name,
        pdfPath=str(pdf_path),
        runDir=str(run_dir),
        pageCount=total,
        pagesRequested=pages,
        projectId=project_id,
        documentId=document_id,
        profile=pipeline.describe(),
    )
    worker.wake()
    return job


# --- маршруты ---------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    """Страница «сервис жив».

    Без неё человек, открывший http://127.0.0.1:8000 в браузере, видит голое
    404 Not Found и решает, что сервис не поднялся.
    """
    jobs = store.list()
    active = [job for job in jobs if job.status in ACTIVE_STATUSES]
    rows = "".join(
        "<tr><td>{name}</td><td>{status}</td><td>{done}/{total}</td>"
        "<td>{page}</td></tr>".format(
            name=escape(job.originalName),
            status=escape(job.status),
            done=len(job.pagesDone),
            total=job.pages_total,
            page=job.processingPage if job.processingPage else "—",
        )
        for job in jobs[-10:][::-1]
    )
    mode_note = (
        "имитация без обращения к модели"
        if config.MODE == "mock"
        else "настоящий прогон VLM, страницы уходят провайдеру HF"
    )
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>ПТО — конвейер</title>
<style>
 body {{ font: 15px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 46rem; }}
 code {{ background: #f4f4f5; padding: .1rem .3rem; border-radius: 3px; }}
 table {{ border-collapse: collapse; margin-top: .5rem; width: 100%; }}
 td, th {{ text-align: left; padding: .3rem .6rem; border-bottom: 1px solid #e4e4e7; }}
</style></head><body>
<h1>Конвейер PDF → Markdown</h1>
<p>Сервис работает. Режим <code>{escape(config.MODE)}</code> — {mode_note}.</p>
<p>Модель: <code>{escape(str(config.MODEL))}</code>. В очереди и в работе: {len(active)}.</p>
<h2>Последние задачи</h2>
<table><tr><th>Документ</th><th>Статус</th><th>Листов</th><th>Текущий</th></tr>
{rows or '<tr><td colspan="4">пока пусто</td></tr>'}</table>
<h2>Куда дальше</h2>
<ul>
 <li><a href="/health">/health</a> — состояние в JSON</li>
 <li><a href="/jobs">/jobs</a> — очередь целиком</li>
 <li><a href="/docs">/docs</a> — интерактивная документация API</li>
</ul>
<p>Интерфейс для инженера — это фронтенд на <a href="http://localhost:8080">8080</a>,
здесь только API.</p>
</body></html>"""


@app.get("/health")
def health():
    jobs = store.list()
    return {
        "ok": True,
        "mode": config.MODE,
        "modeSource": config.MODE_SOURCE,
        "profile": pipeline.describe(),
        "runsDir": str(config.RUNS_DIR),
        "queue": {
            "queued": sum(1 for j in jobs if j.status == STATUS_QUEUED),
            "processing": sum(1 for j in jobs if j.status == STATUS_PROCESSING),
            "done": sum(1 for j in jobs if j.status == STATUS_DONE),
            "error": sum(1 for j in jobs if j.status == STATUS_ERROR),
            "canceled": sum(1 for j in jobs if j.status == STATUS_CANCELED),
        },
        "currentJobId": worker.current_job_id,
        "transcribe": whisper_describe(),
        "time": now_iso(),
    }


@app.post("/transcribe")
async def transcribe_note(audio: UploadFile | None = None):
    """Голосовое замечание → текст.

    Контракт согласован с фронтендом: multipart, поле `audio`, ответ
    `{"text": "фраза"}`, при отсутствии файла — 400.

        curl -s -F "audio=@note.webm" http://127.0.0.1:8000/transcribe

    Аудио на диск не кладём: это голос сотрудника, ему незачем оседать на
    сервере. Файл живёт в памяти столько, сколько идёт запрос.
    """
    if audio is None:
        raise HTTPException(status_code=400, detail="Нет файла: ожидается поле audio")
    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="Пустая запись")
    try:
        text = transcribe(data, audio.filename or "note.webm")
    except TranscribeError as e:
        # 503, а не 500: сервис жив, не настроена или недоступна расшифровка.
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"text": text}


@app.post("/jobs", status_code=201)
async def create_job(
    request: Request,
    file: UploadFile | None = None,
    projectId: str | None = Form(default=None),
    documentId: str | None = Form(default=None),
    pages: str | None = Form(default=None),
    originalName: str | None = Form(default=None),
):
    """Ставит документ в очередь.

    Два способа: multipart с файлом или JSON с путём к уже лежащему PDF
    (когда сервис стоит рядом с фронтом и видит его uploads/).
    """
    if file is not None:
        name = (file.filename or "").lower()
        # DWG и DXF принимаем наравне с PDF: у чертежа текст, слои и размеры
        # лежат данными, и лист читается точнее, чем из отрисованной страницы.
        suffix = next(
            (s for s in (".pdf", ".dwg", ".dxf") if name.endswith(s)), ""
        )
        if not suffix:
            raise HTTPException(
                status_code=400, detail="Принимаются PDF, DWG и DXF"
            )
        config.ensure_dirs()
        target = config.UPLOADS_DIR / f"{uuid.uuid4()}{suffix}"
        size = 0
        with target.open("wb") as sink:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    sink.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail=f"Файл больше {config.MAX_UPLOAD_BYTES // (1024 * 1024)} МБ",
                    )
                sink.write(chunk)
        return _create_job(
            pdf_path=target,
            original_name=originalName or file.filename or target.name,
            project_id=projectId,
            document_id=documentId,
            pages_spec=pages,
        ).to_dict()

    body = await request.json()
    raw_path = body.get("path") or body.get("pdfPath")
    if not raw_path:
        raise HTTPException(status_code=400, detail="Нужен файл или поле path")
    pdf_path = _resolve_pdf_path(str(raw_path))
    return _create_job(
        pdf_path=pdf_path,
        original_name=body.get("originalName") or pdf_path.name,
        project_id=body.get("projectId"),
        document_id=body.get("documentId"),
        pages_spec=body.get("pages"),
    ).to_dict()


@app.get("/jobs")
def list_jobs(
    projectId: str | None = None,
    documentId: str | None = None,
    status: str | None = None,
    active: bool = False,
):
    """Фильтр documentId нужен фронту: после его перезапуска клиент находит
    уже идущий прогон по id документа и подхватывает прогресс вместо того,
    чтобы считать документ заново."""
    jobs = store.list(projectId)
    if documentId:
        jobs = [job for job in jobs if job.documentId == documentId]
    if status:
        wanted = {part.strip() for part in status.split(",") if part.strip()}
        jobs = [job for job in jobs if job.status in wanted]
    if active:
        jobs = [job for job in jobs if job.status in ACTIVE_STATUSES]
    return {"jobs": [job.to_dict() for job in jobs], "currentJobId": worker.current_job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    return _job_or_404(job_id).to_dict()


@app.get("/jobs/{job_id}/pages")
def get_pages(
    job_id: str,
    after: int = Query(default=0, ge=0),
    pages: str | None = None,
    limit: int = Query(default=200, ge=1, le=2000),
):
    """Готовые листы. Фронт тянет дельту: after — наибольший известный ему номер."""
    job = _job_or_404(job_id)
    if pages:
        wanted = _parse_pages(pages, job.pageCount)
    else:
        wanted = [n for n in job.pagesDone if n > after]
    payload = []
    for number in wanted[:limit]:
        page = load_page_json(job, number)
        if page is not None:
            payload.append(page)
    return {
        "job": job.to_dict(),
        "pages": payload,
        "hasMore": len(wanted) > len(payload),
    }


@app.get("/jobs/{job_id}/pages/{page_number}")
def get_page(job_id: str, page_number: int):
    job = _job_or_404(job_id)
    page = load_page_json(job, page_number)
    if page is None:
        raise HTTPException(status_code=404, detail="Лист ещё не готов")
    return page


@app.get("/jobs/{job_id}/pages/{page_number}/raw", response_class=PlainTextResponse)
def get_page_raw(job_id: str, page_number: int):
    """Сырой вывод конвейера по листу: PASS-0 / PASS-A / PASS-B без обработки.

    Нужен, когда важна каждая марка (сверка с ТЗ): в markdown для интерфейса
    извлечение по фрагментам не попадает из-за объёма.
    """
    job = _job_or_404(job_id)
    from service.pipeline import page_file

    target = page_file(Path(job.runDir), page_number)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Лист ещё не готов")
    return PlainTextResponse(
        target.read_text(encoding="utf-8"),
        media_type="text/markdown; charset=utf-8",
    )


@app.get("/jobs/{job_id}/pages/{page_number}/geometry", response_class=PlainTextResponse)
def get_page_geometry(job_id: str, page_number: int):
    """Геометрия листа чертежа таблицей CSV: линии, полилинии и подписи.

    Интерфейс рисует лист по ней сам — и получает то, чего не даёт картинка:
    зум без потери качества, поиск и выделение текста, привязку замечания к
    координатам чертежа. Формат описан в `dwg_geometry.to_csv()`.

    Для PDF маршрут не отвечает: страницу PDF интерфейс рисует через pdf.js.
    """
    job = _job_or_404(job_id)
    source = Path(job.pdfPath)
    if source.suffix.lower() not in (".dwg", ".dxf"):
        raise HTTPException(
            status_code=404,
            detail="Геометрия отдаётся только для чертежей: PDF интерфейс рисует сам",
        )

    # Разбор листа стоит секунды и не меняется, пока лежит тот же файл, —
    # держим рядом с прогоном, вместе с ним и удалится.
    cache = Path(job.runDir) / "geometry"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"page_{page_number:04d}.csv"
    if not target.exists():
        from dwg_geometry import sheet_csv

        try:
            data = sheet_csv(source, page_number)
        except IndexError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(
                status_code=500, detail=f"Не удалось разобрать лист: {error}"
            ) from error
        if not data:
            raise HTTPException(status_code=404, detail="На листе нечего рисовать")
        target.write_text(data, encoding="utf-8")

    return PlainTextResponse(
        target.read_text(encoding="utf-8"),
        media_type="text/csv; charset=utf-8",
    )


@app.get("/jobs/{job_id}/pages/{page_number}/preview")
def get_page_preview(job_id: str, page_number: int, format: str = "svg"):
    """Картинка листа чертежа: `format=svg` для показа, `format=png` для миниатюр.

    Нужна интерфейсу: PDF он рисует сам через pdf.js, а DWG браузер не
    открывает — без этой картинки у чертежа рядом с расшифровкой пустое место
    и сверить одно с другим нечем.

    Для PDF маршрут не отвечает: там страницу по-прежнему рисует фронтенд, и
    отдавать вторую, свою версию той же страницы незачем.
    """
    job = _job_or_404(job_id)
    source = Path(job.pdfPath)
    if source.suffix.lower() not in (".dwg", ".dxf"):
        raise HTTPException(
            status_code=404,
            detail="Предпросмотр отдаётся только для чертежей: PDF интерфейс рисует сам",
        )
    if format not in ("svg", "png"):
        raise HTTPException(status_code=400, detail="format: svg или png")

    # Картинка листа считается секунды, а запрашивается при каждом открытии.
    # Держим её рядом с прогоном — вместе с ним и удалится.
    cache = Path(job.runDir) / "preview"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"page_{page_number:04d}.{format}"
    if not target.exists():
        from dwg_render import sheet_preview

        try:
            if format == "svg":
                image = sheet_preview(source, page_number, "svg")
                if not image:
                    raise HTTPException(status_code=404, detail="Лист нечего рисовать")
                target.write_text(image, encoding="utf-8")
            else:
                if sheet_preview(source, page_number, "png", target) is None:
                    raise HTTPException(status_code=404, detail="Лист нечего рисовать")
        except IndexError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except HTTPException:
            raise
        except Exception as error:  # отрисовка не должна ронять сервис
            raise HTTPException(
                status_code=500, detail=f"Не удалось нарисовать лист: {error}"
            ) from error

    media = "image/svg+xml" if format == "svg" else "image/png"
    return FileResponse(target, media_type=media)


@app.get("/jobs/{job_id}/markdown", response_class=PlainTextResponse)
def get_markdown(job_id: str):
    """Весь документ одним markdown — собирается на лету из готовых листов."""
    job = _job_or_404(job_id)
    chunks = []
    for number in job.pagesDone:
        page = load_page_json(job, number)
        if page:
            chunks.append(page["markdown"].rstrip())
    if not chunks:
        return PlainTextResponse(
            f"# {job.originalName}\n\nНи один лист ещё не готов.\n",
            media_type="text/markdown; charset=utf-8",
        )
    return PlainTextResponse(
        "\n\n---\n\n".join(chunks) + "\n",
        media_type="text/markdown; charset=utf-8",
    )


def _project_or_404(project_id: str):
    jobs = [job for job in store.list(project_id=project_id)]
    if not jobs:
        raise HTTPException(status_code=404, detail="Нет документов такого проекта")
    return jobs


def _project_sections(project_id: str):
    """Разделы проекта, сведённые из всех его документов.

    Модель не вызывается: реквизиты листов читаются из самих файлов. Поэтому
    отчёт о составе доступен сразу после загрузки, ещё до того как конвейер
    дойдёт до последнего листа, — а тексты листов подставляются те, что уже
    посчитаны.
    """
    import bundle

    variants = []
    skipped: list[str] = []
    for job in _project_or_404(project_id):
        path = Path(job.pdfPath)
        if not path.exists():
            skipped.append(f"{job.originalName}: файл не найден")
            continue
        try:
            variants += bundle.read_source(
                path, name=job.originalName, run_dir=job.runDir, job_id=job.id
            )
        except Exception as error:
            # Один нечитаемый файл не должен ронять отчёт по всему проекту:
            # остальные документы инженеру нужны сейчас, а не после разбора
            # с чужим чертежом.
            skipped.append(f"{job.originalName}: {error}")
    return bundle.build(variants), skipped


@app.get("/projects/{project_id}/sections")
def get_project_sections(project_id: str):
    """Состав проекта по разделам: что за листы и из каких файлов взяты."""
    import bundle

    sections, skipped = _project_sections(project_id)
    return {
        "projectId": project_id,
        "sections": bundle.sections_dict(sections),
        "skipped": skipped,
    }


@app.get("/projects/{project_id}/report", response_class=PlainTextResponse)
def get_project_report(project_id: str, bodies: bool = True):
    """Сводный отчёт по проекту одним markdown.

    `bodies=false` отдаёт только состав и расхождения — это быстро и
    достаточно, когда нужно проверить комплектность, а не читать листы.
    """
    import bundle

    sections, skipped = _project_sections(project_id)
    text = bundle.report_markdown(sections, with_bodies=bodies)
    if skipped:
        listed = "\n".join(f"- {item}" for item in skipped)
        text += f"\n\n## Не удалось прочитать\n\n{listed}\n"
    return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")


@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = _job_or_404(job_id)
    if job.status not in ACTIVE_STATUSES:
        return job.to_dict()
    updated = store.patch(job_id, cancelRequested=True)
    if updated and updated.status == STATUS_QUEUED:
        # Задача ещё не в работе — снимаем сразу, воркер её не увидит.
        updated = store.patch(
            job_id,
            status=STATUS_CANCELED,
            processingStep=None,
            processingPage=None,
            finishedAt=now_iso(),
            errorMessage="Снято из очереди",
        )
    return updated.to_dict() if updated else {}


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, reset: bool = False):
    """Повторяет прогон. По умолчанию досчитывает недостающие листы;
    reset=true считает документ заново с нуля."""
    job = _job_or_404(job_id)
    run_dir = Path(job.runDir)
    if reset:
        for sub in ("pages", "frontend", "runs"):
            shutil.rmtree(run_dir / sub, ignore_errors=True)
    updated = store.patch(
        job_id,
        status=STATUS_QUEUED,
        processingStep=STATUS_QUEUED,
        processingPage=None,
        errorMessage=None,
        cancelRequested=False,
        pagesDone=[] if reset else job.pagesDone,
        pageErrors={},
        finishedAt=None,
        elapsedSec=None,
    )
    worker.wake()
    return updated.to_dict() if updated else {}


@app.delete("/jobs/{job_id}")
def delete_job(job_id: str, purge: bool = False):
    job = _job_or_404(job_id)
    if job.status in ACTIVE_STATUSES:
        store.patch(job_id, cancelRequested=True)
    if purge:
        shutil.rmtree(Path(job.runDir), ignore_errors=True)
        pdf_path = Path(job.pdfPath)
        try:
            pdf_path.relative_to(config.UPLOADS_DIR)
            pdf_path.unlink(missing_ok=True)
        except ValueError:
            pass  # чужой файл (например, uploads фронта) не трогаем
    store.delete(job_id)
    return {"ok": True}


@app.exception_handler(PipelineError)
def pipeline_error_handler(request: Request, error: PipelineError):
    return JSONResponse(status_code=503, content={"detail": str(error)})
