from dataclasses import replace
import json
import math
from pathlib import Path
import tempfile
import unittest

from dwg_symbols.artifacts import load_page_result, write_page_result
from dwg_symbols.audit import audit_package
from dwg_symbols.circle_key import (
    circled_number,
    circle_heading,
    legend_display_line,
    parse_circle_block_name,
)
from dwg_symbols.cli import main as dwg_symbols_main
from dwg_symbols.ideal_md import (
    GAP_FOUNDATION,
    GAP_NOTES,
    GAP_SKV,
    GAP_TABLE,
    GAP_VIEWS,
    GAP_WELLS,
    UNREAD,
    WHOLE_SHEET_TITLE,
    render_ideal_markdown,
    write_ideal_md,
)
from dwg_symbols.blocks import block_signature
from dwg_symbols.context_resolver import context_terms, resolve_layer_context
from dwg_symbols.file_catalog import build_file_legend_catalog
from dwg_symbols.furniture import (
    ANNOTATION_FILTER_ANOMALY,
    ANONYMOUS_CONSTRUCTION_LAYER_REASON,
    AXIS_ATTRIBUTE_REASON,
    AXIS_LAYER_REASON,
    COLUMN_BLOCK_LAYER_REASON,
    FILTER_ANOMALY,
    FORMAT_STAMP_REASON,
    FURNITURE_BLOCK_LAYER_REASON,
    FRAME_COVERAGE_THRESHOLD,
    GEOLOGY_NO_JOIN_REASON,
    MARK_FILTER_ANOMALY,
    OBJECT_FILTER_ANOMALY,
    PAPER_FRAME_REASON,
    ROOM_NUMBER_BLOCK_REASON,
    HEIGHT_MARK_BLOCK_REASON,
    BUILDING_LABEL_BLOCK_REASON,
    SERVICE_LAYER_REASON,
    SIGNATURE_BLOCK_REASON,
    STAMP_ATTRIBUTES_REASON,
    TRAP_BLOCK_LAYER_REASON,
    WELD_GOST_REASON,
    classify_sheet_furniture,
    recount_non_symbols,
)
from dwg_symbols.symbol_catalog import catalog_symbol_types
from dwg_symbols.harness import HarnessError, score_fixture, validate_manifest
from dwg_symbols.geometry_resolver import closed_profile, resolve_geometry_profiles
from dwg_symbols.gost_welds import load_weld_table, reset_weld_table
from dwg_symbols.html_report import write_html_reports
from dwg_symbols.legends import (
    _crop_primitives,
    attach_legend_notes_zones,
    detect_legend_entries,
    has_legend_heading,
    has_notes_heading,
)
from dwg_symbols.project_catalog import build_project_legend_catalog
from dwg_symbols.project_application import (
    apply_project_catalog_to_page_dir,
    build_application_summary,
)
from dwg_symbols.project_resolver import resolve_project_exact_blocks
from dwg_symbols.resolver import resolve_exact_blocks
from dwg_symbols.field_geometry import attach_field_geometry
from dwg_symbols.review import (
    _ANNOTATION_COLOR,
    _FURNITURE_COLOR,
    _GEOMETRY_COLOR,
    _MARK_COLOR,
    _OBJECT_COLOR,
    _TEXT_LABEL_COLOR,
    _STAMP_HEIGHT_MM,
    _STAMP_WIDTH_MM,
    _instance_bbox,
    _overlay_svg,
    _review_counts,
    _review_instances,
    summarize_reviews,
)
from dwg_symbols.schema import (
    Evidence,
    FieldGeometry,
    LegendEntry,
    NoteText,
    PageResult,
    Point,
    SchemaError,
    SheetScene,
    SymbolBinding,
    SymbolInstance,
    TextLabel,
    UnknownSymbolCluster,
    stable_id,
)
from dwg_symbols.title_block import (
    attach_title_block,
    is_title_block_insert,
    read_title_block,
)
from dwg_symbols.dimension_read import attach_dimensions, read_dimension
from dwg_symbols.axis_read import attach_axes, read_axis
from dwg_symbols.text_labels import attach_text_labels
from dwg_symbols.sheet_scenes import attach_sheet_scenes, is_view_title
from dwg_symbols.schedule_join import (
    NOTE_ROW_MISSING,
    NOTE_TABLE_CONFLICT,
    NOTE_TABLE_MISSING,
    attach_schedule_notes,
)
from dwg_sheets import TextItem


def _instance() -> SymbolInstance:
    return SymbolInstance(
        id="SI-test",
        page=1,
        source_kind="dwg_insert_candidate",
        status="unresolved",
        position=Point(10.0, 20.0, "paper", "mm"),
        source_handle="A1",
        source_space="modelspace",
        layer="EQUIPMENT",
        signature="blockdef-v1:abc",
        block_name="VALVE",
        attributes={"TAG": "V-1"},
        confidence=1.0,
    )


class SchemaTests(unittest.TestCase):
    def test_stable_id_is_reproducible(self) -> None:
        self.assertEqual(stable_id("SI", 1, "A"), stable_id("SI", 1, "A"))
        self.assertNotEqual(stable_id("SI", 1, "A"), stable_id("SI", 1, "B"))

    def test_missing_block_definition_has_stable_fallback_signature(self) -> None:
        class Blocks:
            @staticmethod
            def get(_name):
                return None

        class Document:
            blocks = Blocks()

        self.assertEqual(
            block_signature(Document(), "MISSING"),
            block_signature(Document(), "MISSING"),
        )

    def test_unresolved_instance_cannot_reference_legend(self) -> None:
        data = _instance().to_dict()
        with self.assertRaisesRegex(SchemaError, "cannot have legendEntryId"):
            SymbolInstance(
                id=data["id"],
                page=1,
                source_kind=data["sourceKind"],
                status="unresolved",
                position=Point(0, 0, "paper", "mm"),
                source_handle=data["sourceHandle"],
                source_space=data["sourceSpace"],
                layer=data["layer"],
                signature=data["signature"],
                legend_entry_id="LE-test",
            )

    def test_page_sidecars_include_empty_future_stages(self) -> None:
        instance = _instance()
        cluster = UnknownSymbolCluster(
            id="US-test",
            page=1,
            signature=instance.signature,
            instance_ids=(instance.id,),
            reason="NO_LEGEND_BINDING_BASELINE",
            representative_instance_id=instance.id,
        )
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            symbol_instances=[instance],
            unknown_symbols=[cluster],
        )
        with tempfile.TemporaryDirectory() as directory:
            page_dir = write_page_result(directory, result)
            expected = {
                "legend_entries.json",
                "symbol_instances.json",
                "symbol_bindings.json",
                "unrecognized_symbols.json",
                "relationships.json",
                "text_labels.json",
                "notes_texts.json",
                "sheet_scenes.json",
                "field_geometry.json",
                "summary.json",
            }
            self.assertEqual(expected, {path.name for path in page_dir.iterdir()})
            bindings = json.loads(
                (page_dir / "symbol_bindings.json").read_text(encoding="utf-8")
            )
            self.assertEqual([], bindings["items"])
            summary = json.loads((page_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertIsNone(summary["titleBlock"])
            self.assertIsNone(summary["sheetZones"])

    def test_unresolved_instance_requires_unknown_cluster(self) -> None:
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="partial",
            symbol_instances=[_instance()],
        )
        with self.assertRaisesRegex(SchemaError, "require unknown clusters"):
            result.validate()

    def test_legend_entry_serializes_crop_evidence(self) -> None:
        entry = LegendEntry(
            id="LE-test",
            page=1,
            label="Valve",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature="legend-vector-v1:abc",
            bbox=(1, 2, 8, 9),
            symbol_bbox=(1, 2, 4, 9),
            crop_path="legend_crops/LE-test.svg",
            source_texts=("Valve",),
            confidence=0.9,
        )
        payload = entry.to_dict()
        self.assertEqual([1.0, 2.0, 4.0, 9.0], payload["symbolBbox"])
        self.assertEqual("legend_crops/LE-test.svg", payload["cropPath"])

    def test_sheet_furniture_serializes_ignored_status_and_reason(self) -> None:
        instance = SymbolInstance(
            id="SI-stamp",
            page=1,
            source_kind="dwg_insert_candidate",
            status="ignored",
            position=Point(0, 0, "paper", "mm"),
            source_handle="STAMP",
            source_space="modelspace:viewport:0",
            layer="FORMAT",
            signature="blockdef-v1:stamp",
            block_name="*U704",
            role="sheet_furniture",
            attributes={"ЛИСТ": "1", "СТАДИЯ": "Р"},
            classification_reason="FORMAT_STAMP_ATTRIBUTES",
        )
        payload = instance.to_dict()
        self.assertEqual("ignored", payload["status"])
        self.assertEqual("sheet_furniture", payload["role"])
        self.assertIsNone(payload["legendEntryId"])
        self.assertEqual("FORMAT_STAMP_ATTRIBUTES", payload["classificationReason"])

    def test_ignored_instance_cannot_reference_legend(self) -> None:
        with self.assertRaisesRegex(SchemaError, "cannot have legendEntryId"):
            SymbolInstance(
                id="SI-stamp",
                page=1,
                source_kind="dwg_insert_candidate",
                status="ignored",
                position=Point(0, 0, "paper", "mm"),
                source_handle="STAMP",
                source_space="modelspace",
                layer="FORMAT",
                signature="blockdef-v1:stamp",
                role="sheet_furniture",
                legend_entry_id="LE-test",
            )

    def test_sheet_furniture_requires_ignored_status(self) -> None:
        with self.assertRaisesRegex(SchemaError, "must have ignored status"):
            SymbolInstance(
                id="SI-stamp",
                page=1,
                source_kind="dwg_insert_candidate",
                status="unresolved",
                position=Point(0, 0, "paper", "mm"),
                source_handle="STAMP",
                source_space="modelspace",
                layer="FORMAT",
                signature="blockdef-v1:stamp",
                role="sheet_furniture",
            )

    def test_specification_mark_requires_ignored_status(self) -> None:
        with self.assertRaisesRegex(SchemaError, "must have ignored status"):
            SymbolInstance(
                id="SI-axis",
                page=1,
                source_kind="dwg_insert_candidate",
                status="unresolved",
                position=Point(0, 0, "paper", "mm"),
                source_handle="AXIS",
                source_space="modelspace",
                layer="OSI",
                signature="blockdef-v1:axis",
                role="specification_mark",
            )

    def test_ignored_status_requires_non_symbol_role(self) -> None:
        with self.assertRaisesRegex(SchemaError, "requires a non-symbol role"):
            SymbolInstance(
                id="SI-stamp",
                page=1,
                source_kind="dwg_insert_candidate",
                status="ignored",
                position=Point(0, 0, "paper", "mm"),
                source_handle="STAMP",
                source_space="modelspace",
                layer="FORMAT",
                signature="blockdef-v1:stamp",
                role="field_candidate",
            )

    def test_confirmed_instance_cannot_be_sheet_furniture(self) -> None:
        with self.assertRaisesRegex(SchemaError, "must have ignored status"):
            SymbolInstance(
                id="SI-confirmed",
                page=1,
                source_kind="dwg_insert_candidate",
                status="confirmed",
                position=Point(0, 0, "paper", "mm"),
                source_handle="A1",
                source_space="modelspace",
                layer="SYMBOLS",
                signature="blockdef-v1:abc",
                role="sheet_furniture",
                legend_entry_id="LE-test",
            )

    def test_probable_instance_cannot_be_sheet_furniture(self) -> None:
        with self.assertRaisesRegex(SchemaError, "must have ignored status"):
            SymbolInstance(
                id="SI-probable",
                page=1,
                source_kind="dwg_insert_candidate",
                status="probable",
                position=Point(0, 0, "paper", "mm"),
                source_handle="A1",
                source_space="modelspace",
                layer="SYMBOLS",
                signature="blockdef-v1:abc",
                role="sheet_furniture",
                legend_entry_id="LE-test",
            )

    def test_blank_classification_reason_is_rejected(self) -> None:
        with self.assertRaisesRegex(SchemaError, "classificationReason"):
            SymbolInstance(
                id="SI-stamp",
                page=1,
                source_kind="dwg_insert_candidate",
                status="ignored",
                position=Point(0, 0, "paper", "mm"),
                source_handle="STAMP",
                source_space="modelspace",
                layer="FORMAT",
                signature="blockdef-v1:stamp",
                role="sheet_furniture",
                classification_reason="   ",
            )

    def test_ignored_sheet_furniture_is_excluded_from_unknown_symbols(self) -> None:
        candidate = _instance()
        furniture = SymbolInstance(
            id="SI-stamp",
            page=1,
            source_kind="dwg_insert_candidate",
            status="ignored",
            position=Point(0, 0, "paper", "mm"),
            source_handle="STAMP",
            source_space="modelspace:viewport:0",
            layer="FORMAT",
            signature="blockdef-v1:stamp",
            role="sheet_furniture",
            classification_reason="FORMAT_STAMP_ATTRIBUTES",
        )
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            symbol_instances=[candidate, furniture],
            unknown_symbols=[
                UnknownSymbolCluster(
                    id="US-test",
                    page=1,
                    signature=candidate.signature,
                    instance_ids=(candidate.id,),
                    reason="NO_LEGEND_BINDING_BASELINE",
                    representative_instance_id=candidate.id,
                )
            ],
        )
        result.validate()

    def test_unknown_cluster_rejects_sheet_furniture(self) -> None:
        furniture = SymbolInstance(
            id="SI-stamp",
            page=1,
            source_kind="dwg_insert_candidate",
            status="ignored",
            position=Point(0, 0, "paper", "mm"),
            source_handle="STAMP",
            source_space="modelspace",
            layer="FORMAT",
            signature="blockdef-v1:stamp",
            role="sheet_furniture",
        )
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            symbol_instances=[furniture],
            unknown_symbols=[
                UnknownSymbolCluster(
                    id="US-stamp",
                    page=1,
                    signature=furniture.signature,
                    instance_ids=(furniture.id,),
                    reason="NO_LEGEND_BINDING_BASELINE",
                    representative_instance_id=furniture.id,
                )
            ],
        )
        with self.assertRaisesRegex(SchemaError, "non-symbol instances"):
            result.validate()

    def test_furniture_only_page_does_not_require_unknown_clusters(self) -> None:
        furniture = SymbolInstance(
            id="SI-stamp",
            page=1,
            source_kind="dwg_insert_candidate",
            status="ignored",
            position=Point(0, 0, "paper", "mm"),
            source_handle="STAMP",
            source_space="modelspace:viewport:0",
            layer="FORMAT",
            signature="blockdef-v1:stamp",
            role="sheet_furniture",
            classification_reason="FORMAT_STAMP_ATTRIBUTES",
        )
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            symbol_instances=[furniture],
        )
        result.validate()
        summary = result.summary_dict()
        self.assertEqual({"ignored": 1}, summary["instanceStatuses"])
        self.assertEqual({"sheet_furniture": 1}, summary["instanceRoles"])
        self.assertEqual(0, summary["counts"]["unknownClusters"])
        self.assertIsNone(summary["titleBlock"])
        self.assertIsNone(summary["sheetZones"])

    def test_untyped_field_candidate_still_requires_unknown_cluster(self) -> None:
        furniture = SymbolInstance(
            id="SI-stamp",
            page=1,
            source_kind="dwg_insert_candidate",
            status="ignored",
            position=Point(0, 0, "paper", "mm"),
            source_handle="STAMP",
            source_space="modelspace",
            layer="FORMAT",
            signature="blockdef-v1:stamp",
            role="sheet_furniture",
        )
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="partial",
            symbol_instances=[_instance(), furniture],
        )
        with self.assertRaisesRegex(SchemaError, "require unknown clusters"):
            result.validate()

    def test_summary_writes_title_block_and_sheet_zones(self) -> None:
        furniture = SymbolInstance(
            id="SI-stamp",
            page=1,
            source_kind="dwg_insert_candidate",
            status="ignored",
            position=Point(830.0, 10.0, "paper", "mm"),
            source_handle="STAMP",
            source_space="modelspace:viewport:0",
            layer="FORMAT",
            signature="blockdef-v1:stamp",
            role="sheet_furniture",
            classification_reason="FORMAT_STAMP_ATTRIBUTES",
            attributes={"ЛИСТ": "1", "СТАДИЯ": "П", "ЛИСТОВ": "8"},
        )
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            symbol_instances=[furniture],
            title_block={
                "code": "",
                "sheet": "1",
                "sheetsTotal": "8",
                "stage": "П",
                "source": "attributes",
                "note": "шифр не прочитан",
                "instanceId": furniture.id,
            },
            sheet_zones={
                "title_block": [645.0, 10.0, 830.0, 65.0],
                "drawing_field": [0.0, 65.0, 841.0, 594.0],
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            page_dir = write_page_result(directory, result)
            summary = json.loads((page_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual("1", summary["titleBlock"]["sheet"])
        self.assertEqual("8", summary["titleBlock"]["sheetsTotal"])
        self.assertEqual("П", summary["titleBlock"]["stage"])
        self.assertEqual("attributes", summary["titleBlock"]["source"])
        self.assertEqual("шифр не прочитан", summary["titleBlock"]["note"])
        self.assertEqual(furniture.id, summary["titleBlock"]["instanceId"])
        self.assertEqual("", summary["titleBlock"]["code"])
        self.assertEqual(
            [645.0, 10.0, 830.0, 65.0],
            summary["sheetZones"]["title_block"],
        )
        self.assertEqual(
            [0.0, 65.0, 841.0, 594.0],
            summary["sheetZones"]["drawing_field"],
        )

    def test_title_block_instance_must_exist(self) -> None:
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            title_block={"instanceId": "SI-missing", "source": "attributes"},
        )
        with self.assertRaisesRegex(SchemaError, "unknown instance"):
            result.validate()

    def test_title_block_rejects_geometry_source_aliases(self) -> None:
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            title_block={"source": "DWG"},
        )
        with self.assertRaisesRegex(SchemaError, "titleBlock.source"):
            result.validate()

    def test_sheet_zones_require_title_block_bbox(self) -> None:
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            sheet_zones={"drawing_field": [0.0, 0.0, 841.0, 594.0]},
        )
        with self.assertRaisesRegex(SchemaError, "title_block"):
            result.validate()

    def test_sheet_zones_accept_optional_legend_and_notes(self) -> None:
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            sheet_zones={
                "title_block": [656.0, 0.0, 841.0, 55.0],
                "drawing_field": [0.0, 55.0, 841.0, 594.0],
                "legend": [40.0, 80.0, 220.0, 210.0],
                "notes": [40.0, 40.0, 220.0, 75.0],
            },
        )
        result.validate()
        self.assertEqual([40.0, 80.0, 220.0, 210.0], result.sheet_zones["legend"])
        self.assertEqual([40.0, 40.0, 220.0, 75.0], result.sheet_zones["notes"])

    def test_sheet_zones_reject_unknown_key(self) -> None:
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            sheet_zones={
                "title_block": [656.0, 0.0, 841.0, 55.0],
                "viewport": [0.0, 0.0, 100.0, 100.0],
            },
        )
        with self.assertRaisesRegex(SchemaError, "unsupported sheetZones key"):
            result.validate()

    def test_attach_title_block_after_furniture_fills_summary(self) -> None:
        stamp = SymbolInstance(
            id="SI-stamp",
            page=1,
            source_kind="dwg_insert_candidate",
            status="unresolved",
            position=Point(830.0, 10.0, "paper", "mm"),
            source_handle="STAMP",
            source_space="modelspace:viewport:0",
            layer="FORMAT",
            signature="blockdef-v1:stamp",
            block_name="*U704",
            attributes={
                "ЛИСТ": "1",
                "СТАДИЯ": "П",
                "ЛИСТОВ": "8",
                "ФОРМАТ": "А1",
            },
            confidence=1.0,
        )
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="partial",
            symbol_instances=[stamp],
            unknown_symbols=[
                UnknownSymbolCluster(
                    id="US-stamp",
                    page=1,
                    signature=stamp.signature,
                    instance_ids=(stamp.id,),
                    reason="NO_LEGEND_BINDING_BASELINE",
                    representative_instance_id=stamp.id,
                )
            ],
        )
        result = classify_sheet_furniture(result, sheet_bbox=(0.0, 0.0, 841.0, 594.0))
        result = attach_title_block(
            result,
            texts=[],
            sheet_bbox=(0.0, 0.0, 841.0, 594.0),
        )
        summary = result.summary_dict()
        self.assertEqual("1", summary["titleBlock"]["sheet"])
        self.assertEqual("8", summary["titleBlock"]["sheetsTotal"])
        self.assertEqual("П", summary["titleBlock"]["stage"])
        self.assertEqual("attributes", summary["titleBlock"]["source"])
        self.assertEqual("шифр не прочитан", summary["titleBlock"]["note"])
        self.assertEqual("SI-stamp", summary["titleBlock"]["instanceId"])
        self.assertIn("title_block", summary["sheetZones"])
        self.assertIn("drawing_field", summary["sheetZones"])
        self.assertEqual(4, len(summary["sheetZones"]["title_block"]))
        self.assertEqual(4, len(summary["sheetZones"]["drawing_field"]))


class FurnitureTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_weld_table()

    @staticmethod
    def _insert(
        instance_id: str,
        *,
        layer: str = "EQUIPMENT",
        block_name: str | None = "VALVE",
        status: str = "unresolved",
        role: str = "field_candidate",
        source_space: str = "modelspace:viewport:0",
        space: str = "paper",
        attributes: dict[str, str] | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        signature: str | None = None,
        legend_entry_id: str | None = None,
        x: float = 10.0,
        y: float = 20.0,
        classification_reason: str | None = None,
    ) -> SymbolInstance:
        return SymbolInstance(
            id=instance_id,
            page=1,
            source_kind="dwg_insert_candidate",
            status=status,
            position=Point(x, y, space, "mm"),
            source_handle=instance_id.replace("SI-", ""),
            source_space=source_space,
            layer=layer,
            signature=signature or f"blockdef-v1:{block_name or instance_id}",
            block_name=block_name,
            role=role,
            bbox=bbox,
            attributes=attributes or {},
            legend_entry_id=legend_entry_id,
            confidence=1.0,
            classification_reason=classification_reason,
        )

    @staticmethod
    def _page(
        instances: list[SymbolInstance],
        *,
        legends: list[LegendEntry] | None = None,
        bindings: list[SymbolBinding] | None = None,
    ) -> PageResult:
        grouped: dict[str, list[SymbolInstance]] = {}
        for instance in instances:
            if instance.role == "field_candidate" and instance.status in {
                "unresolved",
                "unclassified",
            }:
                grouped.setdefault(instance.signature, []).append(instance)
        return PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="partial",
            legend_entries=list(legends or []),
            symbol_instances=list(instances),
            symbol_bindings=list(bindings or []),
            unknown_symbols=[
                UnknownSymbolCluster(
                    id=stable_id("US", signature),
                    page=1,
                    signature=signature,
                    instance_ids=tuple(item.id for item in items),
                    reason="NO_LEGEND_BINDING_BASELINE",
                    representative_instance_id=items[0].id,
                )
                for signature, items in grouped.items()
            ],
        )

    def _write_weld_table(self, directory: str | Path, welds: list[dict]) -> None:
        path = Path(directory) / "gost_welds.json"
        path.write_text(
            json.dumps({"schemaVersion": 1, "welds": welds}, ensure_ascii=False),
            encoding="utf-8",
        )
        reset_weld_table()
        load_weld_table(path)

    def test_format_stamp_attributes_are_ignored_sheet_furniture(self) -> None:
        stamp = self._insert(
            "SI-stamp",
            layer="Format",
            block_name="*U704",
            attributes={
                "ЛИСТ": "1",
                "СТАДИЯ": "Р",
                "ФОРМАТ": "А1",
                "ГИП": "Иванов",
            },
        )
        result = classify_sheet_furniture(self._page([stamp]))
        classified = result.symbol_instances[0]
        self.assertEqual("ignored", classified.status)
        self.assertEqual("sheet_furniture", classified.role)
        self.assertEqual(FORMAT_STAMP_REASON, classified.classification_reason)
        self.assertIsNone(classified.legend_entry_id)
        self.assertIn(FILTER_ANOMALY, result.anomaly_codes)
        self.assertEqual([], result.unknown_symbols)
        result.validate()

    def test_trap_on_technology_layer_is_drawing_object(self) -> None:
        trap = self._insert(
            "SI-trap",
            layer="Технология",
            block_name="трап100",
            attributes={"МАРКА": "Т1"},
        )
        result = classify_sheet_furniture(self._page([trap]))
        classified = result.symbol_instances[0]
        self.assertEqual("ignored", classified.status)
        self.assertEqual("drawing_object", classified.role)
        self.assertEqual(TRAP_BLOCK_LAYER_REASON, classified.classification_reason)
        self.assertIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)
        self.assertEqual([], result.unknown_symbols)

    def test_trap_on_foreign_layer_stays_field_candidate(self) -> None:
        trap = self._insert(
            "SI-trap",
            layer="0",
            block_name="трап100",
            attributes={"МАРКА": "Т1"},
        )
        result = classify_sheet_furniture(self._page([trap]))
        classified = result.symbol_instances[0]
        self.assertEqual("unresolved", classified.status)
        self.assertEqual("field_candidate", classified.role)
        self.assertIsNone(classified.classification_reason)
        self.assertNotIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)
        self.assertEqual((trap.id,), result.unknown_symbols[0].instance_ids)

    def test_column_block_and_layer_is_drawing_object(self) -> None:
        column = self._insert(
            "SI-column",
            layer="колонна",
            block_name="Колонна",
        )
        result = classify_sheet_furniture(self._page([column]))
        classified = result.symbol_instances[0]
        self.assertEqual("ignored", classified.status)
        self.assertEqual("drawing_object", classified.role)
        self.assertEqual(COLUMN_BLOCK_LAYER_REASON, classified.classification_reason)
        self.assertIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)
        self.assertNotIn(FILTER_ANOMALY, result.anomaly_codes)
        self.assertEqual([], result.unknown_symbols)

    def test_column_on_layer_zero_stays_field_candidate(self) -> None:
        column = self._insert(
            "SI-column",
            layer="0",
            block_name="Колонна",
        )
        result = classify_sheet_furniture(self._page([column]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertIsNone(classified.classification_reason)
        self.assertNotIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)

    def test_paper_space_without_format_or_stamp_attrs_is_not_filtered(self) -> None:
        legend_like = self._insert(
            "SI-legend",
            layer="0",
            block_name="*U100",
            source_space="layout:Лист1",
            space="paper",
            x=180.0,
            y=20.0,
        )
        result = classify_sheet_furniture(self._page([legend_like]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertNotIn(FILTER_ANOMALY, result.anomaly_codes)

    def test_format_without_stamp_attrs_or_large_bbox_is_fail_closed(self) -> None:
        anonymous = self._insert(
            "SI-anon",
            layer="FORMAT",
            block_name="*U704",
        )
        result = classify_sheet_furniture(self._page([anonymous]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertIsNone(classified.classification_reason)
        self.assertNotIn(FILTER_ANOMALY, result.anomaly_codes)

    def test_paper_frame_coverage_threshold_is_085(self) -> None:
        self.assertEqual(0.85, FRAME_COVERAGE_THRESHOLD)
        sheet = (0.0, 0.0, 100.0, 100.0)
        covering = self._insert(
            "SI-frame",
            layer="FRAME",
            block_name="RAMKA",
            space="paper",
            bbox=(0.0, 0.0, 100.0, 85.0),
        )
        covered = classify_sheet_furniture(
            self._page([covering]),
            sheet_bbox=sheet,
        )
        self.assertEqual("ignored", covered.symbol_instances[0].status)
        self.assertEqual("sheet_furniture", covered.symbol_instances[0].role)
        self.assertEqual(
            PAPER_FRAME_REASON,
            covered.symbol_instances[0].classification_reason,
        )

        undersized = self._insert(
            "SI-small-frame",
            layer="FRAME",
            block_name="RAMKA",
            space="paper",
            bbox=(0.0, 0.0, 100.0, 84.0),
        )
        skipped = classify_sheet_furniture(
            self._page([undersized]),
            sheet_bbox=sheet,
        )
        self.assertEqual("field_candidate", skipped.symbol_instances[0].role)
        self.assertEqual("unresolved", skipped.symbol_instances[0].status)

    def test_confirmed_instance_is_not_reclassified(self) -> None:
        legend = LegendEntry(
            id="LE-1",
            page=1,
            label="Stamp-like",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature="legend-vector-v1:stamp",
            confidence=0.9,
        )
        confirmed = self._insert(
            "SI-confirmed",
            layer="FORMAT",
            block_name="*U704",
            status="confirmed",
            attributes={"ЛИСТ": "1", "ГИП": "Иванов"},
            legend_entry_id=legend.id,
        )
        binding = SymbolBinding(
            id="SB-1",
            page=1,
            instance_id=confirmed.id,
            legend_entry_id=legend.id,
            status="confirmed",
            confidence=1.0,
            evidence=(
                Evidence(
                    kind="exact_block_definition",
                    score=1.0,
                    source_ids=(legend.id,),
                    detail="already bound",
                ),
            ),
        )
        original = self._page(
            [confirmed],
            legends=[legend],
            bindings=[binding],
        )
        result = classify_sheet_furniture(original)
        classified = result.symbol_instances[0]
        self.assertIs(result, original)
        self.assertEqual("confirmed", classified.status)
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual(legend.id, classified.legend_entry_id)
        self.assertNotIn(FILTER_ANOMALY, result.anomaly_codes)

    def test_legend_exemplar_is_not_touched(self) -> None:
        legend = LegendEntry(
            id="LE-1",
            page=1,
            label="Legend row",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature="legend-vector-v1:row",
            confidence=0.9,
        )
        exemplar = self._insert(
            "SI-exemplar",
            layer="FORMAT",
            block_name="*U50",
            status="reference",
            role="legend_exemplar",
            source_space="layout:Лист1",
            attributes={"ЛИСТ": "1", "СТАДИЯ": "Р"},
            legend_entry_id=legend.id,
        )
        original = self._page([exemplar], legends=[legend])
        result = classify_sheet_furniture(original)
        classified = result.symbol_instances[0]
        self.assertIs(result, original)
        self.assertEqual("reference", classified.status)
        self.assertEqual("legend_exemplar", classified.role)
        self.assertEqual(legend.id, classified.legend_entry_id)

    def test_filter_keeps_untyped_candidates_in_unknown_not_furniture(self) -> None:
        stamp = self._insert(
            "SI-stamp",
            layer="FORMAT",
            block_name="*U704",
            attributes={"ЛИСТ": "1", "Н.контр": "Петров"},
        )
        trap = self._insert(
            "SI-trap",
            layer="Технология",
            block_name="трап100",
        )
        column = self._insert(
            "SI-column",
            layer="колонна",
            block_name="Колонна",
        )
        slope = self._insert(
            "SI-slope",
            layer="0",
            block_name="уклон",
        )
        result = classify_sheet_furniture(self._page([stamp, trap, column, slope]))
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertEqual("sheet_furniture", by_id["SI-stamp"].role)
        self.assertEqual("ignored", by_id["SI-stamp"].status)
        self.assertEqual("drawing_object", by_id["SI-trap"].role)
        self.assertEqual(TRAP_BLOCK_LAYER_REASON, by_id["SI-trap"].classification_reason)
        self.assertEqual("drawing_object", by_id["SI-column"].role)
        self.assertEqual("ignored", by_id["SI-column"].status)
        self.assertEqual("field_candidate", by_id["SI-slope"].role)
        clustered = {
            instance_id
            for cluster in result.unknown_symbols
            for instance_id in cluster.instance_ids
        }
        self.assertEqual({"SI-slope"}, clustered)
        self.assertNotIn("SI-stamp", clustered)
        self.assertNotIn("SI-column", clustered)
        self.assertNotIn("SI-trap", clustered)
        result.validate()

    def test_review_counters_exclude_ignored_and_include_sheet_furniture(self) -> None:
        stamp = self._insert(
            "SI-stamp",
            layer="FORMAT",
            block_name="*U704",
            attributes={"ЛИСТ": "1", "СТАДИЯ": "Р"},
        )
        trap = self._insert(
            "SI-trap",
            layer="Технология",
            block_name="трап100",
        )
        result = classify_sheet_furniture(self._page([stamp, trap]))
        counts = _review_counts(result)
        self.assertEqual(0, counts["unrecognizedCandidates"])
        self.assertEqual(0, counts["unknownClusters"])
        self.assertEqual(0, counts["unknownOccurrences"])
        self.assertEqual(1, counts["sheetFurniture"])
        self.assertEqual(1, counts["sheetFurnitureClusters"])
        self.assertEqual(1, counts["drawingObjects"])
        self.assertEqual(0, counts["drawingAnnotations"])
        self.assertEqual(0, counts["specificationMarks"])
        self.assertEqual(0, counts["confirmed"])
        self.assertEqual(0, counts["probable"])
        ignored = [
            item
            for item in result.symbol_instances
            if item.status == "ignored"
        ]
        self.assertEqual(2, len(ignored))
        self.assertEqual(
            counts["unrecognizedCandidates"]
            + counts["sheetFurniture"]
            + counts["drawingObjects"],
            len(result.symbol_instances),
        )

    def test_layer_zero_stamp_with_two_attributes_is_furniture(self) -> None:
        stamp = self._insert(
            "SI-stamp",
            layer="0",
            block_name="*U74",
            attributes={"ДОЛЖНОСТЬ3": "ГИП", "ФОРМАТ": "А2"},
        )
        result = classify_sheet_furniture(self._page([stamp]))
        classified = result.symbol_instances[0]
        self.assertEqual("ignored", classified.status)
        self.assertEqual("sheet_furniture", classified.role)
        self.assertEqual(STAMP_ATTRIBUTES_REASON, classified.classification_reason)
        self.assertIn(FILTER_ANOMALY, result.anomaly_codes)
        self.assertEqual([], result.unknown_symbols)

    def test_single_stamp_attribute_without_format_is_fail_closed(self) -> None:
        stamp = self._insert(
            "SI-stamp",
            layer="0",
            block_name="*U74",
            attributes={"ЛИСТ": "1"},
        )
        result = classify_sheet_furniture(self._page([stamp]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertIsNone(classified.classification_reason)
        self.assertNotIn(FILTER_ANOMALY, result.anomaly_codes)

    def test_signature_block_on_signatures_layer_is_furniture(self) -> None:
        mark = self._insert(
            "SI-sign",
            layer="Подписи",
            block_name="подпись",
        )
        result = classify_sheet_furniture(self._page([mark]))
        classified = result.symbol_instances[0]
        self.assertEqual("ignored", classified.status)
        self.assertEqual("sheet_furniture", classified.role)
        self.assertEqual(SIGNATURE_BLOCK_REASON, classified.classification_reason)

    def test_osi_layer_is_specification_mark(self) -> None:
        axis = self._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U12",
            attributes={"Ось": "7", "Ось'": "Ж"},
        )
        result = classify_sheet_furniture(self._page([axis]))
        classified = result.symbol_instances[0]
        self.assertEqual("ignored", classified.status)
        self.assertEqual("specification_mark", classified.role)
        self.assertEqual(AXIS_LAYER_REASON, classified.classification_reason)
        self.assertIn(MARK_FILTER_ANOMALY, result.anomaly_codes)
        self.assertNotIn(ANNOTATION_FILTER_ANOMALY, result.anomaly_codes)
        self.assertEqual([], result.unknown_symbols)

    def test_axis_attribute_without_osi_layer_is_specification_mark(self) -> None:
        axis = self._insert(
            "SI-axis",
            layer="0",
            block_name="*U12",
            attributes={"Ось": "А"},
        )
        result = classify_sheet_furniture(self._page([axis]))
        classified = result.symbol_instances[0]
        self.assertEqual("specification_mark", classified.role)
        self.assertEqual(AXIS_ATTRIBUTE_REASON, classified.classification_reason)

    def test_razmer_layer_stays_drawing_annotation(self) -> None:
        dim = self._insert(
            "SI-dim",
            layer="RAZMER",
            block_name="*U382",
            attributes={"В": "1 (2)", "Г": "1 (6)"},
        )
        result = classify_sheet_furniture(self._page([dim]))
        classified = result.symbol_instances[0]
        self.assertEqual("ignored", classified.status)
        self.assertEqual("drawing_annotation", classified.role)
        self.assertEqual(SERVICE_LAYER_REASON, classified.classification_reason)
        self.assertIn(ANNOTATION_FILTER_ANOMALY, result.anomaly_codes)

    def test_marka_attribute_on_equipment_is_not_a_specification_mark(self) -> None:
        trap = self._insert(
            "SI-trap",
            layer="Оборудование",
            block_name="трап100",
            attributes={"МАРКА": "Т1"},
        )
        result = classify_sheet_furniture(self._page([trap]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertNotIn(MARK_FILTER_ANOMALY, result.anomaly_codes)
        self.assertNotIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)

    def test_svarka_layer_is_not_annotation(self) -> None:
        weld = self._insert(
            "SI-weld",
            layer="SVARKA",
            block_name="*U90",
        )
        result = classify_sheet_furniture(self._page([weld]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertNotIn(ANNOTATION_FILTER_ANOMALY, result.anomaly_codes)
        self.assertNotIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)

    def test_weld_signature_in_table_is_annotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._write_weld_table(
                directory,
                [
                    {
                        "signature": "blockdef-v1:butt",
                        "gostCode": "C2",
                        "label": "стыковой",
                    }
                ],
            )
            weld = self._insert(
                "SI-weld",
                layer="SVARKA",
                block_name="*U129",
                signature="blockdef-v1:butt",
            )
            neighbor = self._insert(
                "SI-other",
                layer="SVARKA",
                block_name="*U90",
                signature="blockdef-v1:other",
            )
            result = classify_sheet_furniture(self._page([weld, neighbor]))
        matched, leftover = result.symbol_instances
        self.assertEqual("drawing_annotation", matched.role)
        self.assertEqual(WELD_GOST_REASON, matched.classification_reason)
        self.assertEqual("ignored", matched.status)
        self.assertIn(ANNOTATION_FILTER_ANOMALY, result.anomaly_codes)
        self.assertEqual("field_candidate", leftover.role)
        self.assertEqual("unresolved", leftover.status)
        self.assertIsNone(leftover.classification_reason)

    def test_pending_weld_slice_stays_unknown(self) -> None:
        reset_weld_table()
        weld = self._insert(
            "SI-fs1",
            layer="SVARKA",
            block_name="ФС1_Есипово 3_стр3",
            signature=(
                "blockdef-v1:f9d0fcc0bd5e119d8dabacdf95843cbf12c18edcc7ab1bb372b36f4d772c7120"
            ),
        )
        result = classify_sheet_furniture(self._page([weld]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertIsNone(classified.classification_reason)

    def test_production_weld_table_has_no_enabled_codes(self) -> None:
        reset_weld_table()
        table = load_weld_table()
        self.assertGreaterEqual(len(table), 4)
        self.assertFalse(any(entry.enabled for entry in table.values()))

    def test_weld_table_does_not_apply_off_svarka_layer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._write_weld_table(
                directory,
                [{"signature": "blockdef-v1:butt", "gostCode": "C2", "label": "стыковой"}],
            )
            weld = self._insert(
                "SI-weld",
                layer="0",
                block_name="*U129",
                signature="blockdef-v1:butt",
            )
            result = classify_sheet_furniture(self._page([weld]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)

    def test_weld_match_does_not_move_column_trap_or_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self._write_weld_table(
                directory,
                [{"signature": "blockdef-v1:butt", "gostCode": "C2", "label": "стыковой"}],
            )
            stamp = self._insert(
                "SI-stamp",
                layer="FORMAT",
                block_name="*U704",
                attributes={"ЛИСТ": "1", "СТАДИЯ": "П", "ГИП": "Иванов"},
            )
            column = self._insert(
                "SI-column",
                layer="колонна",
                block_name="Колонна",
            )
            trap = self._insert(
                "SI-trap",
                layer="Технология",
                block_name="трап100",
            )
            weld = self._insert(
                "SI-weld",
                layer="Сварка",
                block_name="*U129",
                signature="blockdef-v1:butt",
            )
            result = classify_sheet_furniture(
                self._page([stamp, column, trap, weld])
            )
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertEqual("sheet_furniture", by_id["SI-stamp"].role)
        self.assertEqual("drawing_object", by_id["SI-column"].role)
        self.assertEqual("drawing_object", by_id["SI-trap"].role)
        self.assertEqual("drawing_annotation", by_id["SI-weld"].role)
        self.assertEqual(WELD_GOST_REASON, by_id["SI-weld"].classification_reason)

    def test_html_weld_card_shows_gost_grafa(self) -> None:
        from dwg_symbols.html_report import _classified_card

        with tempfile.TemporaryDirectory() as directory:
            self._write_weld_table(
                directory,
                [
                    {
                        "signature": "blockdef-v1:butt",
                        "gostCode": "C2",
                        "label": "стыковой",
                    }
                ],
            )
            html = _classified_card(
                {
                    "id": "SI-weld",
                    "role": "drawing_annotation",
                    "classificationReason": WELD_GOST_REASON,
                    "signature": "blockdef-v1:butt",
                    "layer": "SVARKA",
                    "blockName": "*U129",
                    "sourceKind": "dwg_insert_candidate",
                    "position": {"x": 1.0, "y": 2.0, "space": "paper", "units": "mm"},
                },
                None,
                Path(directory),
            )
        self.assertIn("стыковой", html)
        self.assertIn("C2", html)
        self.assertIn("ГОСТ 2.312", html)

    def test_anonymous_fachwerk_is_drawing_object(self) -> None:
        member = self._insert(
            "SI-fachwerk",
            layer="МЕТАЛЛ ФАХВЕРК",
            block_name="*U33",
        )
        result = classify_sheet_furniture(self._page([member]))
        classified = result.symbol_instances[0]
        self.assertEqual("ignored", classified.status)
        self.assertEqual("drawing_object", classified.role)
        self.assertEqual(
            ANONYMOUS_CONSTRUCTION_LAYER_REASON,
            classified.classification_reason,
        )
        self.assertIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)
        self.assertEqual([], result.unknown_symbols)

    def test_named_block_on_fachwerk_layer_stays_candidate(self) -> None:
        named = self._insert(
            "SI-named",
            layer="МЕТАЛЛ ФАХВЕРК",
            block_name="Ферма ФС1",
        )
        result = classify_sheet_furniture(self._page([named]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertIsNone(classified.classification_reason)
        self.assertNotIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)

    def test_anonymous_stoiki_is_drawing_object(self) -> None:
        member = self._insert(
            "SI-post",
            layer="Стойки",
            block_name="A$C6A691B36",
        )
        result = classify_sheet_furniture(self._page([member]))
        classified = result.symbol_instances[0]
        self.assertEqual("drawing_object", classified.role)
        self.assertEqual(
            ANONYMOUS_CONSTRUCTION_LAYER_REASON,
            classified.classification_reason,
        )

    def test_bath_on_santech_layer_is_drawing_object(self) -> None:
        basin = self._insert(
            "SI-bath",
            layer="АР_сантех",
            block_name="M_BATH_BASIN_Basin - Rect_P",
        )
        result = classify_sheet_furniture(self._page([basin]))
        classified = result.symbol_instances[0]
        self.assertEqual("ignored", classified.status)
        self.assertEqual("drawing_object", classified.role)
        self.assertEqual(FURNITURE_BLOCK_LAYER_REASON, classified.classification_reason)
        self.assertIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)
        self.assertEqual([], result.unknown_symbols)

    def test_bath_on_furniture_layer_stays_candidate(self) -> None:
        basin = self._insert(
            "SI-bath",
            layer="АР_мебель",
            block_name="M_BATH_BASIN_Basin - Rect_P",
        )
        result = classify_sheet_furniture(self._page([basin]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertIsNone(classified.classification_reason)
        self.assertNotIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)

    def test_workplace_on_furniture_layer_is_drawing_object(self) -> None:
        desk = self._insert(
            "SI-desk",
            layer="АР_мебель",
            block_name="Индивидуальное Рабочее Место 23_6",
        )
        result = classify_sheet_furniture(self._page([desk]))
        classified = result.symbol_instances[0]
        self.assertEqual("drawing_object", classified.role)
        self.assertEqual(FURNITURE_BLOCK_LAYER_REASON, classified.classification_reason)

    def test_html_furniture_card_says_product_not_legend(self) -> None:
        from dwg_symbols.html_report import _classified_card

        html = _classified_card(
            {
                "id": "SI-bath",
                "role": "drawing_object",
                "classificationReason": FURNITURE_BLOCK_LAYER_REASON,
                "layer": "АР_сантех",
                "blockName": "M_BATH_BASIN_Basin - Rect_P",
                "sourceKind": "dwg_insert_candidate",
                "position": {"x": 1.0, "y": 2.0, "space": "paper", "units": "mm"},
            },
            None,
            Path("."),
        )
        self.assertIn("изделие", html)
        self.assertIn("не условный знак", html)

    def test_html_cpe_unknown_card_says_no_join(self) -> None:
        from dwg_symbols.html_report import _unknown_card

        html = _unknown_card(
            {
                "id": "US-cpe",
                "reason": GEOLOGY_NO_JOIN_REASON,
                "signature": "blockdef-v1:cpe",
                "occurrences": 1,
                "representativeInstanceId": "SI-cpe",
                "instanceIds": ["SI-cpe"],
            },
            {
                "SI-cpe": {
                    "id": "SI-cpe",
                    "layer": "Skv",
                    "blockName": "CPE",
                    "position": {"x": 1.0, "y": 2.0, "space": "paper", "units": "mm"},
                }
            },
            None,
            Path("."),
        )
        self.assertIn("нет стыка", html)
        self.assertNotIn("скважина по ГОСТ", html)

    def test_anonymous_armatura_stays_candidate(self) -> None:
        bar = self._insert(
            "SI-rebar",
            layer="ARMATURA",
            block_name="*U8",
        )
        result = classify_sheet_furniture(self._page([bar]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertNotIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)

    def test_anonymous_metiz_stays_candidate(self) -> None:
        fastener = self._insert(
            "SI-metiz",
            layer="METIZ",
            block_name="*U11",
        )
        result = classify_sheet_furniture(self._page([fastener]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertNotIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)

    def test_anonymous_fundament_is_drawing_object(self) -> None:
        member = self._insert(
            "SI-foundation",
            layer="FUNDAMENT",
            block_name="*U41",
        )
        result = classify_sheet_furniture(self._page([member]))
        classified = result.symbol_instances[0]
        self.assertEqual("ignored", classified.status)
        self.assertEqual("drawing_object", classified.role)
        self.assertEqual(
            ANONYMOUS_CONSTRUCTION_LAYER_REASON,
            classified.classification_reason,
        )

    def test_trapezia_on_technology_is_not_a_trap(self) -> None:
        decoy = self._insert(
            "SI-trap-like",
            layer="Технология",
            block_name="трапеция",
        )
        result = classify_sheet_furniture(self._page([decoy]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertNotIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)

    def test_building_code_stays_candidate(self) -> None:
        mark = self._insert(
            "SI-zd",
            layer="0",
            block_name="ЗД-1.ЗД-3",
        )
        result = classify_sheet_furniture(self._page([mark]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertIsNone(classified.classification_reason)
        self.assertNotIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)
        self.assertNotIn(MARK_FILTER_ANOMALY, result.anomaly_codes)

    def test_cpe_on_skv_stays_field_candidate(self) -> None:
        borehole = self._insert(
            "SI-cpe",
            layer="Skv",
            block_name="CPE",
        )
        result = classify_sheet_furniture(self._page([borehole]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertEqual(GEOLOGY_NO_JOIN_REASON, classified.classification_reason)
        self.assertIsNone(classified.legend_entry_id)
        self.assertEqual(GEOLOGY_NO_JOIN_REASON, result.unknown_symbols[0].reason)
        self.assertEqual((borehole.id,), result.unknown_symbols[0].instance_ids)

    def test_cpe_legend_label_without_block_is_not_decoded(self) -> None:
        legend = LegendEntry(
            id="LE-well",
            page=1,
            label="номер скважины",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature="legend-vector-v1:well-tick",
            bbox=(0.0, 0.0, 40.0, 12.0),
            symbol_bbox=(0.0, 0.0, 12.0, 12.0),
            confidence=0.9,
        )
        borehole = self._insert(
            "SI-cpe",
            layer="Skv",
            block_name="CPE",
            x=200.0,
            y=200.0,
        )
        result = classify_sheet_furniture(self._page([borehole], legends=[legend]))
        classified = result.symbol_instances[0]
        self.assertEqual("unresolved", classified.status)
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual(GEOLOGY_NO_JOIN_REASON, classified.classification_reason)
        self.assertIsNone(classified.legend_entry_id)

    def test_cpe_unique_legend_block_join_confirms(self) -> None:
        legend = LegendEntry(
            id="LE-well",
            page=1,
            label="номер скважины",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature="legend-vector-v1:well-tick",
            bbox=(0.0, 0.0, 40.0, 12.0),
            symbol_bbox=(0.0, 0.0, 12.0, 12.0),
            confidence=0.9,
        )
        exemplar = self._insert(
            "SI-ex",
            layer="Skv",
            block_name="CPE",
            signature="blockdef-v1:cpe",
            x=5.0,
            y=5.0,
        )
        field = self._insert(
            "SI-field",
            layer="Skv",
            block_name="CPE",
            signature="blockdef-v1:cpe",
            x=200.0,
            y=200.0,
        )
        result = classify_sheet_furniture(
            self._page([exemplar, field], legends=[legend])
        )
        result = resolve_exact_blocks(result)
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertEqual("reference", by_id["SI-ex"].status)
        self.assertEqual("legend_exemplar", by_id["SI-ex"].role)
        self.assertEqual("confirmed", by_id["SI-field"].status)
        self.assertEqual("LE-well", by_id["SI-field"].legend_entry_id)
        self.assertIsNone(by_id["SI-field"].classification_reason)
        self.assertEqual([], result.unknown_symbols)

    def test_anonymous_u_on_layer_zero_stays_unknown(self) -> None:
        blob = self._insert(
            "SI-u",
            layer="0",
            block_name="*U150",
        )
        result = classify_sheet_furniture(self._page([blob]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertIsNone(classified.classification_reason)
        self.assertNotIn(OBJECT_FILTER_ANOMALY, result.anomaly_codes)

    def test_numbering_on_layer_zero_is_specification_mark(self) -> None:
        mark = self._insert(
            "SI-num",
            layer="0",
            block_name="номерация 5",
        )
        result = classify_sheet_furniture(self._page([mark]))
        classified = result.symbol_instances[0]
        self.assertEqual("ignored", classified.status)
        self.assertEqual("specification_mark", classified.role)
        self.assertEqual(ROOM_NUMBER_BLOCK_REASON, classified.classification_reason)
        self.assertIn(MARK_FILTER_ANOMALY, result.anomaly_codes)
        self.assertNotIn(ANNOTATION_FILTER_ANOMALY, result.anomaly_codes)
        self.assertEqual([], result.unknown_symbols)

    def test_numbering_on_technology_layer_stays_candidate(self) -> None:
        mark = self._insert(
            "SI-num",
            layer="Технология",
            block_name="номерация 5",
        )
        result = classify_sheet_furniture(self._page([mark]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)
        self.assertIsNone(classified.classification_reason)

    def test_height_mark_on_layer_zero_is_annotation(self) -> None:
        mark = self._insert(
            "SI-height",
            layer="0",
            block_name="высоты 1.1",
        )
        result = classify_sheet_furniture(self._page([mark]))
        classified = result.symbol_instances[0]
        self.assertEqual("drawing_annotation", classified.role)
        self.assertEqual(HEIGHT_MARK_BLOCK_REASON, classified.classification_reason)

    def test_height_like_name_without_prefix_is_fail_closed(self) -> None:
        mark = self._insert(
            "SI-tall",
            layer="0",
            block_name="высотный блок",
        )
        result = classify_sheet_furniture(self._page([mark]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)

    def test_abk_label_on_layer_zero_is_annotation(self) -> None:
        mark = self._insert(
            "SI-abk",
            layer="0",
            block_name="НОВЫЙ АБК 6м СДВОЕННЫЙ",
        )
        result = classify_sheet_furniture(self._page([mark]))
        classified = result.symbol_instances[0]
        self.assertEqual("drawing_annotation", classified.role)
        self.assertEqual(BUILDING_LABEL_BLOCK_REASON, classified.classification_reason)

    def test_slope_block_on_layer_zero_stays_candidate(self) -> None:
        mark = self._insert(
            "SI-slope",
            layer="0",
            block_name="уклон",
        )
        result = classify_sheet_furniture(self._page([mark]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)

    def test_anonymous_layer_zero_stays_candidate(self) -> None:
        mark = self._insert(
            "SI-anon",
            layer="0",
            block_name="*U34",
        )
        result = classify_sheet_furniture(self._page([mark]))
        classified = result.symbol_instances[0]
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual("unresolved", classified.status)

    def test_confirmed_soil_circle_is_not_reclassified(self) -> None:
        legend = LegendEntry(
            id="LE-soil",
            page=1,
            label="Глина",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature="legend-vector-v1:soil",
            confidence=0.9,
        )
        circle = self._insert(
            "SI-circle",
            layer="Decoration",
            block_name="круг1",
            status="confirmed",
            attributes={},
            legend_entry_id=legend.id,
        )
        binding = SymbolBinding(
            id="SB-soil",
            page=1,
            instance_id=circle.id,
            legend_entry_id=legend.id,
            status="confirmed",
            confidence=1.0,
            evidence=(
                Evidence(
                    kind="exact_block_definition",
                    score=1.0,
                    source_ids=(legend.id,),
                    detail="soil hatch",
                ),
            ),
        )
        original = self._page([circle], legends=[legend], bindings=[binding])
        result = classify_sheet_furniture(original)
        classified = result.symbol_instances[0]
        self.assertIs(result, original)
        self.assertEqual("confirmed", classified.status)
        self.assertEqual("field_candidate", classified.role)
        self.assertEqual(legend.id, classified.legend_entry_id)

    def test_recount_non_symbols_classifies_json_without_page_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page_dir = Path(directory) / "dwg_symbols" / "page_0001"
            page_dir.mkdir(parents=True)
            (page_dir / "symbol_instances.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "page": 1,
                        "items": [
                            self._insert(
                                "SI-stamp",
                                layer="0",
                                block_name="*U74",
                                attributes={"ЛИСТ": "1", "ФОРМАТ": "А2"},
                            ).to_dict(),
                            self._insert(
                                "SI-column",
                                layer="колонна",
                                block_name="Колонна",
                            ).to_dict(),
                            self._insert(
                                "SI-cpe",
                                layer="Skv",
                                block_name="CPE",
                            ).to_dict(),
                            self._insert(
                                "SI-trap",
                                layer="Технология",
                                block_name="трап100",
                            ).to_dict(),
                            self._insert(
                                "SI-fachwerk",
                                layer="МЕТАЛЛ ФАХВЕРК",
                                block_name="*U33",
                            ).to_dict(),
                            self._insert(
                                "SI-weld",
                                layer="SVARKA",
                                block_name="*U90",
                            ).to_dict(),
                            self._insert(
                                "SI-slope",
                                layer="0",
                                block_name="уклон",
                            ).to_dict(),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary = recount_non_symbols(directory)
        self.assertEqual(1, summary["pages"])
        self.assertEqual(7, summary["unknownBefore"])
        self.assertEqual(3, summary["unknownAfter"])
        self.assertEqual(1, summary["geologyUnknownAfter"])
        self.assertEqual(
            {
                "sheet_furniture": 1,
                "drawing_object": 3,
                "field_candidate": 3,
            },
            summary["rolesAfter"],
        )
        self.assertEqual(
            {
                STAMP_ATTRIBUTES_REASON: 1,
                COLUMN_BLOCK_LAYER_REASON: 1,
                TRAP_BLOCK_LAYER_REASON: 1,
                ANONYMOUS_CONSTRUCTION_LAYER_REASON: 1,
                GEOLOGY_NO_JOIN_REASON: 1,
            },
            summary["reasons"],
        )


class CatalogTests(unittest.TestCase):
    def tearDown(self) -> None:
        reset_weld_table()

    @staticmethod
    def _insert(*args, **kwargs):
        return FurnitureTests._insert(*args, **kwargs)

    def _write_page(
        self,
        root: Path,
        *,
        page: int,
        items: list,
        document_id: str = "DOC-a",
        document_path: str = "a.dwg",
        relative_path: str | None = None,
        file_id: str = "file-a",
    ) -> None:
        page_dir = root / file_id / "dwg_symbols" / f"page_{page:04d}"
        page_dir.mkdir(parents=True)
        (page_dir / "symbol_instances.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "page": page,
                    "items": [item.to_dict() for item in items],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (page_dir / "summary.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "documentId": document_id,
                    "documentPath": document_path,
                    "page": page,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        if relative_path is not None:
            (root / file_id / "document_application.json").write_text(
                json.dumps(
                    {"fileId": file_id, "relativePath": relative_path},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

    def _by_signature(self, catalog: dict) -> dict[str, dict]:
        return {item["signature"]: item for item in catalog["types"]}

    def test_catalog_groups_signatures_and_counts_application(self) -> None:
        trap = self._insert(
            "SI-trap",
            layer="Технология",
            block_name="трап100",
            signature="blockdef-v1:trap",
        )
        column = self._insert(
            "SI-column",
            layer="колонна",
            block_name="Колонна",
            signature="blockdef-v1:column",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_page(
                root,
                page=1,
                items=[trap, column],
                document_id="DOC-a",
                document_path="/work/a.dwg",
                relative_path="6 - ТХ/a.dwg",
                file_id="file-a",
            )
            self._write_page(
                root,
                page=2,
                items=[replace(trap, id="SI-trap-2")],
                document_id="DOC-a",
                document_path="/work/a.dwg",
                relative_path="6 - ТХ/a.dwg",
                file_id="file-a",
            )
            self._write_page(
                root,
                page=1,
                items=[replace(trap, id="SI-trap-b")],
                document_id="DOC-b",
                document_path="/work/b.dwg",
                relative_path="6 - ТХ/b.dwg",
                file_id="file-b",
            )
            catalog = catalog_symbol_types(root)
        self.assertEqual(3, catalog["pages"])
        self.assertEqual(2, catalog["documents"])
        self.assertEqual(4, catalog["instances"])
        self.assertEqual(2, catalog["uniqueTypes"])
        types = self._by_signature(catalog)
        trap_type = types["blockdef-v1:trap"]
        self.assertEqual("трап100", trap_type["blockName"])
        self.assertEqual(["Технология"], trap_type["layers"])
        self.assertEqual(3, trap_type["instanceCount"])
        self.assertEqual(3, trap_type["pageCount"])
        self.assertEqual(2, trap_type["documentCount"])
        self.assertEqual("drawing_object", trap_type["sectionId"])
        self.assertEqual("drawing_object", trap_type["pipelineRole"])
        self.assertEqual("not_applicable", trap_type["gostCheck"])
        self.assertEqual(
            [
                {
                    "documentId": "DOC-a",
                    "documentPath": "/work/a.dwg",
                    "relativePath": "6 - ТХ/a.dwg",
                    "page": 1,
                    "count": 1,
                },
                {
                    "documentId": "DOC-a",
                    "documentPath": "/work/a.dwg",
                    "relativePath": "6 - ТХ/a.dwg",
                    "page": 2,
                    "count": 1,
                },
                {
                    "documentId": "DOC-b",
                    "documentPath": "/work/b.dwg",
                    "relativePath": "6 - ТХ/b.dwg",
                    "page": 1,
                    "count": 1,
                },
            ],
            trap_type["occurrences"],
        )
        column_type = types["blockdef-v1:column"]
        self.assertEqual("drawing_object", column_type["sectionId"])
        self.assertEqual("drawing_object", column_type["pipelineRole"])
        self.assertEqual("not_applicable", column_type["gostCheck"])

    def test_catalog_draft_shelves_and_gost_queue(self) -> None:
        items = [
            self._insert(
                "SI-legend",
                layer="грунт",
                block_name="круг1",
                status="confirmed",
                signature="blockdef-v1:soil",
                legend_entry_id="LE-soil",
            ),
            self._insert(
                "SI-stamp",
                layer="FORMAT",
                block_name="*U704",
                signature="blockdef-v1:stamp",
                attributes={"ЛИСТ": "1", "ГИП": "Иванов"},
            ),
            self._insert(
                "SI-axis",
                layer="OSI",
                block_name="*U12",
                signature="blockdef-v1:axis",
                attributes={"Ось": "7", "Ось'": "Ж"},
            ),
            self._insert(
                "SI-room",
                layer="0",
                block_name="номерация 5",
                signature="blockdef-v1:room",
            ),
            self._insert(
                "SI-dim",
                layer="RAZMER",
                block_name="*U382",
                signature="blockdef-v1:dim",
            ),
            self._insert(
                "SI-weld",
                layer="SVARKA",
                block_name="*U90",
                signature="blockdef-v1:weld",
            ),
            self._insert(
                "SI-fachwerk",
                layer="МЕТАЛЛ ФАХВЕРК",
                block_name="*U33",
                signature="blockdef-v1:fachwerk",
            ),
            self._insert(
                "SI-cpe",
                layer="Skv",
                block_name="CPE",
                signature="blockdef-v1:cpe",
            ),
            self._insert(
                "SI-slope",
                layer="0",
                block_name="уклон",
                signature="blockdef-v1:slope",
            ),
            self._insert(
                "SI-zd",
                layer="0",
                block_name="ЗД-1.ЗД-3",
                signature="blockdef-v1:zd",
            ),
            self._insert(
                "SI-rebar",
                layer="ARMATURA",
                block_name="*U8",
                signature="blockdef-v1:rebar",
            ),
            self._insert(
                "SI-anon",
                layer="0",
                block_name="*U34",
                signature="blockdef-v1:anon",
            ),
            self._insert(
                "SI-blank-stamp",
                layer="Оформление",
                block_name="Штамп",
                signature="blockdef-v1:blank-stamp",
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_page(root, page=1, items=items)
            catalog = catalog_symbol_types(root)
        types = self._by_signature(catalog)
        expected = {
            "blockdef-v1:soil": ("legend", "legend", "not_applicable"),
            "blockdef-v1:stamp": ("sheet_furniture", "sheet_furniture", "worth"),
            "blockdef-v1:axis": ("specification_mark", "specification_mark", "worth"),
            "blockdef-v1:room": ("specification_mark", "specification_mark", "not_applicable"),
            "blockdef-v1:dim": ("drawing_annotation", "drawing_annotation", "worth"),
            "blockdef-v1:weld": ("welding", "unknown", "worth"),
            "blockdef-v1:fachwerk": ("drawing_object", "drawing_object", "not_applicable"),
            "blockdef-v1:cpe": ("geology_mark", "unknown", "not_applicable"),
            "blockdef-v1:slope": ("slope_mark", "unknown", "later"),
            "blockdef-v1:zd": ("building_code", "unknown", "not_applicable"),
            "blockdef-v1:rebar": ("rebar_fasteners", "unknown", "later"),
            "blockdef-v1:anon": ("anonymous_other", "unknown", "not_applicable"),
            "blockdef-v1:blank-stamp": ("stamp_unclassified", "unknown", "later"),
        }
        for signature, (section, role, gost) in expected.items():
            item = types[signature]
            self.assertEqual(section, item["sectionId"], signature)
            self.assertEqual(role, item["pipelineRole"], signature)
            self.assertEqual(gost, item["gostCheck"], signature)
            self.assertTrue(item["rationale"], signature)
        self.assertEqual("unknown", types["blockdef-v1:weld"]["pipelineRole"])
        self.assertIn("гост", types["blockdef-v1:weld"]["rationale"].casefold())

    def test_catalog_matched_weld_stays_on_welding_shelf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            table = root / "gost_welds.json"
            table.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "welds": [
                            {
                                "signature": "blockdef-v1:butt",
                                "gostCode": "C2",
                                "label": "стыковой",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            reset_weld_table()
            load_weld_table(table)
            self._write_page(
                root,
                page=1,
                items=[
                    self._insert(
                        "SI-weld",
                        layer="SVARKA",
                        block_name="*U129",
                        signature="blockdef-v1:butt",
                    )
                ],
            )
            catalog = catalog_symbol_types(root)
        item = catalog["types"][0]
        self.assertEqual("welding", item["sectionId"])
        self.assertEqual("drawing_annotation", item["pipelineRole"])
        self.assertEqual("worth", item["gostCheck"])
        self.assertEqual(WELD_GOST_REASON, next(iter(item["reasons"])))
        self.assertIn("2.312", item["rationale"])

    def test_catalog_named_furniture_stays_unknown_equipment_shelf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_page(
                root,
                page=1,
                items=[
                    self._insert(
                        "SI-bath",
                        layer="АР_мебель",
                        block_name="M_BATH_BASIN_Basin - Rect_P",
                        signature="blockdef-v1:bath",
                    )
                ],
            )
            payload = catalog_symbol_types(root)
        self.assertEqual("symbol_type_catalog", payload["kind"])
        self.assertEqual(1, payload["uniqueTypes"])
        item = payload["types"][0]
        self.assertEqual("named_equipment", item["sectionId"])
        self.assertEqual("unknown", item["pipelineRole"])
        self.assertEqual("not_applicable", item["gostCheck"])

    def test_catalog_bath_on_santech_is_drawing_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_page(
                root,
                page=1,
                items=[
                    self._insert(
                        "SI-bath",
                        layer="АР_сантех",
                        block_name="M_BATH_BASIN_Basin - Rect_P",
                        signature="blockdef-v1:bath-rect",
                    )
                ],
            )
            payload = catalog_symbol_types(root)
        item = payload["types"][0]
        self.assertEqual("drawing_object", item["sectionId"])
        self.assertEqual("drawing_object", item["pipelineRole"])
        self.assertEqual("not_applicable", item["gostCheck"])
        self.assertIn(FURNITURE_BLOCK_LAYER_REASON, item["reasons"])


class HarnessTests(unittest.TestCase):
    def test_manifest_rejects_duplicate_fixture_ids(self) -> None:
        fixture = {
            "id": "same",
            "documentPath": "a.dwg",
            "page": 1,
            "split": "development",
            "expected": {
                "instanceKeys": [],
                "unknownInstanceKeys": [],
                "bindings": [],
                "relationships": [],
            },
        }
        with self.assertRaisesRegex(HarnessError, "duplicate"):
            validate_manifest({"schemaVersion": 1, "fixtures": [fixture, fixture]})

    def test_exact_key_scoring(self) -> None:
        instance = _instance()
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            symbol_instances=[instance],
            unknown_symbols=[
                UnknownSymbolCluster(
                    id="US-test",
                    page=1,
                    signature=instance.signature,
                    instance_ids=(instance.id,),
                    reason="NO_LEGEND_BINDING_BASELINE",
                    representative_instance_id=instance.id,
                )
            ],
        )
        fixture = {
            "id": "fixture",
            "expected": {
                "instanceKeys": ["modelspace|A1"],
                "unknownInstanceKeys": ["modelspace|A1"],
                "bindings": [],
                "relationships": [],
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            page_dir = write_page_result(directory, result)
            score = score_fixture(fixture, page_dir)
        self.assertEqual(1.0, score["instances"]["f1"])
        self.assertEqual(1.0, score["unknownInstances"]["f1"])
        self.assertEqual(0, score["falseConfirmedBindings"])

    def test_shallow_audit_does_not_require_converter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sample.dwg"
            source.write_bytes(b"not a real DWG")
            result = audit_package(directory, deep=False)
        self.assertEqual(1, result["summary"]["documents"])
        self.assertEqual({"not_inspected": 1}, result["summary"]["statuses"])


class LegendTests(unittest.TestCase):
    def test_extracts_aligned_rows_with_vector_signatures(self) -> None:
        texts = [
            TextItem(100, 90, 4, "Условные обозначения", "mtext"),
            TextItem(82, 80, 3, "Стена из панелей", "mtext"),
            TextItem(82.4, 68, 3, "Огнестойкая перегородка", "mtext"),
            TextItem(82.2, 56, 3, "Перегородка из блоков", "mtext"),
            TextItem(82.2, -10, 3, "Название объекта в штампе", "mtext"),
        ]
        primitives = [
            {
                "type": "line",
                "layer": "WALL",
                "color": "#000000",
                "lw": 0.25,
                "points": [(52, 80), (77, 80)],
            },
            {
                "type": "polyline",
                "layer": "WALL",
                "color": "#000000",
                "lw": 0.25,
                "points": [(52, 66), (60, 70), (68, 66), (77, 70)],
            },
            {
                "type": "line",
                "layer": "WALL",
                "color": "#000000",
                "lw": 0.5,
                "points": [(52, 56), (77, 56)],
            },
        ]
        entries, crops, anomalies = detect_legend_entries(
            document_id="DOC-test",
            page=1,
            texts=texts,
            primitives=primitives,
            paper_width=297,
            paper_height=210,
        )
        self.assertEqual(3, len(entries))
        self.assertTrue(all(item.signature for item in entries))
        self.assertEqual({item.id for item in entries}, set(crops))
        self.assertEqual([], anomalies)

    def test_missing_heading_is_explicit(self) -> None:
        entries, crops, anomalies = detect_legend_entries(
            document_id="DOC-test",
            page=1,
            texts=[TextItem(10, 10, 3, "Стена", "mtext")],
            primitives=[],
            paper_width=297,
            paper_height=210,
        )
        self.assertEqual([], entries)
        self.assertEqual({}, crops)
        self.assertEqual(["LEGEND_NOT_FOUND"], anomalies)

    def test_crop_uses_segment_intersection_not_enclosing_bbox(self) -> None:
        frame = {
            "type": "polyline",
            "points": [
                (-100, -100),
                (100, -100),
                (100, 100),
                (-100, 100),
                (-100, -100),
            ],
        }
        crossing = {"type": "line", "points": [(-5, 5), (15, 5)]}
        cropped, exceeded = _crop_primitives([frame, crossing], (0, 0, 10, 10))
        self.assertFalse(exceeded)
        self.assertEqual(1, len(cropped))
        self.assertEqual([(0.0, 5.0), (10.0, 5.0)], cropped[0]["points"])


class ResolverTests(unittest.TestCase):
    @staticmethod
    def _candidate(
        instance_id: str,
        handle: str,
        x: float,
        y: float,
        signature: str,
    ) -> SymbolInstance:
        return SymbolInstance(
            id=instance_id,
            page=1,
            source_kind="dwg_insert_candidate",
            status="unresolved",
            position=Point(x, y, "paper", "mm"),
            source_handle=handle,
            source_space="modelspace:viewport:0",
            layer="SYMBOLS",
            signature=signature,
            block_name="MARK",
            confidence=1.0,
        )

    @staticmethod
    def _legend(entry_id: str, bbox: tuple[float, float, float, float]) -> LegendEntry:
        return LegendEntry(
            id=entry_id,
            page=1,
            label=entry_id,
            status="extracted",
            source_kind="dwg_vector_legend",
            signature=f"legend-vector-v1:{entry_id}",
            bbox=bbox,
            symbol_bbox=bbox,
            confidence=0.9,
        )

    @staticmethod
    def _baseline(
        legends: list[LegendEntry], instances: list[SymbolInstance]
    ) -> PageResult:
        grouped: dict[str, list[SymbolInstance]] = {}
        for instance in instances:
            grouped.setdefault(instance.signature, []).append(instance)
        return PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="partial",
            anomaly_codes=["BLOCK_BASELINE_ONLY", "RELATIONSHIP_STAGE_NOT_RUN"],
            legend_entries=legends,
            symbol_instances=instances,
            unknown_symbols=[
                UnknownSymbolCluster(
                    id=stable_id("US", signature),
                    page=1,
                    signature=signature,
                    instance_ids=tuple(item.id for item in items),
                    reason="NO_LEGEND_BINDING_BASELINE",
                    representative_instance_id=items[0].id,
                )
                for signature, items in grouped.items()
            ],
        )

    def test_exact_block_signature_confirms_field_instance(self) -> None:
        exemplar = self._candidate("SI-exemplar", "A1", 5, 5, "blockdef-v1:a")
        field = self._candidate("SI-field", "A2", 20, 20, "blockdef-v1:a")
        unknown = self._candidate("SI-unknown", "B1", 30, 30, "blockdef-v1:b")
        result = resolve_exact_blocks(
            self._baseline([self._legend("LE-a", (0, 0, 10, 10))], [
                exemplar,
                field,
                unknown,
            ])
        )
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertEqual("reference", by_id["SI-exemplar"].status)
        self.assertEqual("legend_exemplar", by_id["SI-exemplar"].role)
        self.assertEqual("confirmed", by_id["SI-field"].status)
        self.assertEqual("LE-a", by_id["SI-field"].legend_entry_id)
        self.assertEqual(1, len(result.symbol_bindings))
        self.assertEqual(("SI-unknown",), result.unknown_symbols[0].instance_ids)
        summary = result.summary_dict()
        self.assertEqual(
            {"confirmed": 1, "reference": 1, "unresolved": 1},
            summary["instanceStatuses"],
        )

    def test_shared_signature_is_not_confirmed(self) -> None:
        first = self._candidate("SI-first", "A1", 5, 5, "blockdef-v1:a")
        second = self._candidate("SI-second", "A2", 25, 5, "blockdef-v1:a")
        field = self._candidate("SI-field", "A3", 50, 50, "blockdef-v1:a")
        result = resolve_exact_blocks(
            self._baseline(
                [
                    self._legend("LE-a", (0, 0, 10, 10)),
                    self._legend("LE-b", (20, 0, 30, 10)),
                ],
                [first, second, field],
            )
        )
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertEqual("unresolved", by_id["SI-field"].status)
        self.assertEqual([], result.symbol_bindings)
        self.assertIn(
            "H4_BLOCK_SIGNATURE_LEGEND_CONFLICT", result.anomaly_codes
        )
        self.assertEqual(
            "AMBIGUOUS_LEGEND_BLOCK_SIGNATURE",
            result.unknown_symbols[0].reason,
        )

    def test_overlapping_exemplar_is_never_bound_as_field_instance(self) -> None:
        unique = self._candidate("SI-unique", "A1", 2, 5, "blockdef-v1:a")
        overlap = self._candidate("SI-overlap", "A2", 7, 5, "blockdef-v1:a")
        field = self._candidate("SI-field", "A3", 30, 30, "blockdef-v1:a")
        result = resolve_exact_blocks(
            self._baseline(
                [
                    self._legend("LE-a", (0, 0, 10, 10)),
                    self._legend("LE-b", (5, 0, 15, 10)),
                ],
                [unique, overlap, field],
            )
        )
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertEqual("unresolved", by_id["SI-overlap"].status)
        self.assertEqual("confirmed", by_id["SI-field"].status)
        self.assertEqual(
            ["SI-field"], [item.instance_id for item in result.symbol_bindings]
        )


class GeometryResolverTests(unittest.TestCase):
    @staticmethod
    def _rectangle(
        center: tuple[float, float],
        length: float,
        thickness: float,
        angle: float = 0.0,
        color: str = "#ff0000",
        lineweight: float = 0.2,
    ) -> dict:
        cosine, sine = math.cos(angle), math.sin(angle)
        local = [
            (-length / 2, -thickness / 2),
            (length / 2, -thickness / 2),
            (length / 2, thickness / 2),
            (-length / 2, thickness / 2),
        ]
        points = [
            (
                center[0] + x * cosine - y * sine,
                center[1] + x * sine + y * cosine,
            )
            for x, y in local
        ]
        return {
            "type": "polyline",
            "layer": "WALL",
            "color": color,
            "lw": lineweight,
            "points": [*points, points[0]],
        }

    def test_profile_is_rotation_and_length_invariant(self) -> None:
        first = closed_profile(self._rectangle((0, 0), 20, 2))
        second = closed_profile(self._rectangle((30, 30), 35, 2, math.pi / 3))
        scaled = closed_profile(self._rectangle((60, 60), 35, 1))
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNotNone(scaled)
        self.assertEqual(first.signature, second.signature)
        self.assertNotEqual(first.signature, scaled.signature)
        self.assertEqual(first.style_signature, scaled.style_signature)

    def test_unique_legend_profile_creates_probable_binding(self) -> None:
        legend = LegendEntry(
            id="LE-wall",
            page=1,
            label="Wall",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature="legend-vector-v1:wall",
            bbox=(-11, -2, 11, 2),
            symbol_bbox=(-11, -2, 11, 2),
            confidence=0.9,
        )
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="partial",
            legend_entries=[legend],
        )
        result = resolve_exact_blocks(result)
        sample = self._rectangle((0, 0), 20, 2)
        field = self._rectangle((40, 40), 35, 1, math.pi / 4)
        distractor = self._rectangle((80, 80), 35, 3, color="#00ff00")
        result = resolve_geometry_profiles(result, [sample, field, distractor])
        self.assertEqual(1, len(result.symbol_instances))
        self.assertEqual("probable", result.symbol_instances[0].status)
        self.assertEqual("LE-wall", result.symbol_instances[0].legend_entry_id)
        self.assertEqual(1, len(result.symbol_bindings))
        self.assertEqual("probable", result.symbol_bindings[0].status)
        self.assertEqual(
            "normalized_geometry_style",
            result.symbol_bindings[0].evidence[0].kind,
        )

    def test_shared_geometry_style_stays_unbound(self) -> None:
        legends = [
            LegendEntry(
                id="LE-a",
                page=1,
                label="Wall A",
                status="extracted",
                source_kind="dwg_vector_legend",
                signature="legend-vector-v1:a",
                bbox=(-11, -2, 11, 2),
                symbol_bbox=(-11, -2, 11, 2),
                confidence=0.9,
            ),
            LegendEntry(
                id="LE-b",
                page=1,
                label="Wall B",
                status="extracted",
                source_kind="dwg_vector_legend",
                signature="legend-vector-v1:b",
                bbox=(19, -2, 41, 2),
                symbol_bbox=(19, -2, 41, 2),
                confidence=0.9,
            ),
        ]
        result = resolve_exact_blocks(
            PageResult(
                document_id="DOC-test",
                document_path="test.dwg",
                page=1,
                completeness="partial",
                legend_entries=legends,
            )
        )
        result = resolve_geometry_profiles(
            result,
            [
                self._rectangle((0, 0), 20, 2),
                self._rectangle((30, 0), 20, 2),
                self._rectangle((70, 70), 30, 1),
            ],
        )
        self.assertEqual([], result.symbol_bindings)
        self.assertIn(
            "H4_GEOMETRY_PROFILE_LEGEND_CONFLICT", result.anomaly_codes
        )


class ContextResolverTests(unittest.TestCase):
    def test_russian_inflections_share_context_term(self) -> None:
        self.assertIn("блок", context_terms("из блоков ячеистого бетона"))
        self.assertIn("блок", context_terms("АР БЛОКИ"))

    def test_unique_layer_term_adds_probable_binding(self) -> None:
        legend = LegendEntry(
            id="LE-blocks",
            page=1,
            label="Перегородки из блоков ячеистого бетона",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature="legend-vector-v1:blocks",
            bbox=(-11, -3, 11, 3),
            symbol_bbox=(-11, -3, 11, 3),
            confidence=0.9,
        )
        result = resolve_exact_blocks(
            PageResult(
                document_id="DOC-test",
                document_path="test.dwg",
                page=1,
                completeness="partial",
                legend_entries=[legend],
            )
        )
        sample = GeometryResolverTests._rectangle(
            (0, 0), 20, 3, color="#808080", lineweight=0.3
        )
        field = GeometryResolverTests._rectangle(
            (40, 40), 30, 1, color="#00ff00", lineweight=0.25
        )
        field["layer"] = "АР_БЛОКИ"
        result = resolve_geometry_profiles(result, [sample, field])
        self.assertEqual([], result.symbol_bindings)
        result = resolve_layer_context(result, [sample, field])
        self.assertEqual(1, len(result.symbol_bindings))
        self.assertEqual("probable", result.symbol_bindings[0].status)
        self.assertEqual(
            "layer_label_context", result.symbol_bindings[0].evidence[0].kind
        )

    def test_ambiguous_layer_term_does_not_bind(self) -> None:
        legends = [
            LegendEntry(
                id=f"LE-{index}",
                page=1,
                label=f"Стена из блоков, вариант {index}",
                status="extracted",
                source_kind="dwg_vector_legend",
                signature=f"legend-vector-v1:{index}",
                bbox=(index * 30.0, -3, index * 30.0 + 20, 3),
                symbol_bbox=(index * 30.0, -3, index * 30.0 + 20, 3),
                confidence=0.9,
            )
            for index in (0, 1)
        ]
        result = resolve_exact_blocks(
            PageResult(
                document_id="DOC-test",
                document_path="test.dwg",
                page=1,
                completeness="partial",
                legend_entries=legends,
            )
        )
        field = GeometryResolverTests._rectangle(
            (80, 80), 30, 1, color="#00ff00", lineweight=0.25
        )
        field["layer"] = "БЛОКИ"
        first_sample = GeometryResolverTests._rectangle(
            (10, 0), 18, 2, color="#808080", lineweight=0.3
        )
        second_sample = GeometryResolverTests._rectangle(
            (40, 0), 18, 3, color="#a5a552", lineweight=0.3
        )
        primitives = [first_sample, second_sample, field]
        result = resolve_geometry_profiles(result, primitives)
        result = resolve_layer_context(result, primitives)
        self.assertEqual([], result.symbol_bindings)
        self.assertIn("H4_LAYER_CONTEXT_AMBIGUOUS", result.anomaly_codes)


class FileLegendCatalogTests(unittest.TestCase):
    @staticmethod
    def _page(
        page: int,
        entries: list[LegendEntry],
        instances: list[SymbolInstance] | None = None,
    ) -> PageResult:
        return PageResult(
            document_id="DOC-file",
            document_path="drawing.dwg",
            page=page,
            completeness="complete",
            legend_entries=entries,
            symbol_instances=instances or [],
        )

    @staticmethod
    def _entry(page: int, entry_id: str, label: str, signature: str) -> LegendEntry:
        return LegendEntry(
            id=entry_id,
            page=page,
            label=label,
            status="extracted",
            source_kind="dwg_vector_legend",
            signature=f"legend-vector-v1:{entry_id}",
            reference_signatures=(signature,),
            confidence=1.0,
        )

    def test_combines_labels_and_proposes_cross_sheet_exact_candidate(self) -> None:
        first = self._page(
            1,
            [self._entry(1, "LE-one", "Противопожарная перегородка", "blockdef-v1:x")],
        )
        second = self._page(
            2,
            [
                self._entry(
                    2,
                    "LE-two",
                    "противопожарная   перегородка",
                    "blockdef-v1:x",
                )
            ],
        )

        catalog = build_file_legend_catalog([first, second])

        self.assertEqual(1, catalog["counts"]["uniqueLegendEntries"])
        self.assertEqual(1, catalog["counts"]["duplicateLegendOccurrences"])
        self.assertEqual([], catalog["crossSheetCandidates"])

        third_candidate = SymbolInstance(
            id="SI-page3",
            page=3,
            source_kind="dwg_insert_candidate",
            status="unresolved",
            position=Point(40.0, 50.0, "paper", "mm"),
            source_handle="C3",
            source_space="modelspace:viewport:0",
            layer="WALL",
            signature="blockdef-v1:x",
            confidence=1.0,
        )
        third = self._page(3, [], [third_candidate])
        catalog = build_file_legend_catalog([first, second, third])
        self.assertEqual(1, catalog["counts"]["crossSheetCandidates"])
        self.assertEqual(
            "modelspace:viewport:0|C3",
            catalog["crossSheetCandidates"][0]["sourceKey"],
        )
        self.assertFalse(catalog["policy"]["automaticCrossSheetBinding"])

    def test_conflicting_signature_blocks_cross_sheet_candidate(self) -> None:
        first = self._page(
            1,
            [self._entry(1, "LE-a", "Обозначение А", "blockdef-v1:shared")],
        )
        candidate = SymbolInstance(
            id="SI-conflict",
            page=3,
            source_kind="dwg_insert_candidate",
            status="unresolved",
            position=Point(10.0, 10.0, "paper", "mm"),
            source_handle="D4",
            source_space="modelspace:viewport:0",
            layer="0",
            signature="blockdef-v1:shared",
            confidence=1.0,
        )
        second = self._page(
            2,
            [self._entry(2, "LE-b", "Обозначение Б", "blockdef-v1:shared")],
        )
        third = self._page(3, [], [candidate])

        catalog = build_file_legend_catalog([first, second, third])

        self.assertEqual(1, catalog["counts"]["conflicts"])
        self.assertEqual([], catalog["crossSheetCandidates"])
        self.assertEqual(
            {"conflicting"},
            {entry["status"] for entry in catalog["entries"]},
        )


class ProjectLegendCatalogTests(unittest.TestCase):
    @staticmethod
    def _document(
        file_id: str,
        relative_path: str,
        discipline: str,
        label: str,
        signature: str,
    ) -> dict:
        return {
            "fileId": file_id,
            "relativePath": relative_path,
            "discipline": discipline,
            "externalReference": "Внешние ссылки" in relative_path,
            "status": "processed",
            "pageCount": 2,
            "legendHeadingPages": [1],
            "processedLegendPages": [1],
            "errors": [],
            "catalog": {
                "counts": {"legendOccurrences": 1},
                "entries": [
                    {
                        "id": f"FLE-{file_id}",
                        "label": label,
                        "labelVariants": [label],
                        "sourcePages": [1],
                        "sources": [
                            {
                                "page": 1,
                                "legendEntryId": f"LE-{file_id}",
                                "cropPath": f"legend_crops/LE-{file_id}.svg",
                            }
                        ],
                        "vectorSignatures": [],
                        "referenceSignatures": [signature],
                        "geometrySignatures": [],
                    }
                ],
            },
        }

    def test_merges_same_label_across_files_with_provenance(self) -> None:
        first = self._document(
            "one",
            "4 - КР/plan.dwg",
            "4 - КР",
            "Противопожарная перегородка",
            "blockdef-v1:a",
        )
        second = self._document(
            "two",
            "6 - ТХ/Внешние ссылки/plan.dwg",
            "6 - ТХ",
            "противопожарная   перегородка",
            "blockdef-v1:b",
        )

        catalog = build_project_legend_catalog(
            [first, second],
            package_root="new_files/dwg",
        )

        self.assertEqual(1, catalog["counts"]["uniqueProjectEntries"])
        entry = catalog["entries"][0]
        self.assertEqual(["4 - КР", "6 - ТХ"], entry["disciplines"])
        self.assertEqual(2, entry["sourceFileCount"])
        self.assertEqual(1, entry["independentSourceFileCount"])
        self.assertEqual(1, entry["externalReferenceFileCount"])
        self.assertTrue(entry["sources"][1]["externalReference"])
        self.assertEqual(
            "documents/one/dwg_symbols/page_0001/legend_crops/LE-one.svg",
            entry["sources"][0]["sources"][0]["artifactCropPath"],
        )
        self.assertFalse(catalog["applicationPolicy"]["automaticCrossFileBinding"])

    def test_blocks_signature_reused_by_different_project_labels(self) -> None:
        first = self._document(
            "one",
            "4 - КР/a.dwg",
            "4 - КР",
            "Обозначение А",
            "blockdef-v1:shared",
        )
        second = self._document(
            "two",
            "6 - ТХ/b.dwg",
            "6 - ТХ",
            "Обозначение Б",
            "blockdef-v1:shared",
        )

        catalog = build_project_legend_catalog(
            [first, second],
            package_root="new_files/dwg",
        )

        self.assertEqual(1, catalog["counts"]["conflicts"])
        self.assertEqual(
            {"conflicting"},
            {entry["status"] for entry in catalog["entries"]},
        )
        self.assertEqual(
            {"Обозначение А", "Обозначение Б"},
            set(catalog["conflicts"][0]["labels"]),
        )

    def test_unit_spacing_does_not_create_false_project_conflict(self) -> None:
        first = self._document(
            "one",
            "4 - КР/a.dwg",
            "4 - КР",
            "Стена толщиной 120 мм",
            "blockdef-v1:wall",
        )
        second = self._document(
            "two",
            "6 - ТХ/b.dwg",
            "6 - ТХ",
            "Стена толщиной 120мм",
            "blockdef-v1:wall",
        )

        catalog = build_project_legend_catalog(
            [first, second],
            package_root="new_files/dwg",
        )

        self.assertEqual(1, catalog["counts"]["uniqueProjectEntries"])
        self.assertEqual(0, catalog["counts"]["conflicts"])
        self.assertEqual(2, len(catalog["entries"][0]["labelVariants"]))

    def test_external_reference_only_entry_is_not_independent_evidence(self) -> None:
        external = self._document(
            "xref",
            "4 - КР/Внешние ссылки/plan.dwg",
            "4 - КР",
            "Наружная стена",
            "blockdef-v1:wall",
        )

        catalog = build_project_legend_catalog(
            [external],
            package_root="new_files/dwg",
        )

        self.assertEqual("external_only", catalog["entries"][0]["status"])
        self.assertEqual(1, catalog["counts"]["externalOnlyEntries"])

    def test_detects_explicit_legend_heading(self) -> None:
        self.assertTrue(
            has_legend_heading(
                [TextItem(10, 20, 3, "Условные обозначения", "mtext")]
            )
        )
        self.assertFalse(
            has_legend_heading([TextItem(10, 20, 3, "План первого этажа", "mtext")])
        )
        self.assertTrue(
            has_notes_heading([TextItem(10, 40, 3, "Примечания", "mtext")])
        )
        self.assertTrue(
            has_notes_heading([TextItem(10, 40, 3, "Примечание:", "text")])
        )
        self.assertFalse(
            has_notes_heading(
                [TextItem(10, 40, 3, "Грунт (см. примечание 1)", "mtext")]
            )
        )


class ProjectLegendResolverTests(unittest.TestCase):
    @staticmethod
    def _catalog(status: str = "consistent", independent: int = 1) -> dict:
        return {
            "entries": [
                {
                    "id": "PLE-wall",
                    "label": "Противопожарная перегородка",
                    "status": status,
                    "independentSourceFileCount": independent,
                    "sourceFiles": ["6 - ТХ/plan.dwg"],
                    "referenceSignatures": ["blockdef-v1:abc"],
                }
            ]
        }

    @staticmethod
    def _result(local_legend: bool = False) -> PageResult:
        instance = _instance()
        legends = (
            [
                LegendEntry(
                    id="LE-local",
                    page=1,
                    label="Локальный конфликт",
                    status="extracted",
                    source_kind="dwg_vector_legend",
                    reference_signatures=("blockdef-v1:abc",),
                    confidence=1.0,
                )
            ]
            if local_legend
            else []
        )
        return PageResult(
            document_id="DOC-target",
            document_path="target.dwg",
            page=1,
            completeness="partial",
            legend_entries=legends,
            symbol_instances=[instance],
            unknown_symbols=[
                UnknownSymbolCluster(
                    id="US-old",
                    page=1,
                    signature=instance.signature,
                    instance_ids=(instance.id,),
                    reason="NO_LOCAL_MATCH",
                    representative_instance_id=instance.id,
                )
            ],
        )

    def test_unique_project_signature_adds_probable_binding(self) -> None:
        result = resolve_project_exact_blocks(self._result(), self._catalog())

        self.assertEqual("probable", result.symbol_instances[0].status)
        self.assertEqual(1, len(result.symbol_bindings))
        self.assertEqual(
            "project_exact_block_definition",
            result.symbol_bindings[0].evidence[0].kind,
        )
        self.assertEqual(0.75, result.symbol_bindings[0].confidence)
        self.assertEqual([], result.unknown_symbols)
        self.assertEqual(
            "project_legend_catalog",
            result.legend_entries[0].source_kind,
        )

    def test_sidecar_project_join_fills_ideal_legend_without_role_change(self) -> None:
        soil = FurnitureTests._insert(
            "SI-soil7",
            layer="Fill",
            block_name="круг7",
            status="unresolved",
            role="field_candidate",
            signature="blockdef-v1:krug7-join",
            x=250.0,
            y=430.0,
        )
        result = FurnitureTests._page([soil])
        result.sheet_zones = dict(SheetSceneTests.ZONES)
        result.sheet_scenes = [
            SheetScene(
                id="SC-xii",
                page=1,
                title="Разрез по линии XII-XII",
                bbox=(0.0, 55.0, 640.0, 594.0),
                soil_ids=(soil.id,),
            )
        ]
        catalog = {
            "entries": [
                {
                    "id": "PLE-sand",
                    "label": "Песок пылеватый коричневато-серый",
                    "status": "consistent",
                    "independentSourceFileCount": 1,
                    "sourceFiles": ["4 - КР/geo.dwg"],
                    "referenceSignatures": ["blockdef-v1:krug7-join"],
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            page_dir = write_page_result(directory, result)
            catalog_path = Path(directory) / "catalog.json"
            catalog_path.write_text(
                json.dumps(catalog, ensure_ascii=False),
                encoding="utf-8",
            )
            review_path = Path(directory) / "review.json"
            review_path.write_text(
                json.dumps({"probable": 0, "confirmed": 0, "anomalyCodes": []}),
                encoding="utf-8",
            )
            applied = apply_project_catalog_to_page_dir(page_dir, catalog_path)
            loaded = load_page_result(page_dir)
            markdown = render_ideal_markdown(page_dir)
            review = json.loads(review_path.read_text(encoding="utf-8"))
        self.assertEqual("probable", applied.symbol_instances[0].status)
        self.assertEqual("field_candidate", applied.symbol_instances[0].role)
        self.assertEqual((soil.id,), loaded.sheet_scenes[0].soil_ids)
        self.assertEqual("probable", loaded.symbol_instances[0].status)
        self.assertIn("⑦ Песок пылеватый коричневато-серый", markdown)
        self.assertIn("слои ⑦", markdown)
        self.assertEqual(1, review["probable"])
        self.assertEqual(0, review["confirmed"])

    def test_conflicting_external_or_local_signature_is_not_applied(self) -> None:
        conflicting = resolve_project_exact_blocks(
            self._result(),
            self._catalog(status="conflicting"),
        )
        external = resolve_project_exact_blocks(
            self._result(),
            self._catalog(status="external_only", independent=0),
        )
        local = resolve_project_exact_blocks(
            self._result(local_legend=True),
            self._catalog(),
        )

        for result in (conflicting, external, local):
            self.assertEqual("unresolved", result.symbol_instances[0].status)
            self.assertEqual([], result.symbol_bindings)
            self.assertEqual(1, len(result.unknown_symbols))

    def test_application_summary_separates_local_and_project_matches(self) -> None:
        records = [
            {
                "fileId": "PF-one",
                "relativePath": "6 - ТХ/plan.dwg",
                "discipline": "6 - ТХ",
                "externalReference": False,
                "status": "processed",
                "pageCount": 1,
                "processedPages": 1,
                "legendPages": [],
                "errors": [],
                "pageResults": [
                    {
                        "page": 1,
                        "localConfirmed": 2,
                        "localProbable": 3,
                        "projectProbable": 4,
                        "projectLabels": ["Тип А", "Тип Б"],
                        "unresolvedCandidates": 5,
                        "unknownClusters": 2,
                    }
                ],
            }
        ]

        summary = build_application_summary(
            records,
            package_root="new_files/dwg",
            catalog_path="project_legend_catalog.json",
        )

        self.assertEqual(4, summary["counts"]["projectProbable"])
        self.assertEqual(2, summary["counts"]["projectLabelsApplied"])
        self.assertEqual(5, summary["counts"]["unresolvedCandidates"])
        self.assertEqual("probable", summary["policy"]["projectMatchesStatus"])


class ReviewTests(unittest.TestCase):
    def test_furniture_without_bbox_uses_gost_stamp_rectangle(self) -> None:
        stamp = replace(
            _instance(),
            id="SI-stamp",
            status="ignored",
            role="sheet_furniture",
            layer="FORMAT",
            block_name="*U704",
            bbox=None,
            classification_reason="FORMAT_STAMP_ATTRIBUTES",
            position=Point(830.0, 10.0, "paper", "mm"),
        )
        meta = {"bbox": [0.0, 0.0, 841.0, 594.0]}
        bbox = _instance_bbox(stamp, meta)
        self.assertAlmostEqual(_STAMP_WIDTH_MM, bbox[2] - bbox[0])
        self.assertAlmostEqual(_STAMP_HEIGHT_MM, bbox[3] - bbox[1])
        self.assertLess(bbox[0], stamp.position.x)
        self.assertGreaterEqual(bbox[1], stamp.position.y)
        self.assertLessEqual(bbox[2], 841.0)

    def test_field_candidate_without_bbox_stays_a_small_marker(self) -> None:
        trap = replace(_instance(), bbox=None, position=Point(830.0, 10.0, "paper", "mm"))
        bbox = _instance_bbox(trap, {"bbox": [0.0, 0.0, 841.0, 594.0]})
        self.assertAlmostEqual(12.0, bbox[2] - bbox[0])
        self.assertAlmostEqual(12.0, bbox[3] - bbox[1])

    def test_overlay_marks_ignored_furniture_with_gray_f(self) -> None:
        stamp = replace(
            _instance(),
            id="SI-stamp",
            status="ignored",
            role="sheet_furniture",
            layer="FORMAT",
            block_name="*U704",
            bbox=None,
            classification_reason="FORMAT_STAMP_ATTRIBUTES",
            position=Point(100.0, 20.0, "paper", "mm"),
        )
        confirmed = replace(
            _instance(),
            id="SI-ok",
            status="confirmed",
            legend_entry_id="LE-1",
            bbox=(10.0, 10.0, 16.0, 16.0),
        )
        primitives = [{"points": [[0, 0], [200, 0]], "color": "#000000", "lw": 0.25}]
        meta = {"bbox": [0.0, 0.0, 200.0, 100.0]}
        svg = _overlay_svg(primitives, meta, [confirmed, stamp])
        self.assertIn("F — оформление листа", svg)
        self.assertIn("O — объект чертежа", svg)
        self.assertIn("A — аннотация", svg)
        self.assertIn("M — марка / подпись", svg)
        self.assertIn("зона основной надписи", svg)
        self.assertIn(f'fill="{_FURNITURE_COLOR}">F</text>', svg)
        self.assertIn(f'stroke="{_FURNITURE_COLOR}"', svg)
        self.assertIn(">C</text>", svg)
        self.assertNotIn(">I</text>", svg)

    def test_overlay_draws_title_block_zone_without_a_letter(self) -> None:
        primitives = [{"points": [[0, 0], [841, 0]], "color": "#000000", "lw": 0.25}]
        meta = {"bbox": [0.0, 0.0, 841.0, 594.0]}
        zones = {
            "title_block": [656.0, 0.0, 841.0, 55.0],
            "drawing_field": [0.0, 55.0, 841.0, 594.0],
        }
        svg = _overlay_svg(primitives, meta, [], sheet_zones=zones)
        self.assertIn('id="title-block-zone"', svg)
        self.assertIn("зона основной надписи", svg)
        self.assertIn('width="185.000"', svg)
        self.assertIn('height="55.000"', svg)
        self.assertIn('fill="none"', svg)
        self.assertIn('stroke-dasharray="4 2"', svg)
        self.assertNotIn(">T</text>", svg)
        self.assertNotIn(f'fill="{_FURNITURE_COLOR}">F</text>', svg)
        self.assertNotIn('id="drawing-field"', svg)
        self.assertNotIn("drawing_field", svg)
        zone_markup = svg.split('id="title-block-zone"')[1].split("</g>")[0]
        self.assertNotIn("fill=\"#", zone_markup)
        self.assertNotIn('id="legend-zone"', svg)
        self.assertNotIn(">L</text>", svg)

    def test_overlay_draws_legend_zone_without_a_letter(self) -> None:
        primitives = [{"points": [[0, 0], [841, 0]], "color": "#000000", "lw": 0.25}]
        meta = {"bbox": [0.0, 0.0, 841.0, 594.0]}
        zones = {
            "title_block": [656.0, 0.0, 841.0, 55.0],
            "drawing_field": [0.0, 55.0, 841.0, 594.0],
            "legend": [50.0, 80.0, 220.0, 210.0],
            "notes": [50.0, 40.0, 220.0, 72.0],
        }
        svg = _overlay_svg(primitives, meta, [], sheet_zones=zones)
        self.assertIn('id="legend-zone"', svg)
        self.assertIn("зона легенды", svg)
        self.assertIn('id="notes-zone"', svg)
        self.assertIn("зона примечаний", svg)
        self.assertIn('stroke-dasharray="4 2"', svg)
        self.assertNotIn(">L</text>", svg)
        self.assertNotIn(">N</text>", svg)
        self.assertNotIn('id="drawing-field"', svg)
        legend_markup = svg.split('id="legend-zone"')[1].split("</g>")[0]
        self.assertIn('stroke-dasharray="4 2"', legend_markup)
        self.assertNotIn("<text", legend_markup)
        notes_markup = svg.split('id="notes-zone"')[1].split("</g>")[0]
        self.assertNotIn("<text", notes_markup)

    def test_overlay_draws_field_geometry_without_a_letter(self) -> None:
        primitives = [{"points": [[0, 0], [200, 0]], "color": "#000000", "lw": 0.25}]
        meta = {"bbox": [0.0, 0.0, 200.0, 100.0]}
        item = FieldGeometry(
            id="FG-wall",
            page=1,
            kind="line",
            layer="WALL",
            color="#000000",
            lineweight=0.25,
            bbox=(20.0, 40.0, 80.0, 41.0),
            label="",
        )
        svg = _overlay_svg(primitives, meta, [], field_geometry=[item])
        self.assertIn('id="field-geometry-FG-wall"', svg)
        self.assertIn("линия / штриховка", svg)
        self.assertIn(_GEOMETRY_COLOR, svg)
        self.assertIn('stroke-dasharray="4 2"', svg)
        self.assertNotIn(">G</text>", svg)
        self.assertNotIn(">L</text>", svg)
        markup = svg.split('id="field-geometry-FG-wall"')[1].split("</g>")[0]
        self.assertNotIn("<text", markup)

    def test_overlay_marks_text_labels_with_t(self) -> None:
        primitives = [{"points": [[0, 0], [200, 0]], "color": "#000000", "lw": 0.25}]
        meta = {"bbox": [0.0, 0.0, 200.0, 100.0]}
        label = TextLabel(
            id="TL-axis",
            page=1,
            kind="axis",
            text="Ж / 7",
            letter="Ж",
            digit="7",
            value="",
            source="text",
            layer="OSI",
            x=50.0,
            y=50.0,
            bbox=(47.5, 47.5, 52.5, 52.5),
        )
        svg = _overlay_svg(primitives, meta, [], text_labels=[label])
        self.assertIn(f'fill="{_TEXT_LABEL_COLOR}">T</text>', svg)
        self.assertIn("T — текст вне блока", svg)
        self.assertIn("Ж / 7", svg)

    def test_overlay_marks_objects_annotations_and_spec_marks(self) -> None:
        column = replace(
            _instance(),
            id="SI-column",
            status="ignored",
            role="drawing_object",
            layer="колонна",
            block_name="Колонна",
            classification_reason="COLUMN_BLOCK_LAYER",
            bbox=(20.0, 20.0, 28.0, 28.0),
        )
        dim = replace(
            _instance(),
            id="SI-dim",
            status="ignored",
            role="drawing_annotation",
            layer="RAZMER",
            block_name="*U382",
            classification_reason="SERVICE_LAYER",
            bbox=(40.0, 40.0, 48.0, 48.0),
        )
        axis = replace(
            _instance(),
            id="SI-axis",
            status="ignored",
            role="specification_mark",
            layer="OSI",
            block_name="*U12",
            classification_reason="AXIS_LAYER",
            bbox=(60.0, 20.0, 68.0, 28.0),
        )
        trap = replace(
            _instance(),
            id="SI-trap",
            status="ignored",
            role="drawing_object",
            layer="Технология",
            block_name="трап100",
            classification_reason="TRAP_BLOCK_LAYER",
            bbox=(80.0, 20.0, 88.0, 28.0),
        )
        fachwerk = replace(
            _instance(),
            id="SI-fachwerk",
            status="ignored",
            role="drawing_object",
            layer="МЕТАЛЛ ФАХВЕРК",
            block_name="*U33",
            classification_reason="ANONYMOUS_CONSTRUCTION_LAYER",
            bbox=(100.0, 20.0, 108.0, 28.0),
        )
        primitives = [{"points": [[0, 0], [200, 0]], "color": "#000000", "lw": 0.25}]
        meta = {"bbox": [0.0, 0.0, 200.0, 100.0]}
        svg = _overlay_svg(primitives, meta, [column, dim, axis, trap, fachwerk])
        self.assertIn(f'fill="{_OBJECT_COLOR}">O</text>', svg)
        self.assertIn(f'fill="{_ANNOTATION_COLOR}">A</text>', svg)
        self.assertIn(f'fill="{_MARK_COLOR}">M</text>', svg)
        self.assertNotIn(">T</text>", svg)
        self.assertEqual(3, svg.count(f'fill="{_OBJECT_COLOR}">O</text>'))

    def test_review_instances_keep_furniture_out_of_unknown(self) -> None:
        stamp = replace(
            _instance(),
            id="SI-stamp",
            status="ignored",
            role="sheet_furniture",
            layer="FORMAT",
            block_name="*U704",
            classification_reason="FORMAT_STAMP_ATTRIBUTES",
        )
        trap = replace(
            _instance(),
            id="SI-trap",
            status="ignored",
            role="drawing_object",
            layer="Технология",
            block_name="трап100",
            classification_reason="TRAP_BLOCK_LAYER",
        )
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="partial",
            symbol_instances=[stamp, trap],
            unknown_symbols=[],
        )
        buckets = _review_instances(result)
        self.assertEqual([], buckets.recognized)
        self.assertEqual([], buckets.unknown)
        self.assertEqual(["SI-stamp"], [item.id for item in buckets.furniture])
        self.assertEqual(["SI-trap"], [item.id for item in buckets.drawing_objects])
        self.assertEqual([], buckets.drawing_annotations)
        self.assertEqual([], buckets.specification_marks)

    def test_summarize_reviews_writes_machine_and_human_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selection = root / "selection.json"
            selection.write_text(
                json.dumps(
                    {
                        "fixtures": [
                            {"id": "one"},
                            {"id": "two"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            base = {
                "documentPath": "drawing.dwg",
                "page": 1,
                "completeness": "partial",
                "legendEntries": 2,
                "recognized": 3,
                "confirmed": 1,
                "probable": 2,
                "unrecognizedCandidates": 4,
                "unknownClusters": 2,
                "unknownOccurrences": 4,
                "sheetFurniture": 1,
                "sheetFurnitureClusters": 1,
                "drawingObjects": 3,
                "drawingObjectClusters": 1,
                "drawingAnnotations": 2,
                "drawingAnnotationClusters": 1,
                "specificationMarks": 5,
                "specificationMarkClusters": 2,
                "bindings": 3,
                "bindingEvidence": {"exact_block_definition": 1},
                "anomalyCodes": ["RELATIONSHIP_STAGE_NOT_RUN"],
            }
            for fixture_id in ("one", "two"):
                target = root / fixture_id
                target.mkdir()
                (target / "review.json").write_text(
                    json.dumps({"id": fixture_id, **base}),
                    encoding="utf-8",
                )
            report = summarize_reviews(selection, root)
            self.assertEqual(2, report["documents"])
            self.assertEqual(6, report["totals"]["recognized"])
            self.assertEqual(2, report["totals"]["sheetFurniture"])
            self.assertEqual(2, report["totals"]["sheetFurnitureClusters"])
            self.assertEqual(6, report["totals"]["drawingObjects"])
            self.assertEqual(4, report["totals"]["drawingAnnotations"])
            self.assertEqual(10, report["totals"]["specificationMarks"])
            csv_header = (root / "statistics.csv").read_text("utf-8").splitlines()[0]
            self.assertIn("sheetFurniture", csv_header)
            self.assertIn("drawingObjects", csv_header)
            self.assertIn("specificationMarks", csv_header)
            self.assertTrue((root / "statistics.json").exists())
            self.assertTrue((root / "statistics.csv").exists())
            self.assertIn("confirmed/probable", (root / "REPORT.md").read_text("utf-8"))
            report_text = (root / "REPORT.md").read_text("utf-8")
            self.assertIn("Оформление листа: 2", report_text)
            self.assertIn("оформление листа: 1", report_text)
            self.assertIn("Объекты чертежа: 6", report_text)
            self.assertIn("Аннотации: 4", report_text)
            self.assertIn("Марки / подписи: 10", report_text)
            self.assertIn("sheetFurnitureClusters", csv_header)

    def test_html_report_contains_images_descriptions_and_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selection = root / "selection.json"
            selection.write_text(
                json.dumps(
                    {
                        "fixtures": [
                            {
                                "id": "sheet-one",
                                "documentPath": "drawings/<plan>.dwg",
                                "page": 1,
                                "selectionReason": "Проверочный лист",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            sheet = root / "sheet-one"
            page = sheet / "dwg_symbols" / "page_0001"
            legend_crops = page / "legend_crops"
            symbol_crops = page / "symbol_crops"
            legend_crops.mkdir(parents=True)
            symbol_crops.mkdir()

            def sidecar(name: str, items: list[dict]) -> None:
                (page / name).write_text(
                    json.dumps(
                        {"schemaVersion": 1, "page": 1, "items": items},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            sidecar(
                "legend_entries.json",
                [
                    {
                        "id": "LE-1",
                        "label": "Стена <огнестойкая>",
                        "status": "extracted",
                        "confidence": 0.9,
                        "symbolBbox": [0, 0, 10, 10],
                        "cropPath": "legend_crops/LE-1.svg",
                    }
                ],
            )
            instance = {
                "id": "SI-1",
                "sourceKind": "dwg_insert_candidate",
                "position": {"x": 10, "y": 20, "units": "mm"},
                "layer": "WALL",
                "blockName": "FIRE",
                "attributes": {},
            }
            unknown = {
                **instance,
                "id": "SI-2",
                "position": {"x": 30, "y": 40, "units": "mm"},
            }
            furniture = {
                **instance,
                "id": "SI-3",
                "role": "sheet_furniture",
                "status": "ignored",
                "layer": "FORMAT",
                "blockName": "*U704",
                "classificationReason": "FORMAT_STAMP_ATTRIBUTES",
                "attributes": {"ЛИСТ": "1", "СТАДИЯ": "Р", "ГИП": "Иванов"},
                "position": {"x": 830, "y": 10, "units": "mm"},
            }
            sidecar("symbol_instances.json", [instance, unknown, furniture])
            sidecar(
                "symbol_bindings.json",
                [
                    {
                        "instanceId": "SI-1",
                        "legendEntryId": "LE-1",
                        "status": "confirmed",
                        "confidence": 1.0,
                        "evidence": [
                            {
                                "kind": "exact_block_definition",
                                "detail": "same block",
                            }
                        ],
                    }
                ],
            )
            sidecar(
                "unrecognized_symbols.json",
                [
                    {
                        "id": "US-1",
                        "reason": "NO_EXACT_LEGEND_BLOCK_MATCH_H4",
                        "signature": "blockdef-v1:test",
                        "instanceIds": ["SI-2"],
                        "representativeInstanceId": "SI-2",
                        "occurrences": 1,
                    }
                ],
            )
            for image in (
                legend_crops / "LE-1.svg",
                symbol_crops / "recognized-SI-1.svg",
                symbol_crops / "unknown-SI-2.svg",
                symbol_crops / "furniture-SI-3.svg",
            ):
                image.write_text("<svg></svg>", encoding="utf-8")
            (page / "summary.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "titleBlock": {
                            "code": "",
                            "sheet": "1",
                            "sheetsTotal": "8",
                            "stage": "П",
                            "source": "attributes",
                            "note": "шифр не прочитан",
                            "instanceId": "SI-3",
                        },
                        "sheetZones": {
                            "title_block": [645.0, 10.0, 830.0, 65.0],
                            "drawing_field": [0.0, 65.0, 841.0, 594.0],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (sheet / "review.json").write_text(
                json.dumps(
                    {
                        "page": 1,
                        "completeness": "partial",
                        "legendEntries": 1,
                        "confirmed": 1,
                        "probable": 0,
                        "unknownClusters": 1,
                        "unknownOccurrences": 1,
                        "sheetFurniture": 1,
                        "sheetFurnitureClusters": 1,
                        "anomalyCodes": [],
                        "cropIndex": [
                            {
                                "instanceId": "SI-1",
                                "path": (
                                    "dwg_symbols/page_0001/symbol_crops/"
                                    "recognized-SI-1.svg"
                                ),
                            },
                            {
                                "instanceId": "SI-2",
                                "path": (
                                    "dwg_symbols/page_0001/symbol_crops/"
                                    "unknown-SI-2.svg"
                                ),
                            },
                            {
                                "instanceId": "SI-3",
                                "category": "furniture",
                                "path": (
                                    "dwg_symbols/page_0001/symbol_crops/"
                                    "furniture-SI-3.svg"
                                ),
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            index = write_html_reports(selection, root)
            report = (sheet / "report.html").read_text(encoding="utf-8")
            index_html = index.read_text(encoding="utf-8")
            self.assertTrue(index.exists())
            self.assertIn("sheet-one/report.html", index_html)
            self.assertIn("Оформление", index_html)
            self.assertIn("Марки", index_html)
            self.assertIn("Стена &lt;огнестойкая&gt;", report)
            self.assertIn("Определение DWG-блока точно совпадает", report)
            self.assertIn("Семантическое значение не определено", report)
            self.assertIn("Оформление листа", report)
            self.assertIn("Марки / подписи", report)
            self.assertIn("*U704", report)
            self.assertIn("FORMAT", report)
            self.assertIn("Слой FORMAT и атрибуты основной надписи", report)
            self.assertIn("ЛИСТ", report)
            furniture_html = report.split("Оформление листа")[1].split("Нераспознанные")[0]
            self.assertIn("class=\"grafa\"", furniture_html)
            self.assertIn("Стадия", furniture_html)
            self.assertIn("Лист", furniture_html)
            self.assertIn("Листов", furniture_html)
            self.assertIn("Шифр", furniture_html)
            self.assertIn("не прочитан", furniture_html)
            self.assertIn("П", furniture_html)
            self.assertIn(">8</dd>", furniture_html)
            self.assertLess(
                furniture_html.find("class=\"grafa\""),
                furniture_html.find("Атрибуты DWG-блока"),
            )
            self.assertNotIn("furniture-SI-3.svg", report.split("Нераспознанные")[1])
            self.assertIn("furniture-SI-3.svg", report.split("Оформление листа")[1].split("Нераспознанные")[0])
            self.assertEqual(4, report.count("<img "))

    def test_html_report_shows_exploded_title_block_without_insert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selection = root / "selection.json"
            selection.write_text(
                json.dumps(
                    {
                        "fixtures": [
                            {
                                "id": "foundations",
                                "documentPath": "foundations.dwg",
                                "page": 1,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            sheet = root / "foundations"
            page = sheet / "dwg_symbols" / "page_0001"
            page.mkdir(parents=True)

            def sidecar(name: str, items: list[dict]) -> None:
                (page / name).write_text(
                    json.dumps(
                        {"schemaVersion": 1, "page": 1, "items": items},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

            sidecar("legend_entries.json", [])
            sidecar("symbol_instances.json", [])
            sidecar("symbol_bindings.json", [])
            sidecar("unrecognized_symbols.json", [])
            (page / "summary.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "titleBlock": {
                            "code": "",
                            "sheet": "3",
                            "sheetsTotal": "",
                            "stage": "П",
                            "source": "geometry",
                            "note": "шифр не прочитан",
                            "instanceId": None,
                        },
                        "sheetZones": {
                            "title_block": [656.0, 0.0, 841.0, 55.0],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (sheet / "review.json").write_text(
                json.dumps(
                    {
                        "page": 1,
                        "completeness": "partial",
                        "legendEntries": 0,
                        "confirmed": 0,
                        "probable": 0,
                        "unknownClusters": 0,
                        "unknownOccurrences": 0,
                        "sheetFurniture": 0,
                        "anomalyCodes": [],
                        "cropIndex": [],
                    }
                ),
                encoding="utf-8",
            )
            write_html_reports(selection, root)
            report = (sheet / "report.html").read_text(encoding="utf-8")
            furniture_html = report.split("Оформление листа")[1]
            self.assertIn("Основная надпись", furniture_html)
            self.assertIn("class=\"grafa\"", furniture_html)
            self.assertIn("<dd>3</dd>", furniture_html)
            self.assertIn("не прочитан", furniture_html)
            self.assertNotIn("Атрибуты DWG-блока", furniture_html)


class TitleBlockTests(unittest.TestCase):
    """Merge INSERT attributes with stamp.read; fail-closed on empty ШИФР."""

    SHEET_BBOX = (0.0, 0.0, 841.0, 594.0)
    CODE = "28-ХСА-1/25-КР1"

    @staticmethod
    def _stamp_insert(
        *,
        instance_id: str = "SI-stamp",
        attributes: dict[str, str] | None = None,
        reason: str = FORMAT_STAMP_REASON,
        layer: str = "FORMAT",
        block_name: str = "*U704",
        x: float = 830.0,
        y: float = 10.0,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> SymbolInstance:
        return SymbolInstance(
            id=instance_id,
            page=1,
            source_kind="dwg_insert_candidate",
            status="ignored",
            position=Point(x, y, "paper", "mm"),
            source_handle=instance_id.replace("SI-", ""),
            source_space="modelspace:viewport:0",
            layer=layer,
            signature=f"blockdef-v1:{block_name}",
            block_name=block_name,
            role="sheet_furniture",
            bbox=bbox,
            attributes=attributes or {},
            confidence=1.0,
            classification_reason=reason,
        )

    @staticmethod
    def _gost_texts(
        *,
        sheet: str = "19",
        sheets_total: str = "8",
        stage: str = "П",
        code: str | None = "28-ХСА-1/25-КР1",
        title: str = "Схема расположения фундаментов",
        extra: list[TextItem] | None = None,
        stage_x: float = 800.0,
        stage_y: float = 50.0,
    ) -> list[TextItem]:
        items = [
            TextItem(stage_x, stage_y, 2.5, "Стадия", "text"),
            TextItem(stage_x + 15.0, stage_y, 2.5, "Лист", "text"),
            TextItem(stage_x + 35.0, stage_y, 2.5, "Листов", "text"),
            TextItem(stage_x, stage_y - 8.0, 3.5, stage, "text"),
            TextItem(stage_x + 15.0, stage_y - 8.0, 3.5, sheet, "text"),
            TextItem(stage_x + 35.0, stage_y - 8.0, 3.5, sheets_total, "text"),
            TextItem(stage_x - 80.0, stage_y - 30.0, 4.0, title, "text"),
        ]
        if code:
            items.append(
                TextItem(stage_x - 80.0, stage_y + 30.0, 7.0, code, "text")
            )
        items.extend(extra or [])
        return items

    def test_attributes_fill_sheet_stage_and_sheets_total(self) -> None:
        stamp = self._stamp_insert(
            attributes={"ЛИСТ": "1", "СТАДИЯ": "П", "ЛИСТОВ": "8", "ФОРМАТ": "А1"}
        )
        title = read_title_block([stamp], texts=[], sheet_bbox=self.SHEET_BBOX)
        self.assertTrue(is_title_block_insert(stamp))
        self.assertEqual("1", title.sheet)
        self.assertEqual("8", title.sheets_total)
        self.assertEqual("П", title.stage)
        self.assertEqual("", title.code)
        self.assertEqual("attributes", title.source)
        self.assertEqual("SI-stamp", title.instance_id)
        self.assertEqual("шифр не прочитан", title.note)

    def test_empty_cipher_attribute_does_not_invent_a_code(self) -> None:
        stamp = self._stamp_insert(
            attributes={
                "ЛИСТ": "1",
                "СТАДИЯ": "П",
                "ЛИСТОВ": "8",
                "ШИФР": "",
                "ШИФР(1)": "Иванов",
            }
        )
        far_code = TextItem(120.0, 400.0, 7.0, self.CODE, "text")
        title = read_title_block([stamp], texts=[far_code], sheet_bbox=self.SHEET_BBOX)
        self.assertEqual("", title.code)
        self.assertEqual("1", title.sheet)
        self.assertEqual("шифр не прочитан", title.note)
        self.assertNotIn("надпись не найдена", title.note)

    def test_empty_cipher_attribute_does_not_block_geometry_code(self) -> None:
        stamp = self._stamp_insert(
            attributes={
                "ЛИСТ": "19",
                "СТАДИЯ": "П",
                "ЛИСТОВ": "8",
                "ШИФР": "",
            }
        )
        title = read_title_block(
            [stamp],
            self._gost_texts(),
            sheet_bbox=self.SHEET_BBOX,
        )
        self.assertEqual(self.CODE, title.code)
        self.assertEqual("merged", title.source)
        self.assertNotIn("шифр не прочитан", title.note)

    def test_sheet_texts_supply_code_through_stamp_read(self) -> None:
        stamp = self._stamp_insert(
            attributes={"ЛИСТ": "1", "СТАДИЯ": "П", "ЛИСТОВ": "8"}
        )
        title = read_title_block(
            [stamp],
            self._gost_texts(sheet="1", sheets_total="8", stage="П"),
            sheet_bbox=self.SHEET_BBOX,
        )
        self.assertEqual(self.CODE, title.code)
        self.assertEqual("1", title.sheet)
        self.assertEqual("8", title.sheets_total)
        self.assertEqual("П", title.stage)
        self.assertEqual("Схема расположения фундаментов", title.title)
        self.assertEqual("merged", title.source)
        self.assertEqual("SI-stamp", title.instance_id)
        self.assertEqual("", title.note)

    def test_filled_attribute_wins_conflict_with_geometry(self) -> None:
        stamp = self._stamp_insert(
            attributes={"ЛИСТ": "1", "СТАДИЯ": "П", "ЛИСТОВ": "8"}
        )
        title = read_title_block(
            [stamp],
            self._gost_texts(sheet="19", sheets_total="24", stage="Р"),
            sheet_bbox=self.SHEET_BBOX,
        )
        self.assertEqual("1", title.sheet)
        self.assertEqual("8", title.sheets_total)
        self.assertEqual("П", title.stage)
        self.assertEqual(self.CODE, title.code)
        self.assertEqual("merged", title.source)
        self.assertIn("конфликт листа: атрибут 1, геометрия 19; принят атрибут", title.note)
        self.assertIn("конфликт листов: атрибут 8, геометрия 24; принят атрибут", title.note)
        self.assertIn("конфликт стадии: атрибут П, геометрия Р; принят атрибут", title.note)
        self.assertNotIn("шифр не прочитан", title.note)

    def test_signature_block_is_not_the_title_block(self) -> None:
        visa = self._stamp_insert(
            instance_id="SI-sign",
            attributes={"ЛИСТ": "99", "СТАДИЯ": "Р", "ЛИСТОВ": "3"},
            reason=SIGNATURE_BLOCK_REASON,
            layer="Подписи",
            block_name="подпись",
            x=40.0,
            y=20.0,
        )
        self.assertFalse(is_title_block_insert(visa))
        title = read_title_block(
            [visa],
            self._gost_texts(),
            sheet_bbox=self.SHEET_BBOX,
        )
        self.assertIsNone(title.instance_id)
        self.assertEqual("19", title.sheet)
        self.assertEqual("П", title.stage)
        self.assertEqual(self.CODE, title.code)
        self.assertEqual("geometry", title.source)
        self.assertNotEqual("99", title.sheet)

    def test_exploded_stamp_without_insert_has_no_instance(self) -> None:
        title = read_title_block(
            [],
            self._gost_texts(),
            sheet_bbox=self.SHEET_BBOX,
        )
        self.assertIsNone(title.instance_id)
        self.assertEqual("geometry", title.source)
        self.assertEqual("19", title.sheet)
        self.assertEqual("8", title.sheets_total)
        self.assertEqual("П", title.stage)
        self.assertEqual(self.CODE, title.code)
        self.assertEqual("Схема расположения фундаментов", title.title)
        self.assertEqual("", title.note)
        self.assertIsNotNone(title.bbox)
        self.assertEqual(4, len(title.bbox))
        width = title.bbox[2] - title.bbox[0]
        height = title.bbox[3] - title.bbox[1]
        self.assertAlmostEqual(185.0, width, places=1)
        self.assertAlmostEqual(55.0, height, places=1)

    def test_note_distinguishes_missing_title_block_from_unread_code(self) -> None:
        missing = read_title_block([], texts=[], sheet_bbox=self.SHEET_BBOX)
        self.assertEqual("надпись не найдена", missing.note)
        self.assertEqual("", missing.code)
        self.assertIsNone(missing.instance_id)

        exploded = read_title_block(
            [],
            self._gost_texts(code=None),
            sheet_bbox=self.SHEET_BBOX,
        )
        self.assertEqual("шифр не прочитан", exploded.note)
        self.assertEqual("", exploded.code)
        self.assertEqual("19", exploded.sheet)
        self.assertIsNone(exploded.instance_id)
        self.assertNotEqual(missing.note, exploded.note)


class DimensionTests(unittest.TestCase):
    """Read size/elevation numbers; visibility tags and empty ОТМЕТКА are not values."""

    def _classified(self, *inserts: SymbolInstance) -> PageResult:
        return classify_sheet_furniture(FurnitureTests._page(list(inserts)))

    def test_otmetka_attribute_is_elevation(self) -> None:
        mark = FurnitureTests._insert(
            "SI-level",
            layer="OTMETKI",
            block_name="*U302",
            attributes={"ОТМЕТКА": "-2.950", "ПРИМ.": "Ур.ч.п."},
        )
        result = attach_dimensions(self._classified(mark))
        parsed = result.symbol_instances[0].dimension
        self.assertEqual("-2.950", parsed["value"])
        self.assertEqual("elevation", parsed["kind"])
        self.assertEqual("attributes", parsed["source"])
        self.assertEqual("", parsed["note"])

    def test_empty_otmetka_is_not_absence(self) -> None:
        mark = FurnitureTests._insert(
            "SI-level",
            layer="OTMETKI",
            block_name="*U302",
            attributes={"ОТМЕТКА": "", "ПРИМ.": ""},
        )
        result = attach_dimensions(self._classified(mark))
        parsed = result.symbol_instances[0].dimension
        self.assertEqual("", parsed["value"])
        self.assertEqual("отметка не прочитана", parsed["note"])
        self.assertEqual("", parsed["source"])

    def test_visibility_tags_are_not_a_linear_size(self) -> None:
        dim = FurnitureTests._insert(
            "SI-dim",
            layer="RAZMER",
            block_name="*U382",
            attributes={"В": "1 (2)", "Г": "1 (6)", "ВГ": "1 (2)", "ГГ": "1 (6)"},
        )
        result = attach_dimensions(self._classified(dim))
        parsed = result.symbol_instances[0].dimension
        self.assertEqual("", parsed["value"])
        self.assertEqual("linear", parsed["kind"])
        self.assertEqual("число не прочитано", parsed["note"])

    def test_nearby_text_fills_linear_size(self) -> None:
        dim = FurnitureTests._insert(
            "SI-dim",
            layer="RAZMER",
            block_name="A$C8d897611",
            x=100.0,
            y=200.0,
        )
        texts = [TextItem(100.0, 200.5, 2.5, "3500", "dimension")]
        result = attach_dimensions(self._classified(dim), texts)
        parsed = result.symbol_instances[0].dimension
        self.assertEqual("3500", parsed["value"])
        self.assertEqual("text", parsed["source"])
        self.assertEqual("linear", parsed["kind"])

    def test_two_nearby_numbers_are_ambiguous(self) -> None:
        dim = FurnitureTests._insert(
            "SI-dim",
            layer="RAZMER",
            block_name="*U1",
            x=100.0,
            y=200.0,
        )
        texts = [
            TextItem(100.0, 200.0, 2.5, "3500", "text"),
            TextItem(100.0, 200.0, 2.5, "1200", "text"),
        ]
        result = attach_dimensions(self._classified(dim), texts)
        parsed = result.symbol_instances[0].dimension
        self.assertEqual("", parsed["value"])
        self.assertEqual("несколько чисел рядом", parsed["note"])

    def test_height_mark_is_not_linear_chain(self) -> None:
        mark = FurnitureTests._insert(
            "SI-height",
            layer="0",
            block_name="высоты 1.1",
        )
        result = attach_dimensions(self._classified(mark))
        parsed = result.symbol_instances[0].dimension
        self.assertEqual("height", parsed["kind"])
        self.assertEqual("", parsed["value"])
        self.assertNotEqual("linear", parsed["kind"])

    def test_nadpisi_and_slope_are_not_read(self) -> None:
        label = FurnitureTests._insert(
            "SI-open",
            layer="NADPISI",
            block_name="_Open30",
        )
        slope = FurnitureTests._insert(
            "SI-slope",
            layer="0",
            block_name="уклон",
        )
        result = attach_dimensions(self._classified(label, slope))
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertIsNone(by_id["SI-open"].dimension)
        self.assertIsNone(by_id["SI-slope"].dimension)
        self.assertEqual("drawing_annotation", by_id["SI-open"].role)
        self.assertEqual("field_candidate", by_id["SI-slope"].role)

    def test_column_and_stamp_stay_untouched(self) -> None:
        stamp = FurnitureTests._insert(
            "SI-stamp",
            layer="FORMAT",
            block_name="*U704",
            attributes={"ЛИСТ": "1", "СТАДИЯ": "П", "ГИП": "Иванов", "ФОРМАТ": "А1"},
        )
        column = FurnitureTests._insert(
            "SI-column",
            layer="колонна",
            block_name="Колонна",
        )
        mark = FurnitureTests._insert(
            "SI-level",
            layer="OTMETKI",
            block_name="*U302",
            attributes={"ОТМЕТКА": "0.000"},
        )
        result = attach_dimensions(self._classified(stamp, column, mark))
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertEqual("sheet_furniture", by_id["SI-stamp"].role)
        self.assertIsNone(by_id["SI-stamp"].dimension)
        self.assertEqual("drawing_object", by_id["SI-column"].role)
        self.assertIsNone(by_id["SI-column"].dimension)
        self.assertEqual("0.000", by_id["SI-level"].dimension["value"])

    def test_html_dimension_card_shows_number_above_attributes(self) -> None:
        from dwg_symbols.html_report import _classified_card

        html = _classified_card(
            {
                "id": "SI-level",
                "role": "drawing_annotation",
                "classificationReason": SERVICE_LAYER_REASON,
                "layer": "OTMETKI",
                "blockName": "*U302",
                "sourceKind": "dwg_insert_candidate",
                "position": {"x": 1.0, "y": 2.0, "space": "paper", "units": "mm"},
                "attributes": {"ОТМЕТКА": "-2.950", "ПРИМ.": "Ур.ч.п."},
            },
            None,
            Path("."),
        )
        self.assertIn("-2.950", html)
        self.assertIn("Отметка", html)
        self.assertIn("ОТМЕТКА", html)
        otmetka_at = html.index("-2.950")
        attrs_at = html.index("Атрибуты DWG-блока")
        self.assertLess(otmetka_at, attrs_at)

    def test_confirmed_legend_match_is_not_a_dimension(self) -> None:
        hit = FurnitureTests._insert(
            "SI-legend",
            layer="I-WALL",
            block_name="грунт",
            status="confirmed",
            role="field_candidate",
            legend_entry_id="LE-soil",
            classification_reason=None,
        )
        parsed = read_dimension(hit)
        self.assertIsNone(parsed)


class AxisTests(unittest.TestCase):
    """Read axis letter/digit by value; extra markers and room numbers are not the axis."""

    MARKERS = {
        "1_ДОП.МАРКЕР": "12",
        "2_ДОП.МАРКЕР": "4",
        "Доп.маркеры": "1, 2, 3, 4",
        "Направление": "",
    }

    def _classified(self, *inserts: SymbolInstance) -> PageResult:
        return classify_sheet_furniture(FurnitureTests._page(list(inserts)))

    def test_geology_pair_is_letter_and_digit(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U52",
            attributes={**self.MARKERS, "Ось": "7", "Ось'": "Ж"},
        )
        result = attach_axes(self._classified(axis))
        parsed = result.symbol_instances[0].axis
        self.assertEqual("Ж", parsed["letter"])
        self.assertEqual("7", parsed["digit"])
        self.assertEqual("Ж / 7", parsed["label"])
        self.assertEqual("attributes", parsed["source"])

    def test_tag_order_does_not_decide_letter_vs_digit(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U143",
            attributes={**self.MARKERS, "Ось": "А", "Ось'": "2"},
        )
        result = attach_axes(self._classified(axis))
        parsed = result.symbol_instances[0].axis
        self.assertEqual("А", parsed["letter"])
        self.assertEqual("2", parsed["digit"])

    def test_extra_markers_are_not_the_axis_name(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U52",
            attributes={**self.MARKERS, "Ось": "6", "Ось'": "Ж"},
        )
        parsed = attach_axes(self._classified(axis)).symbol_instances[0].axis
        self.assertEqual("6", parsed["digit"])
        self.assertNotEqual("12", parsed["digit"])
        self.assertNotEqual("И", parsed["letter"])

    def test_empty_letter_slot_is_not_absence(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U52",
            attributes={"Ось": "7", "Ось'": ""},
        )
        parsed = attach_axes(self._classified(axis)).symbol_instances[0].axis
        self.assertEqual("7", parsed["digit"])
        self.assertEqual("", parsed["letter"])
        self.assertIn("буква не прочитана", parsed["note"])

    def test_missing_digit_tag_is_a_note_not_a_guess(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U143",
            attributes={"Ось": "А"},
        )
        parsed = attach_axes(self._classified(axis)).symbol_instances[0].axis
        self.assertEqual("А", parsed["letter"])
        self.assertEqual("", parsed["digit"])
        self.assertIn("цифра не прочитана", parsed["note"])

    def test_nearby_text_fills_missing_letter(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U52",
            attributes={"Ось": "7", "Ось'": ""},
            x=100.0,
            y=200.0,
        )
        texts = [TextItem(100.0, 200.5, 2.5, "Ж", "text")]
        parsed = attach_axes(self._classified(axis), texts).symbol_instances[0].axis
        self.assertEqual("Ж", parsed["letter"])
        self.assertEqual("7", parsed["digit"])
        self.assertEqual("merged", parsed["source"])

    def test_two_nearby_letters_are_ambiguous(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U1",
            x=100.0,
            y=200.0,
        )
        texts = [
            TextItem(100.0, 200.0, 2.5, "Ж", "text"),
            TextItem(100.0, 200.0, 2.5, "А", "text"),
        ]
        parsed = attach_axes(self._classified(axis), texts).symbol_instances[0].axis
        self.assertEqual("", parsed["letter"])
        self.assertIn("несколько подписей рядом", parsed["note"])

    def test_room_number_is_not_an_axis(self) -> None:
        room = FurnitureTests._insert(
            "SI-room",
            layer="0",
            block_name="номерация 5",
        )
        result = attach_axes(self._classified(room))
        self.assertEqual("specification_mark", result.symbol_instances[0].role)
        self.assertIsNone(result.symbol_instances[0].axis)

    def test_column_stamp_and_level_stay_untouched(self) -> None:
        stamp = FurnitureTests._insert(
            "SI-stamp",
            layer="FORMAT",
            block_name="*U704",
            attributes={"ЛИСТ": "1", "СТАДИЯ": "П", "ГИП": "Иванов", "ФОРМАТ": "А1"},
        )
        column = FurnitureTests._insert(
            "SI-column",
            layer="колонна",
            block_name="Колонна",
        )
        level = FurnitureTests._insert(
            "SI-level",
            layer="OTMETKI",
            block_name="*U302",
            attributes={"ОТМЕТКА": "0.000"},
        )
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U52",
            attributes={"Ось": "7", "Ось'": "Ж"},
        )
        result = attach_axes(self._classified(stamp, column, level, axis))
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertIsNone(by_id["SI-stamp"].axis)
        self.assertIsNone(by_id["SI-column"].axis)
        self.assertIsNone(by_id["SI-level"].axis)
        self.assertEqual("Ж / 7", by_id["SI-axis"].axis["label"])

    def test_html_axis_card_shows_letter_and_digit(self) -> None:
        from dwg_symbols.html_report import _classified_card

        html = _classified_card(
            {
                "id": "SI-axis",
                "role": "specification_mark",
                "classificationReason": AXIS_LAYER_REASON,
                "layer": "OSI",
                "blockName": "*U52",
                "sourceKind": "dwg_insert_candidate",
                "position": {"x": 1.0, "y": 2.0, "space": "paper", "units": "mm"},
                "attributes": {**self.MARKERS, "Ось": "7", "Ось'": "Ж"},
            },
            None,
            Path("."),
        )
        self.assertIn("Ж / 7", html)
        self.assertIn("Ось", html)
        self.assertLess(html.index("Ж / 7"), html.index("Атрибуты DWG-блока"))

    def test_confirmed_legend_is_not_an_axis(self) -> None:
        hit = FurnitureTests._insert(
            "SI-legend",
            layer="I-WALL",
            block_name="грунт",
            status="confirmed",
            role="field_candidate",
            legend_entry_id="LE-soil",
        )
        self.assertIsNone(read_axis(hit))


class SheetZoneTests(unittest.TestCase):
    """Optional legend/notes windows on a sheet that already has a stamp."""

    ZONES = {
        "title_block": [656.0, 0.0, 841.0, 55.0],
        "drawing_field": [0.0, 55.0, 841.0, 594.0],
    }

    @staticmethod
    def _legend(
        entry_id: str = "LE-soil",
        bbox: tuple[float, float, float, float] = (47.0, 50.0, 160.0, 82.0),
    ) -> LegendEntry:
        return LegendEntry(
            id=entry_id,
            page=1,
            label="Суглинок",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature=f"legend-vector-v1:{entry_id}",
            bbox=bbox,
            symbol_bbox=bbox,
            confidence=0.9,
        )

    def _page(
        self,
        *,
        legends: list[LegendEntry] | None = None,
        inserts: list[SymbolInstance] | None = None,
        zones: dict | None = None,
        bindings: list[SymbolBinding] | None = None,
    ) -> PageResult:
        result = FurnitureTests._page(
            list(inserts or []),
            legends=legends,
            bindings=bindings,
        )
        if zones is not None:
            result.sheet_zones = dict(zones)
        elif zones is None and inserts is None:
            result.sheet_zones = dict(self.ZONES)
        return result

    def test_heading_and_rows_create_legend_zone(self) -> None:
        heading = TextItem(80.0, 90.0, 4.0, "Условные обозначения", "mtext")
        entry = self._legend()
        result = attach_legend_notes_zones(
            self._page(legends=[entry]),
            [heading],
        )
        self.assertIn("legend", result.sheet_zones)
        box = result.sheet_zones["legend"]
        self.assertEqual(4, len(box))
        self.assertLessEqual(box[0], entry.bbox[0])
        self.assertLessEqual(box[1], entry.bbox[1])
        self.assertGreaterEqual(box[2], entry.bbox[2])
        self.assertGreaterEqual(box[3], heading.y)
        self.assertNotIn("notes", result.sheet_zones)

    def test_missing_legend_heading_omits_legend_key(self) -> None:
        result = attach_legend_notes_zones(
            self._page(legends=[self._legend()]),
            [TextItem(80.0, 90.0, 4.0, "План первого этажа", "mtext")],
        )
        self.assertNotIn("legend", result.sheet_zones)
        self.assertNotIn("notes", result.sheet_zones)

    def test_heading_without_rows_omits_legend_key(self) -> None:
        result = attach_legend_notes_zones(
            self._page(legends=[]),
            [TextItem(80.0, 90.0, 4.0, "Условные обозначения", "mtext")],
        )
        self.assertNotIn("legend", result.sheet_zones)

    def test_notes_heading_creates_notes_zone(self) -> None:
        heading = TextItem(60.0, 70.0, 3.5, "Примечания", "mtext")
        result = attach_legend_notes_zones(self._page(), [heading])
        self.assertIn("notes", result.sheet_zones)
        box = result.sheet_zones["notes"]
        self.assertLessEqual(box[0], heading.x)
        self.assertGreaterEqual(box[2], heading.x)
        self.assertLessEqual(box[1], heading.y)
        self.assertGreaterEqual(box[3], heading.y)
        self.assertNotIn("legend", result.sheet_zones)
        self.assertEqual([], result.notes_texts)

    def test_missing_notes_anchor_omits_notes_key(self) -> None:
        result = attach_legend_notes_zones(
            self._page(),
            [TextItem(60.0, 70.0, 3.5, "Грунт (см. примечание 1)", "mtext")],
        )
        self.assertNotIn("notes", result.sheet_zones)
        self.assertEqual([], result.notes_texts)

    def test_notes_heading_mtext_with_inline_body_fills_sidecar(self) -> None:
        heading = TextItem(
            670.0,
            72.0,
            3.5,
            "Примечание: 1. Инженерно-геологические изыскания выполнены "
            "ООО «МОСГЕОТЕХ» в период с 10.12.2025 г. по 19.01.2026 г. "
            "2. Разрезы XIV–XIX см. лист 18.",
            "mtext",
            width=150.0,
        )
        result = attach_legend_notes_zones(self._page(), [heading])
        self.assertIn("notes", result.sheet_zones)
        texts = [item.text for item in result.notes_texts]
        self.assertTrue(any("МОСГЕОТЕХ" in item for item in texts))
        self.assertTrue(any("лист 18" in item for item in texts))
        stamp = self.ZONES["title_block"]
        self.assertGreaterEqual(result.sheet_zones["notes"][1], stamp[3])

    def test_notes_body_below_heading_fills_sidecar_and_zone(self) -> None:
        heading = TextItem(
            670.0, 72.0, 3.5, "Примечания", "mtext", width=150.0
        )
        body = TextItem(
            672.0,
            62.0,
            2.5,
            "1. Изыскания выполнены ООО «МОСГЕОТЕХ».",
            "text",
        )
        second = TextItem(
            672.0,
            58.0,
            2.5,
            "2. Разрезы XIV–XIX см. лист 18.",
            "text",
        )
        above = TextItem(
            672.0, 180.0, 3.0, "Разрез по линии XII-XII", "mtext"
        )
        before = list(self.ZONES["drawing_field"])
        result = attach_legend_notes_zones(
            self._page(),
            [heading, body, second, above],
        )
        self.assertIn("notes", result.sheet_zones)
        box = result.sheet_zones["notes"]
        stamp = self.ZONES["title_block"]
        self.assertGreaterEqual(box[3], heading.y)
        self.assertLessEqual(box[1], body.y)
        self.assertGreaterEqual(box[1], stamp[3])
        self.assertLess(box[3], above.y)
        self.assertEqual(
            [
                "1. Изыскания выполнены ООО «МОСГЕОТЕХ».",
                "2. Разрезы XIV–XIX см. лист 18.",
            ],
            [item.text for item in result.notes_texts],
        )
        self.assertNotIn(
            "Разрез по линии XII-XII",
            [item.text for item in result.notes_texts],
        )
        self.assertEqual(before, result.sheet_zones["drawing_field"])
        self.assertEqual(self.ZONES["title_block"], result.sheet_zones["title_block"])
        with tempfile.TemporaryDirectory() as directory:
            page_dir = write_page_result(directory, result)
            payload = json.loads(
                (page_dir / "notes_texts.json").read_text(encoding="utf-8")
            )
            summary = json.loads((page_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(
            [
                "1. Изыскания выполнены ООО «МОСГЕОТЕХ».",
                "2. Разрезы XIV–XIX см. лист 18.",
            ],
            [item["text"] for item in payload["items"]],
        )
        self.assertEqual(2, summary["counts"]["notesTexts"])
        self.assertEqual(before, summary["sheetZones"]["drawing_field"])

    def test_notes_zone_does_not_recut_drawing_field(self) -> None:
        heading = TextItem(
            670.0, 72.0, 3.5, "Примечания", "mtext", width=150.0
        )
        body = TextItem(672.0, 62.0, 2.5, "1. Изыскания выполнены.", "text")
        before = list(self.ZONES["drawing_field"])
        result = attach_legend_notes_zones(self._page(), [heading, body])
        self.assertEqual(before, result.sheet_zones["drawing_field"])
        field = result.sheet_zones["drawing_field"]
        stamp_box = result.sheet_zones["title_block"]
        self.assertEqual(stamp_box[3], field[1])
        self.assertEqual([0.0, stamp_box[3], 841.0, 594.0], field)

    def test_notes_body_is_not_a_text_label(self) -> None:
        heading = TextItem(
            670.0, 72.0, 3.5, "Примечания", "mtext", width=150.0
        )
        body = TextItem(672.0, 62.0, 2.5, "7", "text", layer="OSI")
        result = attach_legend_notes_zones(self._page(), [heading, body])
        result = attach_text_labels(result, [heading, body])
        self.assertEqual(["7"], [item.text for item in result.notes_texts])
        self.assertEqual([], result.text_labels)

    def test_stamp_and_drawing_field_stay_as_in_title_block(self) -> None:
        stamp = TitleBlockTests._stamp_insert(
            attributes={"ЛИСТ": "1", "СТАДИЯ": "П", "ЛИСТОВ": "8", "ФОРМАТ": "А1"}
        )
        heading = TextItem(80.0, 90.0, 4.0, "Условные обозначения", "mtext")
        result = FurnitureTests._page(
            [stamp],
            legends=[self._legend()],
        )
        result = classify_sheet_furniture(result, sheet_bbox=TitleBlockTests.SHEET_BBOX)
        result = attach_title_block(
            result,
            texts=[heading],
            sheet_bbox=TitleBlockTests.SHEET_BBOX,
        )
        self.assertIn("title_block", result.sheet_zones)
        self.assertIn("drawing_field", result.sheet_zones)
        self.assertEqual(4, len(result.sheet_zones["title_block"]))
        self.assertEqual(4, len(result.sheet_zones["drawing_field"]))
        self.assertIn("legend", result.sheet_zones)
        field = list(result.sheet_zones["drawing_field"])
        stamp_box = result.sheet_zones["title_block"]
        self.assertEqual(stamp_box[3], field[1])
        self.assertEqual([0.0, stamp_box[3], 841.0, 594.0], field)

    def test_legend_zone_does_not_recut_drawing_field(self) -> None:
        heading = TextItem(80.0, 90.0, 4.0, "Условные обозначения", "mtext")
        before = list(self.ZONES["drawing_field"])
        result = attach_legend_notes_zones(
            self._page(legends=[self._legend()]),
            [heading],
        )
        self.assertEqual(before, result.sheet_zones["drawing_field"])
        self.assertEqual(self.ZONES["title_block"], result.sheet_zones["title_block"])

    def test_no_title_block_does_not_invent_sheet_zones(self) -> None:
        result = self._page(legends=[self._legend()], zones={})
        result.sheet_zones = None
        result = attach_legend_notes_zones(
            result,
            [TextItem(80.0, 90.0, 4.0, "Условные обозначения", "mtext")],
        )
        self.assertIsNone(result.sheet_zones)
        self.assertEqual([], result.notes_texts)

    def test_confirmed_soil_and_text_axis_keep_roles(self) -> None:
        soil = FurnitureTests._insert(
            "SI-soil",
            layer="I-WALL",
            block_name="грунт",
            status="confirmed",
            role="field_candidate",
            legend_entry_id="LE-soil",
        )
        binding = SymbolBinding(
            id="SB-soil",
            page=1,
            instance_id=soil.id,
            legend_entry_id="LE-soil",
            status="confirmed",
            confidence=1.0,
            evidence=(
                Evidence(
                    kind="exact_block_definition",
                    score=1.0,
                    source_ids=("LE-soil",),
                    detail="same block",
                ),
            ),
        )
        texts = [
            TextItem(80.0, 90.0, 4.0, "Условные обозначения", "mtext"),
            TextItem(100.0, 200.0, 3.0, "Ж", "text", layer="OSI"),
            TextItem(104.0, 200.0, 3.0, "7", "text", layer="OSI"),
        ]
        result = self._page(
            legends=[self._legend()],
            inserts=[soil],
            bindings=[binding],
            zones=dict(self.ZONES),
        )
        result = attach_legend_notes_zones(result, texts)
        result = attach_text_labels(result, texts)
        self.assertEqual("confirmed", result.symbol_instances[0].status)
        self.assertEqual("field_candidate", result.symbol_instances[0].role)
        self.assertEqual("LE-soil", result.symbol_instances[0].legend_entry_id)
        self.assertEqual("axis", result.text_labels[0].kind)
        self.assertEqual("Ж / 7", result.text_labels[0].text)
        result.validate()

    def test_old_sidecar_without_legend_notes_still_summarizes(self) -> None:
        furniture = SymbolInstance(
            id="SI-stamp",
            page=1,
            source_kind="dwg_insert_candidate",
            status="ignored",
            position=Point(830.0, 10.0, "paper", "mm"),
            source_handle="STAMP",
            source_space="modelspace:viewport:0",
            layer="FORMAT",
            signature="blockdef-v1:stamp",
            role="sheet_furniture",
            classification_reason="FORMAT_STAMP_ATTRIBUTES",
            attributes={"ЛИСТ": "1", "СТАДИЯ": "П", "ЛИСТОВ": "8"},
        )
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            symbol_instances=[furniture],
            title_block={
                "code": "",
                "sheet": "1",
                "sheetsTotal": "8",
                "stage": "П",
                "source": "attributes",
                "note": "шифр не прочитан",
                "instanceId": furniture.id,
            },
            sheet_zones={
                "title_block": [645.0, 10.0, 830.0, 65.0],
                "drawing_field": [0.0, 65.0, 841.0, 594.0],
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            page_dir = write_page_result(directory, result)
            summary = json.loads((page_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertNotIn("legend", summary["sheetZones"])
        self.assertNotIn("notes", summary["sheetZones"])
        self.assertEqual(
            [645.0, 10.0, 830.0, 65.0],
            summary["sheetZones"]["title_block"],
        )


class SheetSceneTests(unittest.TestCase):
    """View titles → sheetScenes; fail-closed without an anchor."""

    ZONES = {
        "title_block": [656.0, 0.0, 841.0, 55.0],
        "drawing_field": [0.0, 55.0, 841.0, 594.0],
        "legend": [650.0, 180.0, 830.0, 520.0],
        "notes": [650.0, 70.0, 830.0, 120.0],
    }

    def _page(self, inserts: list[SymbolInstance] | None = None) -> PageResult:
        result = FurnitureTests._page(list(inserts or []))
        result.sheet_zones = dict(self.ZONES)
        return result

    def test_is_view_title_is_narrow(self) -> None:
        self.assertTrue(is_view_title("Разрез по линии XII-XII"))
        self.assertTrue(is_view_title("Схема расположения скважин на плане"))
        self.assertTrue(is_view_title("Геологический разрез"))
        self.assertTrue(is_view_title("по линии XIV-XIV (по отчету ИГИ)"))
        self.assertTrue(is_view_title("XII-XII (по отчету ИГИ)"))
        self.assertTrue(is_view_title("XIII-XIII (по отчету ИГИ)"))
        self.assertTrue(is_view_title("XIV-XIV (по отчету ИГИ))f3ff"))
        self.assertFalse(is_view_title("Грунт (см. примечание 1)"))
        self.assertFalse(is_view_title("ИГЭ-1 суглинок тугопластичный"))
        self.assertFalse(is_view_title("План 1 этажа"))
        self.assertFalse(is_view_title("2. Разрезы XIV–XIX см. лист 18."))
        self.assertFalse(is_view_title("XII"))

    def test_section_title_creates_scene(self) -> None:
        heading = TextItem(200.0, 300.0, 3.5, "Разрез по линии XII-XII", "mtext")
        result = attach_sheet_scenes(self._page(), [heading])
        self.assertEqual(1, len(result.sheet_scenes))
        scene = result.sheet_scenes[0]
        self.assertIn("Разрез", scene.title)
        self.assertIn("XII-XII", scene.title)
        self.assertLessEqual(scene.bbox[0], heading.x)
        self.assertGreaterEqual(scene.bbox[2], heading.x)
        self.assertLessEqual(scene.bbox[1], heading.y)
        self.assertGreaterEqual(scene.bbox[3], heading.y)
        self.assertEqual((), scene.soil_ids)
        self.assertEqual((), scene.axis_ids)

    def test_roman_cut_line_creates_scene(self) -> None:
        heading = TextItem(
            200.0, 300.0, 3.5, "XII-XII (по отчету ИГИ))f3ff", "mtext"
        )
        result = attach_sheet_scenes(self._page(), [heading])
        self.assertEqual(1, len(result.sheet_scenes))
        title = result.sheet_scenes[0].title
        self.assertIn("XII-XII", title)
        self.assertIn("(по отчету ИГИ)", title)
        self.assertNotIn("f3ff", title)
        self.assertNotIn("ИГЭ", title)

    def test_soil_note_is_not_a_scene(self) -> None:
        result = attach_sheet_scenes(
            self._page(),
            [TextItem(200.0, 300.0, 3.0, "Грунт (см. примечание 1)", "mtext")],
        )
        self.assertEqual([], result.sheet_scenes)

    def test_one_title_is_one_scene_not_four_cuts(self) -> None:
        result = attach_sheet_scenes(
            self._page(),
            [TextItem(200.0, 300.0, 3.5, "Разрез по линии XII-XII", "mtext")],
        )
        self.assertEqual(1, len(result.sheet_scenes))
        self.assertEqual([], [item.title for item in result.sheet_scenes if "XIII" in item.title])
        self.assertEqual([], [item.title for item in result.sheet_scenes if "XIV" in item.title])

    def test_tx1_ige_texts_are_not_scenes(self) -> None:
        texts = [
            TextItem(120.0, 500.0, 5.0, "План 1 этажа", "mtext"),
            TextItem(80.0, 200.0, 4.0, "Условные обозначения", "mtext"),
            TextItem(80.0, 180.0, 2.5, "Грунт (см. примечание 1)", "mtext"),
            TextItem(80.0, 160.0, 2.5, "ИГЭ-1 суглинок тугопластичный", "mtext"),
            TextItem(200.0, 300.0, 3.0, "А", "text", layer="OSI"),
            TextItem(400.0, 300.0, 3.0, "2", "text", layer="OSI"),
        ]
        result = attach_sheet_scenes(self._page(), texts)
        self.assertEqual([], result.sheet_scenes)

    def test_stamp_and_notes_titles_are_not_scenes(self) -> None:
        texts = [
            TextItem(
                700.0,
                20.0,
                4.0,
                "Геологические разрезы по линиям XII-XII ... XIV-XIV",
                "text",
            ),
            TextItem(700.0, 30.0, 3.5, "Схема расположения фундаментов", "text"),
            TextItem(670.0, 90.0, 2.5, "2. Разрезы XIV–XIX см. лист 18.", "text"),
            TextItem(670.0, 300.0, 2.5, "Грунт (см. примечание 1)", "mtext"),
        ]
        result = attach_sheet_scenes(self._page(), texts)
        self.assertEqual([], result.sheet_scenes)

    def test_no_zones_yield_no_scenes(self) -> None:
        result = self._page()
        result.sheet_zones = None
        result = attach_sheet_scenes(
            result,
            [TextItem(200.0, 300.0, 3.5, "Разрез по линии XII-XII", "mtext")],
        )
        self.assertEqual([], result.sheet_scenes)

    def test_drawing_field_is_not_recut(self) -> None:
        before = list(self.ZONES["drawing_field"])
        result = attach_sheet_scenes(
            self._page(),
            [TextItem(200.0, 300.0, 3.5, "Разрез по линии XII-XII", "mtext")],
        )
        self.assertEqual(before, result.sheet_zones["drawing_field"])
        self.assertEqual(self.ZONES["title_block"], result.sheet_zones["title_block"])

    def test_multiline_title_is_one_scene(self) -> None:
        texts = [
            TextItem(400.0, 200.0, 3.5, "Разрез по оси Д в осях 1-15 для блока 1.", "mtext"),
            TextItem(400.0, 194.0, 3.0, "Геологический разрез", "mtext"),
            TextItem(400.0, 188.0, 3.0, "по линии XII-XII (по отчету ИГИ)", "mtext"),
        ]
        result = attach_sheet_scenes(self._page(), texts)
        self.assertEqual(1, len(result.sheet_scenes))
        title = result.sheet_scenes[0].title
        self.assertIn("Разрез по оси Д", title)
        self.assertIn("XII-XII", title)

    def test_scheme_and_four_sections_are_five_scenes(self) -> None:
        texts = [
            TextItem(80.0, 400.0, 4.0, "Схема расположения скважин на плане", "mtext"),
            TextItem(400.0, 500.0, 3.5, "Разрез по оси Д в осях 1-15 для блока 1.", "mtext"),
            TextItem(400.0, 493.0, 3.0, "Геологический разрез", "mtext"),
            TextItem(400.0, 486.0, 3.0, "по линии XII-XII (по отчету ИГИ)", "mtext"),
            TextItem(400.0, 380.0, 3.5, "Разрез по оси А в осях 1-15 для блока 1.", "mtext"),
            TextItem(400.0, 373.0, 3.0, "по линии XIII-XIII (по отчету ИГИ)", "mtext"),
            TextItem(400.0, 260.0, 3.5, "Разрез по оси Д в осях 1-15 для блока 2.", "mtext"),
            TextItem(400.0, 253.0, 3.0, "по линии XIII-XIII (по отчету ИГИ)", "mtext"),
            TextItem(400.0, 140.0, 3.5, "Разрез по оси А в осях 1-15 для блока 2.", "mtext"),
            TextItem(400.0, 133.0, 3.0, "по линии XIV-XIV (по отчету ИГИ)", "mtext"),
        ]
        result = attach_sheet_scenes(self._page(), texts)
        titles = [item.title for item in result.sheet_scenes]
        self.assertEqual(5, len(titles))
        self.assertIn("Схема расположения", result.sheet_scenes[0].title)
        self.assertEqual(1, sum("XII-XII" in title for title in titles))
        self.assertEqual(2, sum("XIII-XIII" in title for title in titles))
        self.assertEqual(1, sum("XIV-XIV" in title for title in titles))
        xiii = [title for title in titles if "XIII-XIII" in title]
        self.assertTrue(any("блока 1" in title for title in xiii))
        self.assertTrue(any("блока 2" in title for title in xiii))
        xii = next(item for item in result.sheet_scenes if "XII-XII" in item.title)
        self.assertTrue(xii.bbox[0] <= 400.0 <= xii.bbox[2])
        self.assertTrue(xii.bbox[1] <= 520.0 <= xii.bbox[3])
        for scene in result.sheet_scenes:
            if "Разрез" not in scene.title:
                continue
            self.assertFalse(
                scene.bbox[0] <= 700.0 <= scene.bbox[2]
                and scene.bbox[1] <= 300.0 <= scene.bbox[3]
            )

    def test_four_roman_lines_are_four_scenes(self) -> None:
        texts = [
            TextItem(80.0, 400.0, 4.0, "Схема расположения скважин на плане", "mtext"),
            TextItem(
                200.0, 56.0, 3.0, "XII-XII (по отчету ИГИ)", "mtext", width=400.0
            ),
            TextItem(
                340.0, 56.0, 3.0, "XIII-XIII (по отчету ИГИ)", "mtext", width=400.0
            ),
            TextItem(
                480.0, 56.0, 3.0, "XIII-XIII (по отчету ИГИ)", "mtext", width=400.0
            ),
            TextItem(
                620.0, 56.0, 3.0, "XIV-XIV (по отчету ИГИ)", "mtext", width=400.0
            ),
        ]
        result = attach_sheet_scenes(self._page(), texts)
        titles = [item.title for item in result.sheet_scenes]
        self.assertEqual(5, len(titles))
        self.assertEqual(1, sum("XII-XII" in title for title in titles))
        self.assertEqual(2, sum("XIII-XIII" in title for title in titles))
        self.assertEqual(1, sum("XIV-XIV" in title for title in titles))
        self.assertTrue(all("f3ff" not in title for title in titles))

    def test_view_title_is_not_a_text_label(self) -> None:
        texts = [TextItem(200.0, 300.0, 3.5, "Разрез по линии XII-XII", "mtext")]
        result = attach_sheet_scenes(self._page(), texts)
        result = attach_text_labels(result, texts)
        self.assertEqual(1, len(result.sheet_scenes))
        self.assertEqual([], result.text_labels)

    def test_confirmed_soil_keeps_role(self) -> None:
        legend = LegendEntry(
            id="LE-soil",
            page=1,
            label="Суглинок",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature="legend-vector-v1:soil",
            confidence=0.9,
        )
        soil = FurnitureTests._insert(
            "SI-soil",
            layer="Fill",
            block_name="круг7",
            status="confirmed",
            role="field_candidate",
            legend_entry_id="LE-soil",
        )
        binding = SymbolBinding(
            id="SB-soil",
            page=1,
            instance_id=soil.id,
            legend_entry_id="LE-soil",
            status="confirmed",
            confidence=1.0,
            evidence=(
                Evidence(
                    kind="exact_block_definition",
                    score=1.0,
                    source_ids=("LE-soil",),
                    detail="same block",
                ),
            ),
        )
        result = FurnitureTests._page([soil], legends=[legend], bindings=[binding])
        result.sheet_zones = dict(self.ZONES)
        result = attach_sheet_scenes(
            result,
            [TextItem(200.0, 300.0, 3.5, "Разрез по линии XII-XII", "mtext")],
        )
        self.assertEqual("confirmed", result.symbol_instances[0].status)
        self.assertEqual("field_candidate", result.symbol_instances[0].role)
        self.assertEqual(1, len(result.sheet_scenes))

    def _binding(self, binding_id: str, instance_id: str, legend_id: str) -> SymbolBinding:
        return SymbolBinding(
            id=binding_id,
            page=1,
            instance_id=instance_id,
            legend_entry_id=legend_id,
            status="confirmed",
            confidence=1.0,
            evidence=(
                Evidence(
                    kind="exact_block_definition",
                    score=1.0,
                    source_ids=(legend_id,),
                    detail="same block",
                ),
            ),
        )

    def test_field_soils_and_axes_cluster_into_scene(self) -> None:
        legend = LegendEntry(
            id="LE-soil",
            page=1,
            label="Суглинок",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature="legend-vector-v1:soil",
            confidence=0.9,
        )
        upper = FurnitureTests._insert(
            "SI-soil7",
            layer="Fill",
            block_name="круг7",
            status="confirmed",
            role="field_candidate",
            legend_entry_id="LE-soil",
            x=250.0,
            y=430.0,
        )
        lower = FurnitureTests._insert(
            "SI-soil2",
            layer="Fill",
            block_name="круг2",
            status="confirmed",
            role="field_candidate",
            legend_entry_id="LE-soil",
            signature="blockdef-v1:krug2",
            x=260.0,
            y=360.0,
        )
        exemplar = FurnitureTests._insert(
            "SI-ex",
            layer="Fill",
            block_name="круг7",
            status="reference",
            role="legend_exemplar",
            legend_entry_id="LE-soil",
            signature="blockdef-v1:krug7-ex",
            x=700.0,
            y=300.0,
        )
        legend_soil = FurnitureTests._insert(
            "SI-legend-soil",
            layer="Fill",
            block_name="круг1",
            status="confirmed",
            role="field_candidate",
            legend_entry_id="LE-soil",
            signature="blockdef-v1:krug1",
            x=700.0,
            y=280.0,
        )
        well = FurnitureTests._insert(
            "SI-cpe",
            x=250.0,
            y=400.0,
            classification_reason=GEOLOGY_NO_JOIN_REASON,
        )
        axis = replace(
            FurnitureTests._insert(
                "SI-axis",
                layer="OSI",
                block_name="*U52",
                status="ignored",
                role="specification_mark",
                classification_reason=AXIS_LAYER_REASON,
                x=220.0,
                y=450.0,
                attributes={"Ось": "1", "Ось'": "Ж"},
            ),
            axis={
                "letter": "Ж",
                "digit": "1",
                "label": "Ж / 1",
                "source": "attributes",
                "note": "",
            },
        )
        axis15 = replace(
            FurnitureTests._insert(
                "SI-axis15",
                layer="OSI",
                block_name="*U53",
                status="ignored",
                role="specification_mark",
                classification_reason=AXIS_LAYER_REASON,
                signature="blockdef-v1:u53",
                x=400.0,
                y=450.0,
                attributes={"Ось": "15", "Ось'": "Ж"},
            ),
            axis={
                "letter": "Ж",
                "digit": "15",
                "label": "Ж / 15",
                "source": "attributes",
                "note": "",
            },
        )
        room = FurnitureTests._insert(
            "SI-room",
            layer="0",
            block_name="номерация 5",
            status="ignored",
            role="specification_mark",
            classification_reason=ROOM_NUMBER_BLOCK_REASON,
            x=300.0,
            y=400.0,
        )
        result = FurnitureTests._page(
            [upper, lower, exemplar, legend_soil, well, axis, axis15, room],
            legends=[legend],
            bindings=[
                self._binding("SB-7", upper.id, legend.id),
                self._binding("SB-2", lower.id, legend.id),
                self._binding("SB-legend", legend_soil.id, legend.id),
            ],
        )
        result.sheet_zones = dict(self.ZONES)
        result.anomaly_codes = ["BLOCK_BASELINE_ONLY", "RELATIONSHIP_STAGE_NOT_RUN"]
        result = attach_sheet_scenes(
            result,
            [TextItem(200.0, 300.0, 3.5, "Разрез по линии XII-XII", "mtext")],
        )
        scene = result.sheet_scenes[0]
        self.assertEqual((upper.id, lower.id), scene.soil_ids)
        self.assertEqual((axis.id, axis15.id), scene.axis_ids)
        self.assertNotIn(exemplar.id, scene.soil_ids)
        self.assertNotIn(legend_soil.id, scene.soil_ids)
        self.assertNotIn(well.id, scene.soil_ids)
        self.assertNotIn(room.id, scene.axis_ids)
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertEqual("confirmed", by_id[upper.id].status)
        self.assertEqual("field_candidate", by_id[upper.id].role)
        self.assertEqual("legend_exemplar", by_id[exemplar.id].role)
        self.assertEqual("specification_mark", by_id[axis.id].role)
        self.assertEqual(GEOLOGY_NO_JOIN_REASON, by_id[well.id].classification_reason)
        self.assertNotIn("RELATIONSHIP_STAGE_NOT_RUN", result.anomaly_codes)
        self.assertIn("BLOCK_BASELINE_ONLY", result.anomaly_codes)

    def test_unresolved_circle_fills_soil_ids_without_confirm(self) -> None:
        pending = FurnitureTests._insert(
            "SI-soil7",
            layer="Fill",
            block_name="круг7",
            status="unresolved",
            role="field_candidate",
            x=250.0,
            y=430.0,
        )
        result = FurnitureTests._page([pending])
        result.sheet_zones = dict(self.ZONES)
        result = attach_sheet_scenes(
            result,
            [TextItem(200.0, 300.0, 3.5, "XII-XII (по отчету ИГИ)", "mtext")],
        )
        self.assertEqual((pending.id,), result.sheet_scenes[0].soil_ids)
        self.assertEqual("unresolved", result.symbol_instances[0].status)
        self.assertEqual("field_candidate", result.symbol_instances[0].role)

    def test_caption_above_splits_stacked_cuts(self) -> None:
        upper = FurnitureTests._insert(
            "SI-soil7",
            layer="Fill",
            block_name="круг7",
            status="unresolved",
            role="field_candidate",
            x=200.0,
            y=450.0,
        )
        lower = FurnitureTests._insert(
            "SI-soil2",
            layer="Fill",
            block_name="круг2",
            status="unresolved",
            role="field_candidate",
            signature="blockdef-v1:krug2",
            x=200.0,
            y=180.0,
        )
        key = FurnitureTests._insert(
            "SI-key1",
            layer="Fill",
            block_name="круг1",
            status="unresolved",
            role="field_candidate",
            signature="blockdef-v1:krug1",
            x=500.0,
            y=350.0,
        )
        result = FurnitureTests._page([upper, lower, key])
        result.sheet_zones = dict(self.ZONES)
        result = attach_sheet_scenes(
            result,
            [
                TextItem(
                    200.0,
                    538.0,
                    3.5,
                    "Схема расположения фундаментов на геологическом разрезе "
                    "по линии VIII-VIII (по отчету ИГИ)",
                    "mtext",
                ),
                TextItem(
                    200.0,
                    300.0,
                    3.5,
                    "Схема расположения фундаментов на геологическом разрезе "
                    "по линии X-X (по отчету ИГИ)",
                    "mtext",
                ),
                TextItem(
                    500.0,
                    400.0,
                    3.5,
                    "Схема расположения геологических выработок(по отчету ИГИ)",
                    "mtext",
                ),
            ],
        )
        self.assertEqual(3, len(result.sheet_scenes))
        eight = next(item for item in result.sheet_scenes if "VIII-VIII" in item.title)
        ten = next(item for item in result.sheet_scenes if "X-X" in item.title)
        scheme = next(
            item for item in result.sheet_scenes if "выработок" in item.title
        )
        self.assertEqual((upper.id,), eight.soil_ids)
        self.assertEqual((lower.id,), ten.soil_ids)
        self.assertEqual((), scheme.soil_ids)
        self.assertTrue(eight.bbox[1] <= 450.0 <= eight.bbox[3])
        self.assertTrue(ten.bbox[1] <= 180.0 <= ten.bbox[3])
        self.assertFalse(ten.bbox[1] <= 450.0 <= ten.bbox[3])
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertEqual("unresolved", by_id[upper.id].status)
        self.assertEqual("field_candidate", by_id[upper.id].role)

    def test_relationship_placeholder_stays_without_scenes(self) -> None:
        result = self._page()
        result.anomaly_codes = ["RELATIONSHIP_STAGE_NOT_RUN"]
        result = attach_sheet_scenes(
            result,
            [TextItem(200.0, 300.0, 3.0, "Грунт (см. примечание 1)", "mtext")],
        )
        self.assertEqual([], result.sheet_scenes)
        self.assertIn("RELATIONSHIP_STAGE_NOT_RUN", result.anomaly_codes)

    def test_sidecar_and_ideal_md_use_scene_titles(self) -> None:
        result = attach_sheet_scenes(
            self._page(),
            [TextItem(200.0, 300.0, 3.5, "Разрез по линии XII-XII", "mtext")],
        )
        with tempfile.TemporaryDirectory() as directory:
            page_dir = write_page_result(directory, result)
            payload = json.loads(
                (page_dir / "sheet_scenes.json").read_text(encoding="utf-8")
            )
            summary = json.loads((page_dir / "summary.json").read_text(encoding="utf-8"))
            markdown = render_ideal_markdown(page_dir)
        self.assertEqual(1, summary["counts"]["sheetScenes"])
        self.assertEqual("Разрез по линии XII-XII", payload["items"][0]["title"])
        self.assertEqual([], payload["items"][0]["soilIds"])
        self.assertEqual([], payload["items"][0]["axisIds"])
        self.assertIn("### Описание: Разрез по линии XII-XII", markdown)
        self.assertNotIn(GAP_VIEWS, markdown)
        self.assertEqual(1, markdown.count("### Описание:"))

    def test_attach_title_block_reads_field_title_not_stamp(self) -> None:
        stamp = TitleBlockTests._stamp_insert(
            attributes={"ЛИСТ": "17", "СТАДИЯ": "П", "ФОРМАТ": "А1"}
        )
        result = classify_sheet_furniture(
            FurnitureTests._page([stamp]),
            sheet_bbox=TitleBlockTests.SHEET_BBOX,
        )
        texts = TitleBlockTests._gost_texts(
            sheet="17",
            extra=[TextItem(200.0, 300.0, 3.5, "Разрез по линии XII-XII", "mtext")],
        )
        result = attach_title_block(
            result, texts, sheet_bbox=TitleBlockTests.SHEET_BBOX
        )
        self.assertEqual(1, len(result.sheet_scenes))
        self.assertIn("XII-XII", result.sheet_scenes[0].title)
        self.assertNotIn("Схема расположения фундаментов", result.sheet_scenes[0].title)
        field = result.sheet_zones["drawing_field"]
        stamp_box = result.sheet_zones["title_block"]
        self.assertEqual(stamp_box[3], field[1])


class FieldGeometryTests(unittest.TestCase):
    """Field LINE/POLYLINE windows; label only with a unique same-sheet sample."""

    ZONES = {
        "title_block": [656.0, 0.0, 841.0, 55.0],
        "drawing_field": [0.0, 55.0, 841.0, 594.0],
        "legend": [50.0, 80.0, 220.0, 210.0],
        "notes": [50.0, 40.0, 220.0, 72.0],
    }

    @staticmethod
    def _line(
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        *,
        layer: str = "WALL",
        lw: float = 0.25,
    ) -> dict:
        return {
            "type": "line",
            "layer": layer,
            "color": "#000000",
            "lw": lw,
            "points": [[x0, y0], [x1, y1]],
        }

    @staticmethod
    def _signature(primitive: dict) -> str:
        from dwg_symbols.legends import _vector_signature

        points = primitive["points"]
        xs = [float(point[0]) for point in points]
        ys = [float(point[1]) for point in points]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        signature = _vector_signature([primitive], bbox)
        assert signature
        return signature

    def _legend(
        self,
        entry_id: str,
        label: str,
        signature: str,
        *,
        bbox: tuple[float, float, float, float] = (60.0, 90.0, 200.0, 110.0),
        symbol_bbox: tuple[float, float, float, float] = (60.0, 90.0, 90.0, 110.0),
    ) -> LegendEntry:
        return LegendEntry(
            id=entry_id,
            page=1,
            label=label,
            status="extracted",
            source_kind="dwg_vector_legend",
            signature=signature,
            bbox=bbox,
            symbol_bbox=symbol_bbox,
            confidence=0.9,
        )

    @staticmethod
    def _square(x: float, y: float, size: float = 12.0, layer: str = "HATCH") -> dict:
        return {
            "type": "polyline",
            "layer": layer,
            "color": "#cc9900",
            "lw": 0.15,
            "points": [
                [x, y],
                [x + size, y],
                [x + size, y + size],
                [x, y + size],
                [x, y],
            ],
        }

    @staticmethod
    def _hatch(
        points: list,
        *,
        pattern: str = "ANSI31",
        scale: float = 1.0,
        color: str = "#cc9900",
        layer: str = "GRUNT",
    ) -> dict:
        return {
            "type": "hatch",
            "layer": layer,
            "color": color,
            "lw": 0.0,
            "pattern": pattern,
            "pattern_scale": scale,
            "points": points,
        }

    def _page(
        self,
        *,
        legends: list[LegendEntry] | None = None,
        inserts: list[SymbolInstance] | None = None,
        bindings: list[SymbolBinding] | None = None,
        zones: dict | None = None,
    ) -> PageResult:
        result = FurnitureTests._page(
            list(inserts or []),
            legends=legends,
            bindings=bindings,
        )
        result.sheet_zones = dict(self.ZONES if zones is None else zones)
        return result

    def test_field_line_is_visible_and_not_insert(self) -> None:
        primitive = self._line(300.0, 300.0, 330.0, 300.0)
        result = attach_field_geometry(self._page(), [primitive])
        self.assertEqual(1, len(result.field_geometry))
        item = result.field_geometry[0]
        self.assertEqual("line", item.kind)
        self.assertEqual("sheet_primitive", item.source)
        self.assertEqual("", item.label)
        self.assertEqual("", item.legend_entry_id)
        self.assertEqual([], result.symbol_instances)

    def test_unique_same_sheet_sample_sets_label(self) -> None:
        sample = self._line(60.0, 100.0, 90.0, 100.0)
        field = self._line(300.0, 300.0, 330.0, 300.0)
        legend = self._legend("LE-wall", "стена несущая", self._signature(sample))
        result = attach_field_geometry(
            self._page(legends=[legend]),
            [field],
        )
        self.assertEqual(1, len(result.field_geometry))
        self.assertEqual("стена несущая", result.field_geometry[0].label)
        self.assertEqual("LE-wall", result.field_geometry[0].legend_entry_id)

    def test_shared_legend_signature_stays_unlabeled(self) -> None:
        sample = self._line(60.0, 100.0, 90.0, 100.0)
        signature = self._signature(sample)
        legends = [
            self._legend("LE-a", "стена несущая", signature),
            self._legend("LE-b", "стена ненесущая", signature),
        ]
        field = self._line(300.0, 300.0, 330.0, 300.0)
        result = attach_field_geometry(self._page(legends=legends), [field])
        self.assertEqual(1, len(result.field_geometry))
        self.assertEqual("", result.field_geometry[0].label)
        self.assertEqual("", result.field_geometry[0].legend_entry_id)

    def test_foreign_signature_is_not_labeled(self) -> None:
        vertical = self._line(60.0, 100.0, 60.0, 130.0)
        field = self._line(300.0, 300.0, 330.0, 300.0)
        legend = self._legend("LE-other", "с другого листа", self._signature(vertical))
        result = attach_field_geometry(self._page(legends=[legend]), [field])
        self.assertEqual(1, len(result.field_geometry))
        self.assertEqual("", result.field_geometry[0].label)

    def test_geometry_in_legend_zone_is_not_a_field_object(self) -> None:
        inside = self._line(80.0, 120.0, 120.0, 120.0)
        result = attach_field_geometry(self._page(), [inside])
        self.assertEqual([], result.field_geometry)

    def test_closed_polyline_is_a_contour(self) -> None:
        square = {
            "type": "polyline",
            "layer": "HATCH",
            "color": "#cc9900",
            "lw": 0.15,
            "points": [
                [300.0, 200.0],
                [320.0, 200.0],
                [320.0, 220.0],
                [300.0, 220.0],
                [300.0, 200.0],
            ],
        }
        result = attach_field_geometry(self._page(), [square])
        self.assertEqual(1, len(result.field_geometry))
        self.assertEqual("closed", result.field_geometry[0].kind)

    def test_format_layer_and_short_noise_are_skipped(self) -> None:
        primitives = [
            self._line(300.0, 300.0, 330.0, 300.0, layer="FORMAT"),
            self._line(300.0, 400.0, 302.0, 400.0),
        ]
        result = attach_field_geometry(self._page(), primitives)
        self.assertEqual([], result.field_geometry)

    def test_confirmed_soil_insert_stays_untouched(self) -> None:
        soil = FurnitureTests._insert(
            "SI-soil",
            layer="I-WALL",
            block_name="грунт",
            status="confirmed",
            role="field_candidate",
            legend_entry_id="LE-soil",
        )
        binding = SymbolBinding(
            id="SB-soil",
            page=1,
            instance_id=soil.id,
            legend_entry_id="LE-soil",
            status="confirmed",
            confidence=1.0,
            evidence=(
                Evidence(
                    kind="exact_block_definition",
                    score=1.0,
                    source_ids=("LE-soil",),
                    detail="same block",
                ),
            ),
        )
        legend = self._legend("LE-soil", "суглинок", "legend-vector-v1:soil")
        primitive = self._line(300.0, 300.0, 330.0, 300.0)
        result = attach_field_geometry(
            self._page(legends=[legend], inserts=[soil], bindings=[binding]),
            [primitive],
        )
        self.assertEqual("confirmed", result.symbol_instances[0].status)
        self.assertEqual("field_candidate", result.symbol_instances[0].role)
        self.assertEqual("LE-soil", result.symbol_instances[0].legend_entry_id)
        self.assertEqual("", result.field_geometry[0].label)
        result.validate()

    def test_pzu_without_legend_stays_unlabeled(self) -> None:
        primitive = self._line(300.0, 300.0, 330.0, 300.0)
        result = attach_field_geometry(
            self._page(legends=[], zones={
                "title_block": [656.0, 0.0, 841.0, 55.0],
                "drawing_field": [0.0, 55.0, 841.0, 594.0],
            }),
            [primitive],
        )
        self.assertEqual(1, len(result.field_geometry))
        self.assertEqual("", result.field_geometry[0].label)
        self.assertEqual([], result.symbol_bindings)

    def test_unique_pzu_same_sheet_sample_sets_label(self) -> None:
        cell = self._hatch(
            [[60.0, 90.0], [90.0, 90.0], [90.0, 110.0], [60.0, 110.0], [60.0, 90.0]]
        )
        field = self._hatch(
            [
                [300.0, 200.0],
                [420.0, 200.0],
                [420.0, 280.0],
                [300.0, 280.0],
                [300.0, 200.0],
            ]
        )
        legend = self._legend(
            "LE-fence",
            "проектируемое ограждение (периметровое)",
            "legend-vector-v1:not-the-hatch",
        )
        result = attach_field_geometry(
            self._page(legends=[legend]),
            primitives=[],
            hatches=[cell, field],
        )
        self.assertEqual(1, len(result.field_geometry))
        item = result.field_geometry[0]
        self.assertEqual("hatch", item.kind)
        self.assertEqual("проектируемое ограждение (периметровое)", item.label)
        self.assertEqual("LE-fence", item.legend_entry_id)

    def test_pzu_four_label_conflict_stays_unlabeled(self) -> None:
        """Conflict 4 analog: one hatch, four PZU captions — no transfer."""

        cells = []
        legends = []
        captions = (
            "отмостка/пеш. дорожки из асфальтобетона",
            "кизильник укрытый слоем мульчи (проект.)",
            "озеленение (газон)",
            "проезды и площадки из асфальтобетона",
        )
        for index, caption in enumerate(captions):
            y0 = 90.0 + index * 28.0
            cells.append(
                self._hatch(
                    [
                        [60.0, y0],
                        [90.0, y0],
                        [90.0, y0 + 18.0],
                        [60.0, y0 + 18.0],
                        [60.0, y0],
                    ]
                )
            )
            legends.append(
                self._legend(
                    f"LE-pzu-{index}",
                    caption,
                    f"legend-vector-v1:pzu-{index}",
                    bbox=(60.0, y0, 200.0, y0 + 18.0),
                    symbol_bbox=(60.0, y0, 90.0, y0 + 18.0),
                )
            )
        field = self._hatch(
            [
                [300.0, 200.0],
                [380.0, 200.0],
                [380.0, 260.0],
                [300.0, 260.0],
                [300.0, 200.0],
            ]
        )
        result = attach_field_geometry(
            self._page(legends=legends),
            primitives=[],
            hatches=[*cells, field],
        )
        self.assertEqual(1, len(result.field_geometry))
        self.assertEqual("", result.field_geometry[0].label)
        self.assertEqual("", result.field_geometry[0].legend_entry_id)

    def test_pzu_legend_without_step2_geometry_is_not_applied(self) -> None:
        legend = self._legend(
            "LE-fence",
            "проектируемое ограждение (периметровое)",
            "legend-vector-v1:fence",
        )
        result = attach_field_geometry(self._page(legends=[legend]), [], [])
        self.assertEqual([], result.field_geometry)

    def test_geology_soil_insert_is_not_pzu_legend(self) -> None:
        soil = FurnitureTests._insert(
            "SI-soil",
            layer="I-WALL",
            block_name="грунт",
            status="confirmed",
            role="field_candidate",
            legend_entry_id="LE-soil",
        )
        binding = SymbolBinding(
            id="SB-soil",
            page=1,
            instance_id=soil.id,
            legend_entry_id="LE-soil",
            status="confirmed",
            confidence=1.0,
            evidence=(
                Evidence(
                    kind="exact_block_definition",
                    score=1.0,
                    source_ids=("LE-soil",),
                    detail="same block",
                ),
            ),
        )
        legends = [
            self._legend("LE-soil", "суглинок", "legend-vector-v1:soil"),
            self._legend(
                "LE-lawn",
                "озеленение (газон)",
                "legend-vector-v1:lawn",
                bbox=(60.0, 150.0, 200.0, 170.0),
                symbol_bbox=(60.0, 150.0, 90.0, 170.0),
            ),
        ]
        lawn_cell = self._hatch(
            [
                [60.0, 150.0],
                [90.0, 150.0],
                [90.0, 170.0],
                [60.0, 170.0],
                [60.0, 150.0],
            ]
        )
        lawn_field = self._hatch(
            [
                [300.0, 200.0],
                [360.0, 200.0],
                [360.0, 260.0],
                [300.0, 260.0],
                [300.0, 200.0],
            ]
        )
        result = attach_field_geometry(
            self._page(legends=legends, inserts=[soil], bindings=[binding]),
            primitives=[],
            hatches=[lawn_cell, lawn_field],
        )
        item = result.symbol_instances[0]
        self.assertEqual("confirmed", item.status)
        self.assertEqual("field_candidate", item.role)
        self.assertEqual("LE-soil", item.legend_entry_id)
        self.assertNotIn("озеленение", item.block_name or "")
        labeled = [row for row in result.field_geometry if row.label]
        self.assertEqual(["озеленение (газон)"], [row.label for row in labeled])

    def test_pzu_legend_does_not_travel_to_foreign_sheet(self) -> None:
        field = self._hatch(
            [
                [300.0, 200.0],
                [360.0, 200.0],
                [360.0, 260.0],
                [300.0, 260.0],
                [300.0, 200.0],
            ]
        )
        result = attach_field_geometry(
            self._page(legends=[]),
            primitives=[],
            hatches=[field],
        )
        self.assertEqual(1, len(result.field_geometry))
        self.assertEqual("", result.field_geometry[0].label)
        self.assertEqual("", result.field_geometry[0].legend_entry_id)

    def test_simple_legend_stroke_labels_long_field_line(self) -> None:
        tick = self._line(62.0, 100.0, 88.0, 100.0)
        wall = self._line(300.0, 400.0, 520.0, 400.0)
        legend = self._legend(
            "LE-wall",
            "стена несущая",
            "legend-vector-v1:cell-hash-not-the-wall",
        )
        result = attach_field_geometry(self._page(legends=[legend]), [tick, wall])
        labeled = [item for item in result.field_geometry if item.label]
        self.assertEqual(1, len(labeled))
        self.assertEqual("стена несущая", labeled[0].label)
        self.assertEqual("LE-wall", labeled[0].legend_entry_id)
        self.assertEqual("line", labeled[0].kind)
        self.assertGreater(labeled[0].bbox[2] - labeled[0].bbox[0], 100.0)

    def test_shared_simple_stroke_stays_unlabeled(self) -> None:
        tick = self._line(62.0, 100.0, 88.0, 100.0)
        wall = self._line(300.0, 400.0, 520.0, 400.0)
        legends = [
            self._legend("LE-a", "стена несущая", "legend-vector-v1:a"),
            self._legend("LE-b", "стена ненесущая", "legend-vector-v1:b"),
        ]
        result = attach_field_geometry(self._page(legends=legends), [tick, wall])
        field = [item for item in result.field_geometry if item.kind == "line"]
        self.assertEqual(1, len(field))
        self.assertEqual("", field[0].label)
        self.assertEqual("", field[0].legend_entry_id)

    def test_unique_hatch_pattern_sets_label(self) -> None:
        cell = self._hatch(
            [[60.0, 90.0], [90.0, 90.0], [90.0, 110.0], [60.0, 110.0], [60.0, 90.0]]
        )
        field = self._hatch(
            [
                [300.0, 200.0],
                [360.0, 200.0],
                [360.0, 260.0],
                [300.0, 260.0],
                [300.0, 200.0],
            ]
        )
        legend = self._legend(
            "LE-soil", "суглинок", "legend-vector-v1:not-the-hatch"
        )
        result = attach_field_geometry(
            self._page(legends=[legend]),
            primitives=[],
            hatches=[cell, field],
        )
        self.assertEqual(1, len(result.field_geometry))
        item = result.field_geometry[0]
        self.assertEqual("hatch", item.kind)
        self.assertEqual("суглинок", item.label)
        self.assertEqual("LE-soil", item.legend_entry_id)
        self.assertTrue(item.signature.startswith("hatch-pattern-v1:ANSI31"))

    def test_shared_hatch_pattern_stays_unlabeled(self) -> None:
        cell_a = self._hatch(
            [[60.0, 90.0], [90.0, 90.0], [90.0, 110.0], [60.0, 110.0], [60.0, 90.0]]
        )
        cell_b = self._hatch(
            [
                [60.0, 120.0],
                [90.0, 120.0],
                [90.0, 140.0],
                [60.0, 140.0],
                [60.0, 120.0],
            ]
        )
        field = self._hatch(
            [
                [300.0, 200.0],
                [360.0, 200.0],
                [360.0, 260.0],
                [300.0, 260.0],
                [300.0, 200.0],
            ]
        )
        legends = [
            self._legend("LE-a", "суглинок", "legend-vector-v1:a"),
            self._legend(
                "LE-b",
                "супесь",
                "legend-vector-v1:b",
                bbox=(60.0, 120.0, 200.0, 140.0),
                symbol_bbox=(60.0, 120.0, 90.0, 140.0),
            ),
        ]
        result = attach_field_geometry(
            self._page(legends=legends),
            primitives=[],
            hatches=[cell_a, cell_b, field],
        )
        self.assertEqual(1, len(result.field_geometry))
        self.assertEqual("hatch", result.field_geometry[0].kind)
        self.assertEqual("", result.field_geometry[0].label)

    def test_labeled_open_line_survives_closed_budget(self) -> None:
        sample = self._line(60.0, 100.0, 90.0, 100.0)
        wall = self._line(300.0, 400.0, 400.0, 400.0)
        legend = self._legend(
            "LE-wall", "стена несущая", self._signature(sample)
        )
        closed = [
            self._square(400.0 + (index % 10) * 20.0, 250.0 + (index // 10) * 20.0)
            for index in range(160)
        ]
        result = attach_field_geometry(
            self._page(legends=[legend]),
            [*closed, wall],
        )
        self.assertEqual(160, len(result.field_geometry))
        labeled = [item for item in result.field_geometry if item.label]
        self.assertEqual(1, len(labeled))
        self.assertEqual("line", labeled[0].kind)
        self.assertEqual("стена несущая", labeled[0].label)
        self.assertEqual(159, sum(1 for item in result.field_geometry if item.kind == "closed"))

    def test_html_hatch_card_title(self) -> None:
        from dwg_symbols.html_report import _field_geometry_card

        html = _field_geometry_card(
            {
                "id": "FG-hatch",
                "kind": "hatch",
                "label": "",
                "layer": "GRUNT",
                "source": "sheet_primitive",
                "bbox": [300.0, 200.0, 360.0, 260.0],
                "note": "",
            }
        )
        self.assertIn("Штриховка", html)
        self.assertIn("линия / штриховка", html)

    def test_html_field_geometry_card_shows_unlabeled_line(self) -> None:
        from dwg_symbols.html_report import _field_geometry_card

        html = _field_geometry_card(
            {
                "id": "FG-1",
                "kind": "line",
                "label": "",
                "layer": "WALL",
                "source": "sheet_primitive",
                "bbox": [300.0, 300.0, 330.0, 301.0],
                "note": "",
            }
        )
        self.assertIn("линия / штриховка", html)
        self.assertIn("не INSERT", html)
        self.assertIn("нет образца этого листа", html)

    def test_old_sidecar_without_field_geometry_still_summarizes(self) -> None:
        result = PageResult(
            document_id="DOC-test",
            document_path="test.dwg",
            page=1,
            completeness="complete",
            sheet_zones={
                "title_block": [645.0, 10.0, 830.0, 65.0],
                "drawing_field": [0.0, 65.0, 841.0, 594.0],
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            page_dir = write_page_result(directory, result)
            summary = json.loads((page_dir / "summary.json").read_text(encoding="utf-8"))
            geometry = json.loads(
                (page_dir / "field_geometry.json").read_text(encoding="utf-8")
            )
        self.assertEqual(0, summary["counts"]["fieldGeometry"])
        self.assertEqual([], geometry["items"])


class TextLabelTests(unittest.TestCase):
    """Standalone TEXT: axis and linear size, without inventing INSERT."""

    ZONES = {
        "title_block": [656.0, 0.0, 841.0, 55.0],
        "drawing_field": [0.0, 55.0, 841.0, 594.0],
    }

    def _page(
        self,
        inserts: list[SymbolInstance] | tuple[SymbolInstance, ...] = (),
        texts: list[TextItem] | tuple[TextItem, ...] = (),
        *,
        zones: dict | None = None,
    ) -> PageResult:
        result = classify_sheet_furniture(FurnitureTests._page(list(inserts)))
        result.sheet_zones = dict(self.ZONES if zones is None else zones)
        result = attach_dimensions(result, list(texts))
        result = attach_axes(result, list(texts))
        return attach_text_labels(result, list(texts))

    def test_nearby_letter_and_digit_on_osi_are_one_axis(self) -> None:
        texts = [
            TextItem(100.0, 200.0, 3.0, "Ж", "text", layer="OSI"),
            TextItem(104.0, 200.0, 3.0, "7", "text", layer="OSI"),
        ]
        result = self._page(texts=texts)
        self.assertEqual(1, len(result.text_labels))
        label = result.text_labels[0]
        self.assertEqual("axis", label.kind)
        self.assertEqual("Ж / 7", label.text)
        self.assertEqual("Ж", label.letter)
        self.assertEqual("7", label.digit)
        self.assertEqual([], [item.role for item in result.symbol_instances])

    def test_cyrillic_osi_layer_is_an_axis(self) -> None:
        texts = [
            TextItem(100.0, 200.0, 3.0, "А", "text", layer="ОСИ"),
            TextItem(104.0, 200.0, 3.0, "2", "text", layer="ОСИ"),
        ]
        result = self._page(texts=texts)
        self.assertEqual("axis", result.text_labels[0].kind)
        self.assertEqual("А / 2", result.text_labels[0].text)

    def test_dimension_entity_on_razmer_is_linear(self) -> None:
        texts = [TextItem(120.0, 180.0, 2.5, "3500", "dimension", layer="RAZMER")]
        result = self._page(texts=texts)
        self.assertEqual(1, len(result.text_labels))
        self.assertEqual("linear", result.text_labels[0].kind)
        self.assertEqual("3500", result.text_labels[0].value)

    def test_attrib_is_not_a_standalone_label(self) -> None:
        texts = [TextItem(100.0, 200.0, 3.0, "Ж", "attrib", layer="OSI")]
        result = self._page(texts=texts)
        self.assertEqual([], result.text_labels)

    def test_stamp_zone_text_is_skipped(self) -> None:
        texts = [TextItem(700.0, 20.0, 3.5, "П", "text", layer="OSI")]
        result = self._page(texts=texts)
        self.assertEqual([], result.text_labels)

    def test_room_number_on_layer_0_is_not_an_axis(self) -> None:
        texts = [TextItem(200.0, 300.0, 2.5, "5", "text", layer="0")]
        result = self._page(texts=texts)
        self.assertEqual([], result.text_labels)

    def test_insert_owned_axis_is_not_duplicated(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U52",
            attributes={"Ось": "7", "Ось'": "Ж"},
            x=100.0,
            y=200.0,
        )
        texts = [
            TextItem(100.0, 200.0, 3.0, "Ж", "text", layer="OSI"),
            TextItem(100.0, 200.0, 3.0, "7", "text", layer="OSI"),
        ]
        result = self._page(inserts=[axis], texts=texts)
        self.assertEqual("Ж / 7", result.symbol_instances[0].axis["label"])
        self.assertEqual([], result.text_labels)

    def test_nadpisi_and_format_are_skipped(self) -> None:
        texts = [
            TextItem(150.0, 200.0, 2.5, "А", "text", layer="NADPISI"),
            TextItem(160.0, 200.0, 2.5, "12", "text", layer="FORMAT"),
        ]
        result = self._page(texts=texts)
        self.assertEqual([], result.text_labels)

    def test_unpaired_letter_keeps_a_note(self) -> None:
        texts = [TextItem(100.0, 200.0, 3.0, "Ж", "text", layer="OSI")]
        result = self._page(texts=texts)
        self.assertEqual(1, len(result.text_labels))
        self.assertEqual("axis_letter", result.text_labels[0].kind)
        self.assertIn("цифры", result.text_labels[0].note)

    def test_html_text_label_card_shows_axis(self) -> None:
        from dwg_symbols.html_report import _text_label_card

        html = _text_label_card(
            {
                "id": "TL-1",
                "kind": "axis",
                "text": "Ж / 7",
                "letter": "Ж",
                "digit": "7",
                "source": "text",
                "layer": "OSI",
                "x": 100.0,
                "y": 200.0,
                "note": "",
            }
        )
        self.assertIn("Ж / 7", html)
        self.assertIn("текст вне блока", html)
        self.assertIn("Ось", html)
        self.assertIn("не INSERT", html)

    def test_confirmed_legend_insert_stays_untouched(self) -> None:
        hit = FurnitureTests._insert(
            "SI-legend",
            layer="I-WALL",
            block_name="грунт",
            status="confirmed",
            role="field_candidate",
            legend_entry_id="LE-soil",
        )
        texts = [TextItem(10.0, 80.0, 3.0, "Ж", "text", layer="OSI")]
        result = self._page(inserts=[hit], texts=texts)
        self.assertEqual("confirmed", result.symbol_instances[0].status)
        self.assertEqual("field_candidate", result.symbol_instances[0].role)
        self.assertEqual("axis_letter", result.text_labels[0].kind)


class ScheduleJoinTests(unittest.TestCase):
    """Code → kit table row, or an honest note. No invented schedule."""

    def _page(
        self,
        *inserts: SymbolInstance,
        texts: list[TextItem] | tuple[TextItem, ...] = (),
        rows: list[dict[str, str]] | None = None,
        zones: dict | None = None,
        legends: list[LegendEntry] | None = None,
        bindings: list[SymbolBinding] | None = None,
    ) -> PageResult:
        result = classify_sheet_furniture(
            FurnitureTests._page(list(inserts), legends=legends, bindings=bindings)
        )
        if zones is not None:
            result.sheet_zones = dict(zones)
        result = attach_axes(result, list(texts))
        result = attach_text_labels(result, list(texts))
        return attach_schedule_notes(result, rows)

    def test_unique_axis_row_sets_expansion(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U52",
            attributes={"Ось": "2", "Ось'": "А"},
        )
        result = self._page(
            axis,
            rows=[{"kind": "axis", "code": "А", "label": "блок 1"}],
        )
        item = result.symbol_instances[0]
        self.assertEqual("specification_mark", item.role)
        self.assertEqual("А / 2", item.axis["label"])
        self.assertEqual("ось А — блок 1", item.schedule["label"])
        self.assertEqual("table", item.schedule["source"])
        self.assertEqual("", item.schedule["note"])
        self.assertNotIn(NOTE_TABLE_MISSING, item.axis["note"])

    def test_unique_room_row_sets_expansion(self) -> None:
        room = FurnitureTests._insert(
            "SI-room",
            layer="0",
            block_name="номерация 5",
        )
        result = self._page(
            room,
            rows=[{"kind": "room", "code": "5", "label": "кухня"}],
        )
        item = result.symbol_instances[0]
        self.assertEqual("specification_mark", item.role)
        self.assertIsNone(item.axis)
        self.assertEqual("помещение 5 — кухня", item.schedule["label"])
        self.assertEqual("5", item.schedule["code"])

    def test_missing_table_stamps_note_and_keeps_mark_role(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U52",
            attributes={"Ось": "7", "Ось'": "Ж"},
        )
        result = self._page(axis)
        item = result.symbol_instances[0]
        self.assertEqual("specification_mark", item.role)
        self.assertEqual("Ж / 7", item.axis["label"])
        self.assertEqual("", item.schedule["label"])
        self.assertEqual(NOTE_TABLE_MISSING, item.schedule["note"])
        self.assertIn(NOTE_TABLE_MISSING, item.axis["note"])
        self.assertNotIn("ось Ж —", item.schedule["label"])

    def test_empty_grafa_stays_unread_not_axis_absent(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U52",
            attributes={"Ось": "7", "Ось'": ""},
        )
        result = self._page(axis)
        note = result.symbol_instances[0].axis["note"]
        self.assertIn("буква не прочитана", note)
        self.assertIn(NOTE_TABLE_MISSING, note)
        self.assertNotIn("оси нет", note)

    def test_soil_and_stamp_are_not_codes(self) -> None:
        soil = FurnitureTests._insert(
            "SI-soil",
            layer="I-WALL",
            block_name="грунт",
            status="confirmed",
            role="field_candidate",
            legend_entry_id="LE-soil",
        )
        stamp = FurnitureTests._insert(
            "SI-stamp",
            layer="FORMAT",
            block_name="*U704",
            attributes={"ЛИСТ": "1", "СТАДИЯ": "П", "ГИП": "Иванов", "ФОРМАТ": "А1"},
        )
        legend = LegendEntry(
            id="LE-soil",
            page=1,
            label="Глина",
            status="extracted",
            source_kind="dwg_vector_legend",
            signature="legend-vector-v1:soil",
            confidence=0.9,
        )
        binding = SymbolBinding(
            id="SB-soil",
            page=1,
            instance_id=soil.id,
            legend_entry_id=legend.id,
            status="confirmed",
            confidence=1.0,
            evidence=(
                Evidence(
                    kind="exact_block_definition",
                    score=1.0,
                    source_ids=(legend.id,),
                    detail="same block",
                ),
            ),
        )
        result = self._page(
            soil,
            stamp,
            legends=[legend],
            bindings=[binding],
        )
        by_id = {item.id: item for item in result.symbol_instances}
        self.assertEqual("confirmed", by_id["SI-soil"].status)
        self.assertIsNone(by_id["SI-soil"].schedule)
        self.assertEqual("sheet_furniture", by_id["SI-stamp"].role)
        self.assertIsNone(by_id["SI-stamp"].schedule)
        self.assertIsNone(by_id["SI-stamp"].axis)

    def test_without_customer_table_does_not_invent_expansion(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U143",
            attributes={"Ось": "2", "Ось'": "А"},
        )
        result = self._page(axis, rows=[])
        item = result.symbol_instances[0]
        self.assertEqual("", item.schedule["label"])
        self.assertEqual(NOTE_TABLE_MISSING, item.schedule["note"])
        self.assertNotIn("продольн", (item.schedule["label"] + item.axis["note"]).casefold())
        self.assertNotIn("ось А —", item.schedule["label"])

    def test_conflicting_rows_do_not_expand(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U143",
            attributes={"Ось": "2", "Ось'": "А"},
        )
        result = self._page(
            axis,
            rows=[
                {"kind": "axis", "code": "А", "label": "блок 1"},
                {"kind": "axis", "code": "А", "label": "блок 2"},
            ],
        )
        item = result.symbol_instances[0]
        self.assertEqual("", item.schedule["label"])
        self.assertEqual(NOTE_TABLE_CONFLICT, item.schedule["note"])

    def test_present_table_without_this_code_is_row_missing(self) -> None:
        axis = FurnitureTests._insert(
            "SI-axis",
            layer="OSI",
            block_name="*U143",
            attributes={"Ось": "2", "Ось'": "А"},
        )
        result = self._page(
            axis,
            rows=[{"kind": "axis", "code": "Б", "label": "блок 2"}],
        )
        self.assertEqual(NOTE_ROW_MISSING, result.symbol_instances[0].schedule["note"])
        self.assertEqual("", result.symbol_instances[0].schedule["label"])

    def test_text_letter_and_digit_are_not_glued_for_join(self) -> None:
        zones = {
            "title_block": [656.0, 0.0, 841.0, 55.0],
            "drawing_field": [0.0, 55.0, 841.0, 594.0],
        }
        texts = [
            TextItem(100.0, 200.0, 3.0, "А", "text", layer="OSI"),
            TextItem(400.0, 200.0, 3.0, "2", "text", layer="OSI"),
        ]
        result = self._page(
            texts=texts,
            zones=zones,
            rows=[{"kind": "axis", "code": "А", "label": "блок 1"}],
        )
        by_kind = {item.kind: item for item in result.text_labels}
        self.assertEqual("ось А — блок 1", by_kind["axis_letter"].note.split(";")[0].strip())
        self.assertIn(NOTE_ROW_MISSING, by_kind["axis_digit"].note)
        self.assertNotIn("ось А —", by_kind["axis_digit"].note)
        self.assertNotIn("А / 2", [item.text for item in result.text_labels])

    def test_linear_text_is_not_a_code(self) -> None:
        zones = {
            "title_block": [656.0, 0.0, 841.0, 55.0],
            "drawing_field": [0.0, 55.0, 841.0, 594.0],
        }
        texts = [TextItem(120.0, 180.0, 2.5, "3500", "dimension", layer="RAZMER")]
        result = self._page(texts=texts, zones=zones)
        self.assertEqual("linear", result.text_labels[0].kind)
        self.assertNotIn(NOTE_TABLE_MISSING, result.text_labels[0].note)

    def test_html_join_and_missing_table(self) -> None:
        from dwg_symbols.html_report import _classified_card

        joined = self._page(
            FurnitureTests._insert(
                "SI-axis",
                layer="OSI",
                block_name="*U143",
                attributes={"Ось": "2", "Ось'": "А"},
            ),
            rows=[{"kind": "axis", "code": "А", "label": "блок 1"}],
        ).symbol_instances[0]
        html = _classified_card(joined.to_dict(), None, Path("."))
        self.assertIn("ось А — блок 1", html)
        self.assertIn("А / 2", html)

        missing = self._page(
            FurnitureTests._insert(
                "SI-room",
                layer="0",
                block_name="номерация 5",
            )
        ).symbol_instances[0]
        room_html = _classified_card(missing.to_dict(), None, Path("."))
        self.assertIn("Помещение", room_html)
        self.assertIn("5", room_html)
        self.assertIn(NOTE_TABLE_MISSING, room_html)
        self.assertNotIn("оси нет", room_html)


class CircleKeyTests(unittest.TestCase):
    def test_parse_circle_block_name(self) -> None:
        self.assertEqual(7, parse_circle_block_name("круг7"))
        self.assertEqual(1, parse_circle_block_name("круг1"))
        self.assertEqual(12, parse_circle_block_name("круг12"))
        self.assertEqual("⑦", circled_number(7))
        self.assertEqual("①", circled_number(1))
        self.assertIsNone(parse_circle_block_name("круг"))
        self.assertIsNone(parse_circle_block_name("круг 7"))
        self.assertIsNone(parse_circle_block_name("Грунт"))
        self.assertIsNone(parse_circle_block_name("FIRE"))
        self.assertIsNone(parse_circle_block_name("круг0"))
        self.assertIsNone(parse_circle_block_name("круг21"))
        self.assertIsNone(parse_circle_block_name(None))

    def test_confirmed_card_title_uses_circled_number_and_label(self) -> None:
        from dwg_symbols.html_report import _recognized_card

        html = _recognized_card(
            {
                "id": "SI-soil",
                "position": {"x": 10, "y": 20, "units": "mm"},
                "layer": "Fill",
                "blockName": "круг7",
                "sourceKind": "dwg_insert_candidate",
                "attributes": {},
            },
            {
                "status": "confirmed",
                "confidence": 1.0,
                "evidence": [{"kind": "exact_block_definition", "detail": "same block"}],
            },
            {"label": "Песок пылеватый коричневато-серый"},
            None,
            Path("."),
        )
        self.assertIn("<h3>⑦ — Песок пылеватый коричневато-серый</h3>", html)
        self.assertIn("Песок пылеватый коричневато-серый", html)
        self.assertIn(">круг7</dd>", html)
        self.assertNotIn("<h3>круг7</h3>", html)
        self.assertNotIn("ИГЭ-7", html)

    def test_soil_without_circle_block_has_no_number_on_card(self) -> None:
        from dwg_symbols.html_report import _recognized_card

        html = _recognized_card(
            {
                "id": "SI-wall",
                "position": {"x": 10, "y": 20, "units": "mm"},
                "layer": "WALL",
                "blockName": "FIRE",
                "sourceKind": "dwg_insert_candidate",
                "attributes": {},
            },
            {
                "status": "confirmed",
                "confidence": 1.0,
                "evidence": [{"kind": "exact_block_definition", "detail": "same block"}],
            },
            {"label": "Стена огнестойкая"},
            None,
            Path("."),
        )
        self.assertEqual("Стена огнестойкая", circle_heading("FIRE", "Стена огнестойкая"))
        self.assertIn("<h3>Стена огнестойкая</h3>", html)
        self.assertNotIn("①", html)
        self.assertNotIn("⑦", html)


class IdealMdTests(unittest.TestCase):
    @staticmethod
    def _legend(
        entry_id: str,
        label: str,
        reference_signatures: tuple[str, ...] = (),
    ) -> LegendEntry:
        return LegendEntry(
            id=entry_id,
            page=1,
            label=label,
            status="extracted",
            source_kind="dwg_vector_legend",
            signature=f"legend-vector-v1:{entry_id}",
            bbox=(0.0, 0.0, 40.0, 12.0),
            symbol_bbox=(0.0, 0.0, 12.0, 12.0),
            reference_signatures=reference_signatures,
            confidence=0.9,
        )

    @staticmethod
    def _insert(
        instance_id: str,
        *,
        status: str = "unresolved",
        role: str = "field_candidate",
        layer: str = "Skv",
        block_name: str = "CPE",
        legend_entry_id: str | None = None,
        classification_reason: str | None = None,
        signature: str = "blockdef-v1:cpe",
    ) -> SymbolInstance:
        return SymbolInstance(
            id=instance_id,
            page=1,
            source_kind="dwg_insert_candidate",
            status=status,
            position=Point(10.0, 20.0, "paper", "mm"),
            source_handle=instance_id,
            source_space="modelspace",
            layer=layer,
            signature=signature,
            block_name=block_name,
            role=role,
            legend_entry_id=legend_entry_id,
            classification_reason=classification_reason,
            confidence=1.0,
        )

    def _page(
        self,
        *,
        legends: list[LegendEntry] | None = None,
        instances: list[SymbolInstance] | None = None,
        bindings: list[SymbolBinding] | None = None,
        unknown: list[UnknownSymbolCluster] | None = None,
        title_block: dict | None = None,
    ) -> PageResult:
        legends = list(legends or [])
        instances = list(instances or [])
        bindings = list(bindings or [])
        if unknown is None:
            grouped: dict[str, list[SymbolInstance]] = {}
            for instance in instances:
                if instance.role == "field_candidate" and instance.status in {
                    "unresolved",
                    "unclassified",
                }:
                    grouped.setdefault(instance.signature, []).append(instance)
            unknown = [
                UnknownSymbolCluster(
                    id=stable_id("US", signature),
                    page=1,
                    signature=signature,
                    instance_ids=tuple(item.id for item in items),
                    reason=items[0].classification_reason or "NO_LEGEND_BINDING_BASELINE",
                    representative_instance_id=items[0].id,
                )
                for signature, items in grouped.items()
            ]
        return PageResult(
            document_id="DOC-ideal",
            document_path="geo.dwg",
            page=1,
            completeness="partial",
            legend_entries=legends,
            symbol_instances=instances,
            symbol_bindings=bindings,
            unknown_symbols=list(unknown),
            title_block=title_block,
        )

    def _write(self, directory: str | Path, result: PageResult) -> Path:
        return write_page_result(directory, result)

    def test_stamp_and_legend_labels_in_markdown(self) -> None:
        legend = self._legend(
            "LE-clay",
            "Глина коричневая, легкая, тугопластичная, aQIII",
        )
        result = self._page(
            legends=[legend],
            title_block={
                "code": "28-ХСА-1/25-КР1",
                "sheet": "17",
                "sheetsTotal": "",
                "stage": "П",
                "title": "Геологические разрезы по линиям XII-XII...XIV-XIV",
                "objectName": "Индустриал Сити Жуковский 1",
                "org": "ООО «КУРСКРЕГИОНПРОЕКТ»",
                "source": "merged",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            page_dir = self._write(directory, result)
            markdown = render_ideal_markdown(page_dir)
        self.assertIn("## Страница 1", markdown)
        self.assertIn("Чертёж: `geo.dwg`", markdown)
        self.assertIn("### Основная надпись", markdown)
        self.assertIn("28-ХСА-1/25-КР1", markdown)
        self.assertIn("| Лист | 17 |", markdown)
        self.assertIn("| Листов | не прочитано |", markdown)
        self.assertIn("### УСЛОВНЫЕ ОБОЗНАЧЕНИЯ", markdown)
        self.assertIn(
            "*   Глина коричневая, легкая, тугопластичная, aQIII", markdown
        )
        self.assertNotIn("символ глины", markdown)
        self.assertNotIn("ИГЭ-1", markdown)

    def test_empty_slots_are_unread_and_no_invented_views(self) -> None:
        result = self._page(
            legends=[self._legend("LE-soil", "Почвенно-растительный слой solQIV")],
            title_block={
                "code": "28-ХСА-1/25-КР1",
                "sheet": "17",
                "stage": "П",
                "title": "Геологические разрезы по линиям XII-XII...XIV-XIV",
                "source": "merged",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            page_dir = self._write(directory, result)
            markdown = render_ideal_markdown(page_dir)
        self.assertIn(f"### Описание: {WHOLE_SHEET_TITLE}", markdown)
        self.assertEqual(1, markdown.count("### Описание:"))
        self.assertNotIn("### Описание: Разрез", markdown)
        self.assertIn("**ЧТО изображено:**\n" + UNREAD, markdown)
        self.assertIn("**ИЗ ЧЕГО состоит:**\n" + UNREAD, markdown)
        self.assertIn("**ГДЕ расположены элементы:**\n" + UNREAD, markdown)
        self.assertIn("**КАК связаны:**\n" + UNREAD, markdown)
        self.assertIn(GAP_VIEWS, markdown)
        self.assertIn(GAP_NOTES, markdown)

    def test_unreadable_legend_is_not_invented(self) -> None:
        blank = LegendEntry(
            id="LE-blank",
            page=1,
            label=None,
            status="text_unreadable",
            source_kind="dwg_vector_legend",
            confidence=0.0,
        )
        result = self._page(legends=[blank, self._legend("LE-ok", "Известняк C3kr")])
        with tempfile.TemporaryDirectory() as directory:
            markdown = render_ideal_markdown(self._write(directory, result))
        self.assertIn("*   Известняк C3kr", markdown)
        self.assertNotIn("символ", markdown.lower())
        self.assertNotIn("LE-blank", markdown)

    def test_scenes_emit_one_description_each(self) -> None:
        result = self._page(legends=[self._legend("LE-ok", "Песок мелкий")])
        with tempfile.TemporaryDirectory() as directory:
            page_dir = self._write(directory, result)
            (page_dir / "sheet_scenes.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "page": 1,
                        "items": [
                            {"title": "Схема расположения скважин на плане"},
                            {"title": "Разрез по линии XII-XII"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            markdown = render_ideal_markdown(page_dir)
        self.assertIn("### Описание: Схема расположения скважин на плане", markdown)
        self.assertIn("### Описание: Разрез по линии XII-XII", markdown)
        self.assertEqual(2, markdown.count("### Описание:"))
        self.assertNotIn(f"### Описание: {WHOLE_SHEET_TITLE}", markdown)
        self.assertNotIn(GAP_VIEWS, markdown)

    def test_roman_line_scene_fills_what_slot(self) -> None:
        result = self._page()
        result.sheet_scenes = [
            SheetScene(
                id="SC-xii",
                page=1,
                title="XII-XII (по отчету ИГИ)",
                bbox=(0.0, 55.0, 640.0, 594.0),
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            markdown = render_ideal_markdown(self._write(directory, result))
        self.assertIn("### Описание: XII-XII (по отчету ИГИ)", markdown)
        self.assertIn(
            "**ЧТО изображено:**\nГеологический разрез по линии XII-XII",
            markdown,
        )
        self.assertNotIn("ИГЭ-12", markdown)

    def test_cluster_fills_description_slots_and_expected_gaps(self) -> None:
        legend = self._legend("LE-sand", "Песок пылеватый коричневато-серый")
        upper = FurnitureTests._insert(
            "SI-soil7",
            layer="Fill",
            block_name="круг7",
            status="confirmed",
            legend_entry_id=legend.id,
            signature="blockdef-v1:krug7",
            x=250.0,
            y=430.0,
        )
        lower = FurnitureTests._insert(
            "SI-soil2",
            layer="Fill",
            block_name="круг2",
            status="confirmed",
            legend_entry_id=legend.id,
            signature="blockdef-v1:krug2",
            x=260.0,
            y=360.0,
        )
        axes = [
            replace(
                FurnitureTests._insert(
                    f"SI-axis{number}",
                    layer="OSI",
                    block_name="*U52",
                    status="ignored",
                    role="specification_mark",
                    classification_reason=AXIS_LAYER_REASON,
                    signature=f"blockdef-v1:axis{number}",
                    x=200.0 + number * 8.0,
                    y=450.0,
                    attributes={"Ось": str(number), "Ось'": "Ж"},
                ),
                axis={
                    "letter": "Ж",
                    "digit": str(number),
                    "label": f"Ж / {number}",
                    "source": "attributes",
                    "note": "",
                },
            )
            for number in range(1, 16)
        ]
        binding_evidence = (
            Evidence(
                kind="exact_block_definition",
                score=1.0,
                source_ids=(legend.id,),
                detail="same block",
            ),
        )
        result = FurnitureTests._page(
            [upper, lower, *axes],
            legends=[legend],
            bindings=[
                SymbolBinding(
                    id="SB-7",
                    page=1,
                    instance_id=upper.id,
                    legend_entry_id=legend.id,
                    status="confirmed",
                    confidence=1.0,
                    evidence=binding_evidence,
                ),
                SymbolBinding(
                    id="SB-2",
                    page=1,
                    instance_id=lower.id,
                    legend_entry_id=legend.id,
                    status="confirmed",
                    confidence=1.0,
                    evidence=binding_evidence,
                ),
            ],
        )
        result.sheet_zones = dict(SheetSceneTests.ZONES)
        result.title_block = {
            "code": "28-ХСА-1/25-КР1",
            "sheet": "17",
            "stage": "П",
            "source": "merged",
        }
        result.sheet_scenes = [
            SheetScene(
                id="SC-xii",
                page=1,
                title="Разрез по линии XII-XII",
                bbox=(0.0, 55.0, 640.0, 594.0),
                soil_ids=(upper.id, lower.id),
                axis_ids=tuple(item.id for item in axes),
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            markdown = render_ideal_markdown(self._write(directory, result))
        self.assertIn("### Описание: Разрез по линии XII-XII", markdown)
        self.assertIn("**ЧТО изображено:**\nГеологический разрез по линии XII-XII", markdown)
        self.assertIn("**ИЗ ЧЕГО состоит:**\nоси 1–15, слои ⑦②", markdown)
        self.assertIn("**КАК связаны:**\nоси 1–15, слои ⑦②", markdown)
        self.assertIn("**ГДЕ расположены элементы:**\nполе чертежа; легенда справа", markdown)
        self.assertNotIn("ИГЭ-7", markdown)
        self.assertIn(GAP_FOUNDATION, markdown)
        self.assertIn(GAP_SKV, markdown)
        self.assertIn(GAP_TABLE, markdown)
        self.assertNotIn(GAP_VIEWS, markdown)

    def test_geology_join_gap_and_roles_unchanged(self) -> None:
        legend = self._legend("LE-sand", "Песок пылеватый коричневато-серый")
        soil = self._insert(
            "SI-soil",
            status="confirmed",
            layer="Grunt",
            block_name="круг7",
            legend_entry_id=legend.id,
            signature="blockdef-v1:krug7",
        )
        exemplar = self._insert(
            "SI-ex",
            status="reference",
            role="legend_exemplar",
            layer="Grunt",
            block_name="круг7",
            legend_entry_id=legend.id,
            signature="blockdef-v1:krug7-ex",
        )
        well = self._insert(
            "SI-cpe",
            classification_reason=GEOLOGY_NO_JOIN_REASON,
        )
        binding = SymbolBinding(
            id="SB-soil",
            page=1,
            instance_id=soil.id,
            legend_entry_id=legend.id,
            status="confirmed",
            confidence=1.0,
            evidence=(
                Evidence(
                    kind="exact_block_definition",
                    score=1.0,
                    source_ids=(legend.id,),
                    detail="same block",
                ),
            ),
        )
        result = self._page(
            legends=[legend],
            instances=[soil, exemplar, well],
            bindings=[binding],
        )
        self.assertEqual("confirmed", soil.status)
        self.assertEqual("legend_exemplar", exemplar.role)
        self.assertEqual(GEOLOGY_NO_JOIN_REASON, well.classification_reason)
        with tempfile.TemporaryDirectory() as directory:
            page_dir = self._write(directory, result)
            before = json.loads(
                (page_dir / "symbol_instances.json").read_text(encoding="utf-8")
            )
            markdown = render_ideal_markdown(page_dir)
            after = json.loads(
                (page_dir / "symbol_instances.json").read_text(encoding="utf-8")
            )
        self.assertEqual(before, after)
        self.assertIn(GAP_WELLS, markdown)
        self.assertIn("*   ⑦ Песок пылеватый коричневато-серый", markdown)
        self.assertNotIn("ИГЭ-7", markdown)
        by_id = {item["id"]: item for item in after["items"]}
        self.assertEqual("confirmed", by_id["SI-soil"]["status"])
        self.assertEqual("legend_exemplar", by_id["SI-ex"]["role"])
        self.assertEqual(GEOLOGY_NO_JOIN_REASON, by_id["SI-cpe"]["classificationReason"])

    def test_circle_binding_in_ideal_legend(self) -> None:
        legend = self._legend("LE-sand", "Песок пылеватый коричневато-серый")
        other = self._legend("LE-top", "Почвенно-растительный слой solQIV")
        soil = self._insert(
            "SI-soil",
            status="confirmed",
            layer="Fill",
            block_name="круг7",
            legend_entry_id=legend.id,
            signature="blockdef-v1:krug7",
        )
        plain = self._insert(
            "SI-plain",
            status="confirmed",
            layer="WALL",
            block_name="Грунт",
            legend_entry_id=other.id,
            signature="blockdef-v1:topsoil",
        )
        bindings = [
            SymbolBinding(
                id="SB-soil",
                page=1,
                instance_id=soil.id,
                legend_entry_id=legend.id,
                status="confirmed",
                confidence=1.0,
                evidence=(
                    Evidence(
                        kind="exact_block_definition",
                        score=1.0,
                        source_ids=(legend.id,),
                        detail="same block",
                    ),
                ),
            ),
            SymbolBinding(
                id="SB-plain",
                page=1,
                instance_id=plain.id,
                legend_entry_id=other.id,
                status="confirmed",
                confidence=1.0,
                evidence=(
                    Evidence(
                        kind="exact_block_definition",
                        score=1.0,
                        source_ids=(other.id,),
                        detail="same block",
                    ),
                ),
            ),
        ]
        result = self._page(
            legends=[other, legend],
            instances=[soil, plain],
            bindings=bindings,
        )
        with tempfile.TemporaryDirectory() as directory:
            markdown = render_ideal_markdown(self._write(directory, result))
        self.assertIn("*   ⑦ Песок пылеватый коричневато-серый", markdown)
        self.assertIn("*   Почвенно-растительный слой solQIV", markdown)
        self.assertNotIn("① Почвенно-растительный слой", markdown)
        self.assertNotIn("ИГЭ-7", markdown)
        self.assertNotIn("круг7", markdown.split("### Описание:")[0])

    def test_reference_signature_joins_circle_to_legend(self) -> None:
        legend = self._legend(
            "LE-sand",
            "Песок пылеватый коричневато-серый",
            reference_signatures=("blockdef-v1:krug7-ex",),
        )
        exemplar = self._insert(
            "SI-ex",
            status="reference",
            role="legend_exemplar",
            layer="Decoration",
            block_name="круг7",
            legend_entry_id=legend.id,
            signature="blockdef-v1:krug7-ex",
        )
        result = self._page(legends=[legend], instances=[exemplar])
        with tempfile.TemporaryDirectory() as directory:
            markdown = render_ideal_markdown(self._write(directory, result))
        self.assertIn("*   ⑦ Песок пылеватый коричневато-серый", markdown)
        self.assertNotIn("ИГЭ-7", markdown)
        self.assertEqual(
            "⑦ Песок пылеватый коричневато-серый",
            legend_display_line(
                {
                    "id": legend.id,
                    "label": legend.label,
                    "referenceSignatures": ["blockdef-v1:krug7-ex"],
                },
                [exemplar.to_dict()],
                [],
            ),
        )

    def test_old_sidecar_without_scenes_or_notes_still_renders(self) -> None:
        result = self._page()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "old-sheet"
            page_dir = self._write(fixture, result)
            for name in ("notes_texts.json", "sheet_scenes.json"):
                path = page_dir / name
                if path.is_file():
                    path.unlink()
            self.assertFalse((page_dir / "sheet_scenes.json").is_file())
            self.assertFalse((page_dir / "notes_texts.json").is_file())
            markdown = render_ideal_markdown(page_dir)
            written = write_ideal_md(page_dir)
            self.assertIn("## Страница 1", markdown)
            self.assertIn(f"| Шифр | {UNREAD} |", markdown)
            self.assertIn(f"*   {UNREAD}", markdown)
            self.assertIn(GAP_NOTES, markdown)
            self.assertTrue(written.is_file())
            self.assertEqual(markdown, written.read_text(encoding="utf-8"))
            (fixture / "review.json").write_text(
                json.dumps(
                    {
                        "id": "old-sheet",
                        "documentPath": "geo.dwg",
                        "page": 1,
                        "completeness": "partial",
                        "legendEntries": 0,
                        "recognized": 0,
                        "confirmed": 0,
                        "probable": 0,
                        "unrecognizedCandidates": 0,
                        "unknownClusters": 0,
                        "unknownOccurrences": 0,
                        "sheetFurniture": 0,
                        "drawingObjects": 0,
                        "drawingAnnotations": 0,
                        "specificationMarks": 0,
                        "anomalyCodes": ["RELATIONSHIP_STAGE_NOT_RUN"],
                    }
                ),
                encoding="utf-8",
            )
            selection = root / "selection.json"
            selection.write_text(
                json.dumps(
                    {
                        "fixtures": [
                            {
                                "id": "old-sheet",
                                "documentPath": "geo.dwg",
                                "page": 1,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            report = summarize_reviews(selection, root)
            self.assertEqual(1, report["documents"])
            index = write_html_reports(selection, root)
            html = (fixture / "report.html").read_text(encoding="utf-8")
            self.assertTrue(index.is_file())
            self.assertIn("old-sheet", html)

    def test_notes_body_in_sidecar_renders_and_drops_gap(self) -> None:
        result = self._page()
        result.notes_texts = [
            NoteText(
                id="NT-survey",
                page=1,
                text="1. Изыскания выполнены ООО «МОСГЕОТЕХ».",
                source="text",
                layer="0",
                x=670.0,
                y=62.0,
                bbox=(670.0, 59.0, 820.0, 64.0),
            )
        ]
        with tempfile.TemporaryDirectory() as directory:
            page_dir = self._write(directory, result)
            markdown = render_ideal_markdown(page_dir)
        self.assertIn("### Примечание:", markdown)
        self.assertIn("1. Изыскания выполнены ООО «МОСГЕОТЕХ».", markdown)
        self.assertNotIn(GAP_NOTES, markdown)

    def test_ideal_md_links_source_drawing(self) -> None:
        result = replace(
            self._page(title_block={"code": "28-ХСА-1/25-КР4", "source": "merged"}),
            document_path="/work/new_files/dwg/4 - КР/sheet (КР4).dwg",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            drawing = root / "new_files" / "dwg" / "4 - КР" / "sheet (КР4).dwg"
            drawing.parent.mkdir(parents=True)
            drawing.write_bytes(b"")
            fixture = root / "local_runs" / "review" / "sheet-p1"
            page_dir = self._write(fixture, result)
            out = fixture / "ideal.md"
            markdown = render_ideal_markdown(page_dir, markdown_path=out)
            written = write_ideal_md(page_dir, out)
            self.assertIn(
                "Чертёж: [`sheet (КР4).dwg`](<../../../new_files/dwg/4 - КР/sheet (КР4).dwg>)",
                markdown,
            )
            self.assertEqual(markdown, written.read_text(encoding="utf-8"))

    def test_cli_writes_ideal_md(self) -> None:
        legend = self._legend("LE-soil", "Сапропель темно-серая до черного")
        result = self._page(
            legends=[legend],
            title_block={"code": "28-ХСА-1/25-КР1", "source": "merged"},
        )
        with tempfile.TemporaryDirectory() as directory:
            page_dir = self._write(directory, result)
            out = Path(directory) / "ideal.md"
            code = dwg_symbols_main(
                ["render-ideal-md", str(page_dir), "--out", str(out)]
            )
            self.assertEqual(0, code)
            text = out.read_text(encoding="utf-8")
        self.assertIn("28-ХСА-1/25-КР1", text)
        self.assertIn("Сапропель темно-серая до черного", text)


if __name__ == "__main__":
    unittest.main()
