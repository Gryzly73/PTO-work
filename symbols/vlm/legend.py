"""Constrained VLM recovery of legend regions and rows.

The model only proposes geometry and text. Every proposal is validated here
before it can become a ``LegendEntry``; downstream matching still treats a
template hit as ``probable`` rather than ``confirmed``.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

import pymupdf
from PIL import Image

from ..artifacts import (
    PageArtifacts,
    atomic_write_json,
    page_sidecar_dir,
    write_crop,
    write_page_artifacts,
)
from ..crop_normalizer import (
    crop_image,
    normalize_symbol_crop,
    png_bytes,
)
from ..geometry import BBox, PageTransform
from ..legend_layout import LegendRegionResult
from ..schema import LegendEntry, stable_id


_LOCATE_SYSTEM = (
    "Ты локализатор легенд строительных чертежей. Не описывай лист и не "
    "рассуждай. Верни только один JSON-объект по указанной схеме."
)
_LOCATE_PROMPT = """
Найди компактную область «Условные обозначения»/legend, где каждой графической
метке соответствует текстовая расшифровка. Не выбирай штамп, спецификацию,
основной чертёж или большую таблицу.

Координаты нормализованы: левый верх страницы [0,0], правый низ [1000,1000].
Верни строго:
{"regions":[{"bbox":[x0,y0,x1,y1],"confidence":0.0}]}
Если подходящей области нет: {"regions":[]}
Не добавляй Markdown и другие поля.
""".strip()

_ROWS_SYSTEM = (
    "Ты OCR-экстрактор строк легенды строительного чертежа. Не придумывай "
    "невидимый текст. Верни только один JSON-объект по указанной схеме."
)
_ROWS_PROMPT = """
Это уже вырезанная область легенды. Раздели её на строки «символ слева —
название справа». Координаты нормализованы относительно ЭТОГО кропа:
[0,0]..[1000,1000].

Верни строго:
{"rows":[{"name":"видимый текст","rowBBox":[x0,y0,x1,y1],
"textBBox":[x0,y0,x1,y1],"confidence":0.0}]}

