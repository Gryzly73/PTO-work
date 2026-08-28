"""Обёртка над конвейером. Сам hf_api_bench.py не меняется — он вызывается
как библиотека.

Ключевое решение: run_vlm() зовётся по одному листу за вызов, а не списком.
Так мы получаем точный прогресс, возможность прервать прогон между листами и
возобновление после перезапуска (готовые pages/page_NNNN.md просто не
пересчитываются — на 1000 страницах это разница между «потеряли сутки» и
«продолжили с 341-го листа»).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import fitz  # PyMuPDF

from service import config


class PipelineError(RuntimeError):
    pass


def page_file(run_dir: Path, page_number: int) -> Path:
    return run_dir / "pages" / f"page_{page_number:04d}.md"


# Чертежи AutoCAD. Для них конвейер работает иначе: текст, слои и размеры
# лежат в файле данными, поэтому модель не вызывается вовсе — ни в режиме
# real, ни в mock. Подробности — dwg_sheets.py.
VECTOR_SUFFIXES = {".dwg", ".dxf"}


def is_vector(path: Path) -> bool:
    return path.suffix.lower() in VECTOR_SUFFIXES


def pdf_page_count(pdf_path: Path) -> int:
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def document_sheets(path: Path) -> int:
    """Сколько листов в документе: страницы PDF или листы чертежа."""
    if is_vector(path):
        from dwg_sheets import sheet_count

        return sheet_count(path)
    return pdf_page_count(path)


class Pipeline:
    """Единая точка входа в конвейер. Режим real ходит в модель, mock — нет."""

    def __init__(self, mode: str | None = None) -> None:
        self.mode = (mode or config.MODE).lower()
        self._ready = False
        self._spec = None
        self._client = None
        self._provider = None
        self._defaults: dict = {}

    # --- инициализация ------------------------------------------------------
    def prepare(self) -> None:
        if self._ready or self.mode != "real":
            self._ready = True
            return

        import hf_api_bench as hb

        hb.load_dotenv()
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
        if not token or token.startswith("hf_xxx"):
            raise PipelineError(
                "Нет HF_TOKEN. Впишите токен в backend/.env "
                "или запустите сервис с PTO_PIPELINE_MODE=mock."
            )

        # Тест-сетовые подсказки по номеру страницы губительны на чужих PDF:
        # они подсказывают модели то, чего на листе нет.
        hb.USE_ZONE_HINTS = bool(config.ZONE_HINTS)

        self._spec = hb.get_spec(config.MODEL)
        self._provider = hb.resolve_provider(self._spec, config.PROVIDER, token)
        self._client = hb.make_client(token, self._provider)
        defaults = hb.vision_defaults(self._spec)
        if config.HIGH_DPI:
            defaults = {**defaults, **hb.HIGH_DPI_DEFAULTS}
        self._defaults = defaults
        self._ready = True

    def describe(self) -> dict:
        info = config.profile_dict()
        info["provider"] = self._provider or config.PROVIDER
        if self._spec is not None:
            info["hfModel"] = self._spec.hf_id
        return info

    # --- работа -------------------------------------------------------------
    def skipped_page(
        self, pdf_path: Path, page_number: int, run_dir: Path, *, passport, reason: str
    ) -> dict:
        """Лист, который сознательно не считался (скан при PTO_SCAN_POLICY=skip).

        Пишется в том же контракте, что и обычный лист: паспорт в PASS-0 и
        причина в PASS-A. Так лист остаётся в документе на своём месте, а
        интерфейс и модель-сверщик видят, почему он пуст, вместо того чтобы
        считать его потерянным.
        """
        from sheet_aware import passport_markdown

        started = time.time()
        content = (
            "### PASS-0 Паспорт листа\n\n"
            + passport_markdown(passport)
            + "\n### PASS-A Описание листа\n\n"
            + f"_Лист не обрабатывался: {reason}_\n"
        )
        target = page_file(run_dir, page_number)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "markdown": content,
            "usage": {},
            "elapsed": round(time.time() - started, 1),
            "skipped": True,
        }

    def page_needs_model(self, pdf_path: Path, page_number: int, passport) -> bool:
        """Уйдёт ли лист в модель: скан или слой, на который нельзя опереться."""
        if is_vector(pdf_path) or self.mode != "real":
            return False
        if passport is not None and passport.needs_model:
            return True
        if not config.LAYER_AWARE:
            return True
        try:
            import hf_api_bench as hb

            with fitz.open(pdf_path) as doc:
                page = doc[page_number - 1]
                kind = passport.kind if passport is not None else None
                return not hb.page_layer_is_usable(page, page_number, kind)
        except Exception:
            return True

    def run_page(self, pdf_path: Path, page_number: int, run_dir: Path) -> dict:
        """Считает один лист. Возвращает markdown, usage и время."""
        started = time.time()
        if is_vector(pdf_path):
            # Чертёж читается как данные: ни токена, ни модели не нужно,
            # поэтому и prepare() здесь не к месту — он требует HF_TOKEN.
            raw = self._vector_page(pdf_path, page_number, run_dir)
            return {
                "markdown": raw,
                "usage": {},
                "elapsed": round(time.time() - started, 1),
            }
        self.prepare()
        if self.mode == "mock":
            print(
                f"[pipeline] лист {page_number}: MOCK "
                f"(источник={config.MODE_SOURCE}, модель не вызывается)",
                flush=True,
            )
            raw = self._mock_page(pdf_path, page_number, run_dir)
            usage: dict = {}
        else:
            print(
                f"[pipeline] лист {page_number}: REAL "
                f"модель={config.MODEL}",
                flush=True,
            )
            raw, usage = self._real_page(pdf_path, page_number, run_dir)
        return {
            "markdown": raw,
            "usage": usage,
            "elapsed": round(time.time() - started, 1),
        }

    def _vector_page(self, path: Path, page_number: int, run_dir: Path) -> str:
        """Лист чертежа. Модель не вызывается: всё нужное лежит в файле."""
        from dwg_sheets import page_markdown

        try:
            body, kind = page_markdown(path, page_number)
        except IndexError as e:
            raise PipelineError(str(e)) from e
        except Exception as e:
            raise PipelineError(
                f"Не удалось прочитать чертёж: {e}. Для DWG нужен конвертер "
                "dwg2dxf или ODA File Converter (путь в PTO_DWG2DXF); DXF "
                "читается без него."
            ) from e
        _save_objects(path, page_number, run_dir)
        # Тип листа кладём в паспорт: конвертер PDF берёт его из PASS-0, и
        # тем же способом он доедет до интерфейса.
        nl = chr(10)
        head = f"### PASS-0 kind{nl}{nl}- kind: `{kind}`{nl}{nl}"
        body = head + body
        target = page_file(run_dir, page_number)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return body

    def _real_page(self, pdf_path: Path, page_number: int, run_dir: Path):
        import hf_api_bench as hb

        usage = hb.UsageTotals()
        run_dir.mkdir(parents=True, exist_ok=True)
        hb.run_vlm(
            self._client,
            self._spec,
            pdf_path,
            [page_number],
            page_max=self._defaults["page_max"],
            tile_max=self._defaults["tile_max"],
            out_dir=run_dir,
            fail_fast=False,
            ocr_prompt="ocr",
            image_max_px=self._defaults["image_max_px"],
            jpeg_quality=self._defaults["jpeg_quality"],
            max_tokens=self._defaults["max_tokens"],
            max_tiles=int(self._defaults.get("max_tiles", 6)),
            stamp_crop=config.STAMP_CROP,
            zone_crop=config.ZONE_CROP,
            two_pass=config.TWO_PASS,
            runs=config.RUNS,
            retries=config.RETRIES,
            retry_delay=config.RETRY_DELAY,
            usage=usage,
            sheet_aware=config.SHEET_AWARE,
            layer_aware=config.LAYER_AWARE,
            lean=config.LEAN,
        )
        target = page_file(run_dir, page_number)
        if not target.exists():
            # Конвейер сознательно не пишет лист, на котором не удался ни один
            # вызов модели (обрыв провайдера, 413 и т. п.). Файла нет — значит
            # лист не считается готовым: воркер пометит его ошибкой, и при
            # следующем запуске он будет пересчитан, а не пропущен.
            raise PipelineError(
                f"Лист {page_number}: ни один вызов модели не удался "
                f"(вызовов {usage.calls}, неудачных фрагментов "
                f"{usage.failed_tiles}). Лист не сохранён и будет пересчитан."
            )
        return target.read_text(encoding="utf-8"), usage.as_dict()

    def _mock_page(self, pdf_path: Path, page_number: int, run_dir: Path) -> str:
        """Имитация листа без обращения к модели.

        Нужна, чтобы проверять склейку с фронтом — очередь, потоковую отдачу
        страниц, поведение при обновлении страницы — не тратя по две минуты и
        токены на лист. Вывод помечен, чтобы его нельзя было принять за
        работу модели.
        """
        from service.convert import page_layer_text

        sections = []
        try:
            from sheet_aware import build_passport, passport_markdown

            with fitz.open(pdf_path) as doc:
                passport = build_passport(doc[page_number - 1], page_number)
            sections.append(
                "### PASS-0 Паспорт листа\n\n" + passport_markdown(passport)
            )
        except Exception as error:  # паспорт не критичен
            sections.append(f"### PASS-0 Паспорт листа\n\n_недоступен: {error}_")

        time.sleep(max(0.0, config.MOCK_PAGE_SECONDS))

        layer = page_layer_text(pdf_path, page_number)
        head = "\n".join(layer.splitlines()[:25]) if layer else ""
        body = (
            f"Начало текстового слоя листа:\n\n{head}"
            if head
            else "У листа нет пригодного текстового слоя — здесь работала бы VLM."
        )
        sections.append(
            "### PASS-A Описание листа\n\n"
            "**[MOCK] Это не работа модели.** Сервис запущен в режиме "
            "PTO_PIPELINE_MODE=mock для проверки интеграции с интерфейсом. "
            "Настоящее описание листа появится в режиме real.\n\n" + body
        )
        sections.append(
            "### PASS-B Тайлы / текст\n\n_[MOCK] фрагменты листа не извлекались._"
        )

        content = "\n\n".join(sections)
        pages_dir = run_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        page_file(run_dir, page_number).write_text(content, encoding="utf-8")
        return content


def objects_file(run_dir: Path, page_number: int) -> Path:
    """Файл с картой «строка текста листа → объекты чертежа»."""
    return run_dir / "pages" / f"page_{page_number:04d}.objects.json"


def _save_objects(path: Path, page_number: int, run_dir: Path) -> None:
    """Сохраняет карту строк листа рядом с самим листом.

    По ней интерфейс связывает строку в тексте с подписью на чертеже: ткнул в
    строку — подсветилась надпись. Считать её при запросе нельзя: разобранного
    листа к тому времени уже нет, а разбирать чертёж заново ради подсветки —
    секунды на каждый клик.

    Номер объекта здесь тот же, что в колонке `id` геометрии: обе стороны
    считают его одной функцией (`dwg_sheets.text_uid`), иначе связь развалится
    при первой же правке одной из них.

    Ошибка здесь не должна стоить листа: без карты лист читается, просто без
    подсветки.
    """
    try:
        from dwg_sheets import sheet_objects

        marked = sheet_objects(path, page_number)
        target = objects_file(run_dir, page_number)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"lines": marked}, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as error:
        print(
            f"[pipeline] карта объектов листа {page_number} не сохранена: {error}",
            flush=True,
        )


def rebuild_out_md(run_dir: Path) -> Path | None:
    """Пересобирает out.md прогона из готовых листов.

    Формат «## Страница N» обязателен: на нём завязаны compare_to_etalon.py,
    build_ios2_md.py и остальные инструменты бэкенда.
    """
    pages_dir = run_dir / "pages"
    if not pages_dir.exists():
        return None
    pages: dict[int, str] = {}
    for item in sorted(pages_dir.glob("page_*.md")):
        try:
            number = int(item.stem.split("_")[1])
        except (IndexError, ValueError):
            continue
        pages[number] = item.read_text(encoding="utf-8")
    if not pages:
        return None
    from hf_api_bench import assemble

    out_path = run_dir / "out.md"
    out_path.write_text(assemble(pages), encoding="utf-8")
    return out_path
