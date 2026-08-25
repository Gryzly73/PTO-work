"""Bounded VLM review of already-existing ambiguous legend choices."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from PIL import Image, ImageDraw, ImageOps

from ..artifacts import (
    PageArtifacts,
    atomic_write_json,
    page_sidecar_dir,
    write_page_artifacts,
)
from ..schema import (
    ClassificationEvidence,
    LegendEntry,
    SymbolInstance,
    SymbolType,
)


PROMPT_VERSION = "h6-symbol-review-v1"
_SYSTEM = (
    "Ты проверяешь только визуальное сходство условного знака с ограниченным "
    "набором вариантов из легенды этого же документа. Не придумывай варианты "
    "и не рассуждай. Верни только JSON."
)


class SymbolReviewError(RuntimeError):
    """Provider failure or a response rejected by the H6 contract."""


@dataclass(frozen=True, slots=True)
class SymbolReviewVlmConfig:
    enabled: bool = False
    model: str = "qwen3vl-32b"
    provider: str | None = None
    max_calls_per_page: int = 5
    max_choices_per_type: int = 4
    minimum_repeats: int = 2
    minimum_confidence: float = 0.65
    image_max_px: int = 1600
    max_tokens: int = 300
    retries: int = 2
    retry_delay: float = 2.0

    def __post_init__(self) -> None:
        for name in (
            "max_calls_per_page",
            "max_choices_per_type",
            "minimum_repeats",
            "image_max_px",
            "max_tokens",
            "retries",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.minimum_confidence, bool)
            or not isinstance(self.minimum_confidence, (int, float))
            or not math.isfinite(self.minimum_confidence)
            or not 0 <= self.minimum_confidence <= 1
        ):
            raise ValueError("minimum_confidence must be between 0 and 1")
        if (
            isinstance(self.retry_delay, bool)
            or not isinstance(self.retry_delay, (int, float))
            or not math.isfinite(self.retry_delay)
            or self.retry_delay < 0
        ):
            raise ValueError("retry_delay must be a non-negative number")

    @classmethod
    def from_env(cls) -> "SymbolReviewVlmConfig":
        def env_bool(name: str) -> bool:
            return (os.environ.get(name) or "").strip().casefold() in {
                "1",
                "true",
                "yes",
                "on",
                "да",
            }

        def env_int(name: str, default: int) -> int:
            try:
                return int((os.environ.get(name) or "").strip() or default)
            except ValueError:
                return default

        return cls(
            enabled=env_bool("PTO_SYMBOLS_VLM_REVIEW"),
            model=(
                os.environ.get("PTO_SYMBOLS_VLM_REVIEW_MODEL")
                or os.environ.get("PTO_SYMBOLS_VLM_MODEL")
                or os.environ.get("PTO_MODEL")
                or "qwen3vl-32b"
            ).strip(),
            provider=(
                os.environ.get("PTO_SYMBOLS_VLM_REVIEW_PROVIDER")
                or os.environ.get("PTO_SYMBOLS_VLM_PROVIDER")
                or os.environ.get("PTO_PROVIDER")
                or None
            ),
            max_calls_per_page=env_int("PTO_SYMBOLS_VLM_REVIEW_MAX_CALLS", 5),
            minimum_repeats=env_int("PTO_SYMBOLS_VLM_REVIEW_MIN_REPEATS", 2),
        )


class SymbolReviewBackend(Protocol):
    @property
    def trace_info(self) -> Mapping[str, Any]:
        """Return non-secret provider/model metadata."""

    def choose(self, board: Image.Image, prompt: str) -> str | Mapping[str, Any]:
        """Choose one allowed legend ID or unresolved."""


class _HuggingFaceReviewBackend:
    def __init__(self, config: SymbolReviewVlmConfig) -> None:
        import hf_api_bench as hb

        hb.load_dotenv()
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
        if not token or token.startswith("hf_xxx"):
            raise SymbolReviewError(
                "PTO_SYMBOLS_VLM_REVIEW requires a valid HF_TOKEN"
            )
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

    def choose(self, board: Image.Image, prompt: str) -> str:
        if self._calls >= self._config.max_calls_per_page:
            raise SymbolReviewError("H6 VLM call budget exceeded")
        self._calls += 1
        data_url = self._hb.image_to_data_url(
            board,
            max_px=self._config.image_max_px,
            quality=92,
        )

        def request() -> str:
            return self._hb.chat_vision(
                self._client,
                self._spec.hf_id,
                _SYSTEM,
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


def _safe_crop_path(page_dir: Path, relative: str) -> Path:
    path = page_dir / Path(relative.replace("\\", "/"))
    try:
        path.resolve().relative_to(page_dir.resolve())
    except ValueError as exc:
        raise SymbolReviewError("crop path escapes page directory") from exc
    if not path.is_file():
        raise SymbolReviewError(f"missing crop: {relative}")
    return path


def _open_tile(path: Path, size: tuple[int, int]) -> Image.Image:
    try:
        with Image.open(path) as image:
            tile = ImageOps.contain(image.convert("RGB"), size)
    except OSError as exc:
        raise SymbolReviewError(f"cannot read crop {path.name}") from exc
    canvas = Image.new("RGB", size, "white")
    canvas.paste(
        tile,
        ((size[0] - tile.width) // 2, (size[1] - tile.height) // 2),
    )
    return canvas


def _review_board(
    type_crop: Path,
    choices: Sequence[tuple[LegendEntry, Path]],
) -> Image.Image:
    width = max(520, 190 * len(choices))
    board = Image.new("RGB", (width, 430), "white")
    draw = ImageDraw.Draw(board)
    draw.text((12, 8), "TARGET", fill="black")
    target = _open_tile(type_crop, (180, 180))
    board.paste(target, ((width - 180) // 2, 28))
    option_width = width // len(choices)
    for index, (_, crop_path) in enumerate(choices, start=1):
        x0 = (index - 1) * option_width
        draw.text((x0 + 12, 226), f"OPTION {index}", fill="black")
        tile = _open_tile(crop_path, (150, 150))
        board.paste(tile, (x0 + (option_width - 150) // 2, 252))
    return board


def _prompt(choices: Sequence[tuple[LegendEntry, Path]]) -> str:
    lines = [
        "Сверху TARGET — найденный повторяющийся знак. Снизу варианты легенды.",
        "Разрешено выбрать только один из перечисленных ID либо unresolved.",
    ]
    for index, (entry, _) in enumerate(choices, start=1):
        lines.append(
            f"OPTION {index} = {entry.id} = {entry.name_raw or entry.name_normalized}"
        )
    lines.extend(
        [
            'Верни строго {"choice":"ID или unresolved","confidence":0.0}.',
            "Не добавляй Markdown и другие поля.",
        ]
    )
    return "\n".join(lines)


def _parse_choice(
    value: str | Mapping[str, Any],
    *,
    allowed_ids: set[str],
    minimum_confidence: float,
) -> tuple[str, float]:
    if isinstance(value, str):
        try:
            payload = json.loads(value.strip())
        except json.JSONDecodeError as exc:
            raise SymbolReviewError("H6 response is not strict JSON") from exc
    elif isinstance(value, Mapping):
        payload = value
    else:
        raise SymbolReviewError("H6 response must be an object")
    if not isinstance(payload, Mapping) or set(payload) != {"choice", "confidence"}:
        raise SymbolReviewError("H6 response has unexpected fields")
    choice = payload["choice"]
    confidence = payload["confidence"]
    if not isinstance(choice, str) or (
        choice != "unresolved" and choice not in allowed_ids
    ):
        raise SymbolReviewError("H6 returned an unknown legend entry ID")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise SymbolReviewError("H6 confidence must be between 0 and 1")
    confidence = float(confidence)
    if choice != "unresolved" and confidence < minimum_confidence:
        raise SymbolReviewError("H6 selected a choice with insufficient confidence")
    return choice, confidence


def _cache_digest(
    config: SymbolReviewVlmConfig,
    symbol_type: SymbolType,
    type_crop: Path,
    choices: Sequence[tuple[LegendEntry, Path]],
) -> str:
    digest = hashlib.sha256()
    digest.update(PROMPT_VERSION.encode())
    digest.update(config.model.encode())
    digest.update((config.provider or "").encode())
    digest.update(symbol_type.id.encode())
    digest.update(type_crop.read_bytes())
    for entry, path in choices:
        digest.update(entry.id.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _selected_types(
    artifacts: PageArtifacts,
    config: SymbolReviewVlmConfig,
) -> list[SymbolType]:
    eligible = [
        item
        for item in artifacts.symbol_types
        if item.status in {"conflicting", "unresolved", "unclassified"}
        and len(item.instance_ids) >= config.minimum_repeats
        and item.candidate_legend_entry_ids
    ]
    return sorted(
        eligible,
        key=lambda item: (
            0 if item.status == "conflicting" else 1,
            -len(item.instance_ids),
            item.id,
        ),
    )[: config.max_calls_per_page]


def review_page_symbols(
    artifacts: PageArtifacts,
    *,
    output_root: str | Path,
    config: SymbolReviewVlmConfig,
    backend: SymbolReviewBackend | None = None,
) -> tuple[PageArtifacts, dict[str, int]]:
    """Review bounded ambiguities and persist only validated probable choices."""

    if not config.enabled:
        return artifacts, {}
    page_dir = page_sidecar_dir(output_root, artifacts.page)
    trace_path = page_dir / "vlm_review_trace.json"
    cache_dir = page_dir / "vlm_review_cache"
    entries = {entry.id: entry for entry in artifacts.legend_entries}
    members_by_type: dict[str, list[SymbolInstance]] = {}
    for instance in artifacts.symbol_instances:
        if instance.symbol_type_id:
            members_by_type.setdefault(instance.symbol_type_id, []).append(instance)
    selected = _selected_types(artifacts, config)
    metrics = {
        "eligibleTypeCount": len(selected),
        "callCount": 0,
        "cacheHitCount": 0,
        "resolvedTypeCount": 0,
        "rejectedResponseCount": 0,
    }
    trace: dict[str, Any] = {
        "schemaVersion": 1,
        "promptVersion": PROMPT_VERSION,
        "page": artifacts.page,
        "results": [],
    }
    provider = backend
    type_updates: dict[str, SymbolType] = {}
    instance_updates: dict[str, SymbolInstance] = {}
    resolved_conflict_type_ids: set[str] = set()

    for symbol_type in selected:
        candidate_entries = [
            entries[entry_id]
            for entry_id in symbol_type.candidate_legend_entry_ids[
                : config.max_choices_per_type
            ]
            if entry_id in entries and entries[entry_id].status != "text_unreadable"
        ]
        if not candidate_entries:
            continue
        try:
            type_crop = _safe_crop_path(page_dir, symbol_type.representative_crop)
            choices = [
                (
                    entry,
                    _safe_crop_path(
                        page_dir,
                        entry.normalized_crop or entry.raw_crop,
                    ),
                )
                for entry in candidate_entries
            ]
            digest = _cache_digest(
                config,
                symbol_type,
                type_crop,
                choices,
            )
            cache_path = cache_dir / f"{digest}.json"
            allowed_ids = {entry.id for entry, _ in choices}
            if cache_path.exists():
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                choice, confidence = _parse_choice(
                    cached,
                    allowed_ids=allowed_ids,
                    minimum_confidence=config.minimum_confidence,
                )
                metrics["cacheHitCount"] += 1
                source = "cache"
            else:
                if provider is None:
                    provider = _HuggingFaceReviewBackend(config)
                response = provider.choose(
                    _review_board(type_crop, choices),
                    _prompt(choices),
                )
                metrics["callCount"] += 1
                choice, confidence = _parse_choice(
                    response,
                    allowed_ids=allowed_ids,
                    minimum_confidence=config.minimum_confidence,
                )
                atomic_write_json(
                    cache_path,
                    {"choice": choice, "confidence": confidence},
                )
                source = "provider"
            trace["results"].append(
                {
                    "symbolTypeId": symbol_type.id,
                    "candidateLegendEntryIds": sorted(allowed_ids),
                    "choice": choice,
                    "confidence": confidence,
                    "source": source,
                    "cacheKey": digest,
                }
            )
            if choice == "unresolved":
                continue
            evidence = ClassificationEvidence(
                kind="vlm_review",
                score=confidence,
                source_entity_ids=(symbol_type.id, choice),
                detail=(
                    "VLM selected one existing current-document legend option; "
                    "this evidence is non-confirming"
                ),
            )
            type_updates[symbol_type.id] = replace(
                symbol_type,
                status="probable",
                matched_legend_entry_id=choice,
                classification_evidence=(
                    *symbol_type.classification_evidence,
                    evidence,
                ),
            )
            for instance in members_by_type.get(symbol_type.id, []):
                instance_updates[instance.id] = replace(
                    instance,
                    status="probable",
                    legend_entry_id=choice,
                    classification_confidence=max(
                        instance.classification_confidence,
                        confidence,
                    ),
                    classification_evidence=(
                        *instance.classification_evidence,
                        evidence,
                    ),
                )
            resolved_conflict_type_ids.add(symbol_type.id)
            metrics["resolvedTypeCount"] += 1
        except (OSError, ValueError, json.JSONDecodeError, SymbolReviewError) as exc:
            metrics["rejectedResponseCount"] += 1
            trace["results"].append(
                {
                    "symbolTypeId": symbol_type.id,
                    "status": "rejected",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    reviewed_types = [
        type_updates.get(item.id, item)
        for item in sorted(artifacts.symbol_types, key=lambda item: item.id)
    ]
    reviewed_instances = [
        instance_updates.get(item.id, item)
        for item in sorted(artifacts.symbol_instances, key=lambda item: item.id)
    ]
    reviewed_conflicts = [
        replace(
            conflict,
            status="resolved",
            reason=conflict.reason + "; resolved by bounded H6 VLM review",
        )
        if any(
            entity_id in resolved_conflict_type_ids
            for entity_id in conflict.entity_ids
        )
        else conflict
        for conflict in artifacts.conflicts
    ]
    reviewed = PageArtifacts(
        page=artifacts.page,
        legend_entries=artifacts.legend_entries,
        symbol_candidates=artifacts.symbol_candidates,
        symbol_types=reviewed_types,
        symbol_instances=reviewed_instances,
        unclassified_symbols=[
            item
            for item in reviewed_instances
            if item.status in {"unclassified", "unresolved"}
        ],
        unmatched_legend_entries=artifacts.unmatched_legend_entries,
        conflicts=reviewed_conflicts,
        summary=artifacts.summary,
    )
    write_page_artifacts(output_root, reviewed)
    trace["metrics"] = metrics
    trace["backend"] = (
        dict(provider.trace_info)
        if provider is not None
        else {
            "backend": "cache-only",
            "model": config.model,
            "provider": config.provider,
        }
    )
    atomic_write_json(trace_path, trace)
    return reviewed, metrics