rowBBox охватывает всю строку вместе с символом; textBBox — только название
справа от символа. Строки перечисли сверху вниз. Заголовки и номера листа не
включай. Нечитаемые строки пропускай. Не добавляй Markdown и другие поля.
""".strip()


class LegendAssistError(RuntimeError):
    """A provider failure or a response rejected by strict validation."""


@dataclass(frozen=True, slots=True)
class LegendVlmConfig:
    enabled: bool = False
    model: str = "qwen3vl-32b"
    provider: str | None = None
    min_region_confidence: float = 0.70
    min_row_confidence: float = 0.55
    max_calls_per_page: int = 2
    max_regions: int = 2
    max_rows: int = 80
    locate_max_px: int = 2000
    row_max_px: int = 1800
    render_dpi: int = 180
    max_tokens: int = 1800
    retries: int = 2
    retry_delay: float = 2.0

    def __post_init__(self) -> None:
        for name in ("min_region_confidence", "min_row_confidence"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{name} must be between 0 and 1")
        for name in (
            "max_calls_per_page",
            "max_regions",
            "max_rows",
            "locate_max_px",
            "row_max_px",
            "render_dpi",
            "max_tokens",
            "retries",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.enabled and self.max_calls_per_page < 2:
            raise ValueError("enabled legend VLM requires at least two calls per page")
        if (
            isinstance(self.retry_delay, bool)
            or not isinstance(self.retry_delay, (int, float))
            or not math.isfinite(self.retry_delay)
            or self.retry_delay < 0
        ):
            raise ValueError("retry_delay must be a non-negative number")

    @classmethod
    def from_env(cls) -> "LegendVlmConfig":
        def env_bool(name: str, default: bool = False) -> bool:
            raw = (os.environ.get(name) or "").strip().casefold()
            return default if not raw else raw in {"1", "true", "yes", "on", "да"}

        def env_int(name: str, default: int) -> int:
            try:
                return int((os.environ.get(name) or "").strip() or default)
            except ValueError:
                return default

        return cls(
            enabled=env_bool("PTO_SYMBOLS_VLM"),
            model=(
                os.environ.get("PTO_SYMBOLS_VLM_MODEL")
                or os.environ.get("PTO_MODEL")
                or "qwen3vl-32b"
            ).strip(),
            provider=(
                os.environ.get("PTO_SYMBOLS_VLM_PROVIDER")
                or os.environ.get("PTO_PROVIDER")
                or None
            ),
            max_calls_per_page=env_int("PTO_SYMBOLS_VLM_MAX_CALLS", 2),
            max_rows=env_int("PTO_SYMBOLS_VLM_MAX_ROWS", 80),
        )


class LegendAssistBackend(Protocol):
    """Replaceable provider used by tests and the production HF adapter."""

    @property
    def trace_info(self) -> Mapping[str, Any]:
        """Return non-secret provider/model metadata."""

    def locate(self, page_image: Image.Image) -> str | Mapping[str, Any]:
        """Return the strict region payload."""

    def read_rows(self, legend_image: Image.Image) -> str | Mapping[str, Any]:
        """Return the strict row payload."""


class _HuggingFaceLegendBackend:
    def __init__(self, config: LegendVlmConfig) -> None:
        import hf_api_bench as hb

        hb.load_dotenv()
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
        if not token or token.startswith("hf_xxx"):
            raise LegendAssistError("PTO_SYMBOLS_VLM requires a valid HF_TOKEN")
        spec = hb.get_spec(config.model)
        provider = hb.resolve_provider(spec, config.provider, token)
        self._hb = hb
        self._config = config
        self._spec = spec
        self._provider = provider
        self._client = hb.make_client(token, provider)
        self._usage = hb.UsageTotals()
        self._calls = 0

    @property
    def trace_info(self) -> Mapping[str, Any]:
        return {
            "backend": "huggingface",
            "model": self._spec.hf_id,
            "provider": self._provider,
            "calls": self._calls,
            "usage": self._usage.as_dict(),
        }

    def _call(
        self,
        image: Image.Image,
        *,
        system: str,
        prompt: str,
        max_px: int,
    ) -> str:
        if self._calls >= self._config.max_calls_per_page:
            raise LegendAssistError("legend VLM call budget exceeded")
        self._calls += 1
        data_url = self._hb.image_to_data_url(image, max_px=max_px, quality=92)

        def request() -> str:
            return self._hb.chat_vision(
                self._client,
                self._spec.hf_id,
                system,
                prompt,
                data_url,
                max_tokens=self._config.max_tokens,
                prompt_style=self._spec.prompt_style,
                usage=self._usage,
            )

        return self._hb.call_with_retries(
            request,
            retries=self._config.retries,
            base_delay=self._config.retry_delay,
            usage=self._usage,
        )

    def locate(self, page_image: Image.Image) -> str:
        return self._call(
            page_image,
            system=_LOCATE_SYSTEM,
            prompt=_LOCATE_PROMPT,
            max_px=self._config.locate_max_px,
        )

    def read_rows(self, legend_image: Image.Image) -> str:
        return self._call(
            legend_image,
            system=_ROWS_SYSTEM,
            prompt=_ROWS_PROMPT,
            max_px=self._config.row_max_px,
        )


@dataclass(frozen=True, slots=True)
class _ValidatedRow:
    name: str
    row_bbox: BBox
    text_bbox: BBox
    confidence: float


def _json_object(value: str | Mapping[str, Any], *, stage: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.strip():
        raise LegendAssistError(f"{stage}: empty response")
    text = value.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LegendAssistError(f"{stage}: response is not strict JSON") from exc
    if not isinstance(parsed, Mapping):
        raise LegendAssistError(f"{stage}: root must be an object")
    return parsed


def _normalized_bbox(value: Any, *, name: str) -> BBox:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise LegendAssistError(f"{name} must be [x0,y0,x1,y1]")
    coordinates = tuple(value)
    if len(coordinates) != 4 or any(
        isinstance(item, bool)
        or not isinstance(item, (int, float))
        or not math.isfinite(item)
        for item in coordinates
    ):
        raise LegendAssistError(f"{name} contains invalid coordinates")
    bbox = BBox.from_list(coordinates)
    if bbox.x0 < 0 or bbox.y0 < 0 or bbox.x1 > 1000 or bbox.y1 > 1000:
        raise LegendAssistError(f"{name} must stay inside [0,1000]")
    return bbox


def _confidence(value: Any, *, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise LegendAssistError(f"{name} must be between 0 and 1")
    return float(value)


def _validate_region(
    payload: Mapping[str, Any], config: LegendVlmConfig
) -> tuple[BBox, float]:
    if set(payload) != {"regions"} or not isinstance(payload["regions"], list):
        raise LegendAssistError("locate: expected only a regions array")
    regions = payload["regions"]
    if not regions:
        raise LegendAssistError("locate: model reported no legend")
    if len(regions) > config.max_regions:
        raise LegendAssistError("locate: region budget exceeded")
    validated: list[tuple[BBox, float]] = []
    for index, item in enumerate(regions):
        if not isinstance(item, Mapping) or set(item) != {"bbox", "confidence"}:
            raise LegendAssistError(f"locate: invalid region {index}")
        bbox = _normalized_bbox(item["bbox"], name=f"regions[{index}].bbox")
        confidence = _confidence(
            item["confidence"], name=f"regions[{index}].confidence"
        )
        area_ratio = bbox.width * bbox.height / 1_000_000
        if (
            confidence < config.min_region_confidence
            or bbox.width < 20
            or bbox.height < 20
            or bbox.width > 850
            or bbox.height > 650
            or area_ratio > 0.35
        ):
            raise LegendAssistError(f"locate: unsafe region {index}")
        validated.append((bbox, confidence))
    return max(validated, key=lambda item: (item[1], -item[0].width * item[0].height))


def _validate_rows(
    payload: Mapping[str, Any], config: LegendVlmConfig
) -> list[_ValidatedRow]:
    if set(payload) != {"rows"} or not isinstance(payload["rows"], list):
        raise LegendAssistError("rows: expected only a rows array")
    raw_rows = payload["rows"]
    if not 2 <= len(raw_rows) <= config.max_rows:
        raise LegendAssistError("rows: count outside configured bounds")
    rows: list[_ValidatedRow] = []
    seen_names: set[str] = set()
    for index, item in enumerate(raw_rows):
        expected = {"name", "rowBBox", "textBBox", "confidence"}
        if not isinstance(item, Mapping) or set(item) != expected:
            raise LegendAssistError(f"rows: invalid row {index}")
        name = item["name"]
        if not isinstance(name, str):
            raise LegendAssistError(f"rows[{index}].name must be text")
        name = re.sub(r"\s+", " ", name).strip()
        normalized_name = name.casefold()
        if not name or len(name) > 160 or normalized_name in seen_names:
            raise LegendAssistError(f"rows[{index}].name is unsafe")
        seen_names.add(normalized_name)
        row_bbox = _normalized_bbox(item["rowBBox"], name=f"rows[{index}].rowBBox")
        text_bbox = _normalized_bbox(
            item["textBBox"], name=f"rows[{index}].textBBox"
        )
        confidence = _confidence(
            item["confidence"], name=f"rows[{index}].confidence"
        )
        if confidence < config.min_row_confidence:
            raise LegendAssistError(f"rows[{index}] confidence is too low")
        if (
            text_bbox.x0 < row_bbox.x0
            or text_bbox.y0 < row_bbox.y0
            or text_bbox.x1 > row_bbox.x1
            or text_bbox.y1 > row_bbox.y1
            or text_bbox.x0 - row_bbox.x0 < max(12, row_bbox.width * 0.08)
            or row_bbox.height < 5
            or row_bbox.height > 220
        ):
            raise LegendAssistError(f"rows[{index}] geometry is unsafe")
        rows.append(_ValidatedRow(name, row_bbox, text_bbox, confidence))
    rows.sort(key=lambda item: (item.row_bbox.y0, item.row_bbox.x0))
    for first, second in zip(rows, rows[1:]):
        overlap = min(first.row_bbox.y1, second.row_bbox.y1) - max(
            first.row_bbox.y0, second.row_bbox.y0
        )
        if overlap > min(first.row_bbox.height, second.row_bbox.height) * 0.25:
            raise LegendAssistError("rows: overlapping row geometry")
    return rows


def _scale_normalized(bbox: BBox, container: BBox) -> BBox:
    return BBox(
        container.x0 + bbox.x0 * container.width / 1000,
        container.y0 + bbox.y0 * container.height / 1000,
        container.x0 + bbox.x1 * container.width / 1000,
        container.y0 + bbox.y1 * container.height / 1000,
    )


def _trace_path(output_root: str | Path, page_number: int) -> Path:
    return page_sidecar_dir(output_root, page_number) / "vlm_legend_trace.json"


def assist_page_legend(
    document: str | Path,
    *,
    page_number: int,
    output_root: str | Path,
    config: LegendVlmConfig,
    backend: LegendAssistBackend | None = None,
) -> tuple[LegendRegionResult, list[LegendEntry]]:
    """Recover one legend with two bounded calls or fail without side effects."""

    if not config.enabled:
        raise LegendAssistError("legend VLM assist is disabled")
    source = Path(document)
    provider = backend
    trace: dict[str, Any] = {
        "schemaVersion": 1,
        "page": page_number,
        "status": "started",
    }
    try:
        if provider is None:
            provider = _HuggingFaceLegendBackend(config)
        with pymupdf.open(source) as opened:
            if page_number > opened.page_count:
                raise LegendAssistError("page number exceeds document page count")
            page = opened[page_number - 1]
            page_box = BBox(0, 0, float(page.rect.width), float(page.rect.height))
            locate_pixmap = page.get_pixmap(dpi=96, alpha=False)
            locate_image = Image.frombytes(
                "RGB",
                (locate_pixmap.width, locate_pixmap.height),
                locate_pixmap.samples,
            )
            locate_raw = provider.locate(locate_image)
            locate_payload = _json_object(locate_raw, stage="locate")
            region_normalized, region_confidence = _validate_region(
                locate_payload,
                config,
            )
            region_pdf = _scale_normalized(region_normalized, page_box)

            pixmap = page.get_pixmap(dpi=config.render_dpi, alpha=False)
            page_image = Image.frombytes(
                "RGB", (pixmap.width, pixmap.height), pixmap.samples
            )
            transform = PageTransform(
                pdf_width=page_box.width,
                pdf_height=page_box.height,
                raster_width=pixmap.width,
                raster_height=pixmap.height,
            )
            region_raster = transform.pdf_to_raster(region_pdf)
            legend_image = crop_image(page_image, region_raster)
            rows_raw = provider.read_rows(legend_image)
            rows_payload = _json_object(rows_raw, stage="rows")
            rows = _validate_rows(
                rows_payload,
                config,
            )

            entries: list[LegendEntry] = []
            page_dir = page_sidecar_dir(output_root, page_number)
            for index, row in enumerate(rows, start=1):
                row_pdf = _scale_normalized(row.row_bbox, region_pdf)
                text_pdf = _scale_normalized(row.text_bbox, region_pdf)
                symbol_right = text_pdf.x0 - max(2.0, row_pdf.width * 0.01)
                if symbol_right - row_pdf.x0 < 12:
                    raise LegendAssistError(
                        f"rows[{index - 1}] leaves no usable symbol crop"
                    )
                symbol_pdf = BBox(
                    row_pdf.x0,
                    row_pdf.y0,
                    symbol_right,
                    row_pdf.y1,
                )
                entry_id = stable_id(
                    "LE",
                    page_number,
                    "vlm-legend",
                    index,
                    [round(value, 3) for value in row_pdf.to_list()],
                    row.name,
                )
                raw_name = f"legend/{entry_id}.raw.png"
                normalized_name = f"legend/{entry_id}.normalized.png"
                raw_crop = crop_image(page_image, transform.pdf_to_raster(row_pdf))
                symbol_crop = crop_image(
                    page_image, transform.pdf_to_raster(symbol_pdf)
                )
                write_crop(page_dir, raw_name, png_bytes(raw_crop))
                write_crop(
                    page_dir,
                    normalized_name,
                    png_bytes(normalize_symbol_crop(symbol_crop)),
                )
                entries.append(
                    LegendEntry(
                        id=entry_id,
                        page=page_number,
                        region_id="legend-vlm",
                        position=str(index),
                        name_raw=row.name,
                        name_normalized=row.name.casefold(),
                        bbox_pdf=row_pdf,
                        symbol_bbox_pdf=symbol_pdf,
                        text_bbox_pdf=text_pdf,
                        raw_crop=f"symbol_crops/{raw_name}",
                        normalized_crop=f"symbol_crops/{normalized_name}",
                        source_kind="vlm_text",
                        source_document=source.name,
                        status="extracted",
                        confidence=row.confidence,
                    )
                )

        result = LegendRegionResult(
            "found",
            "legend-vlm",
            region_pdf,
            min(
                region_confidence,
                sum(row.confidence for row in rows) / len(rows),
            ),
            ("vlm_region", "vlm_rows", "validated"),
        )
        write_page_artifacts(
            output_root,
            PageArtifacts(page=page_number, legend_entries=entries),
        )
        trace.update(
            {
                "status": "accepted",
                "region": result.to_dict(),
                "rowCount": len(entries),
                "backend": dict(provider.trace_info),
                "validatedResponse": {
                    "locate": locate_payload,
                    "rows": rows_payload,
                },
            }
        )
        atomic_write_json(_trace_path(output_root, page_number), trace)
        return result, entries
    except Exception as exc:
        trace.update(
            {
                "status": "rejected",
                "error": f"{type(exc).__name__}: {exc}",
                "backend": dict(provider.trace_info) if provider is not None else None,
            }
        )
        atomic_write_json(_trace_path(output_root, page_number), trace)
        if isinstance(exc, LegendAssistError):
            raise
        raise LegendAssistError(str(exc)) from exc
