"""Command-line entry points for audit, extraction and harness scoring."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .artifacts import write_page_result
from .audit import audit_package
from .blocks import extract_block_page
from .context_resolver import resolve_layer_context
from .file_catalog import review_file_legends
from .furniture import recount_non_symbols
from .title_block import recount_title_block_attrs
from .dimension_read import recount_dimension_attrs
from .axis_read import recount_axis_attrs
from .text_labels import dump_sheet_texts
from .field_geometry import dump_sheet_geometry
from .symbol_catalog import catalog_symbol_types
from .geometry_resolver import resolve_geometry_profiles
from .harness import load_manifest, score_manifest
from .html_report import write_html_reports
from .legends import extract_legend_page, write_legend_crops
from .project_catalog import review_project_legends
from .project_application import apply_project_catalog_to_page_dir, apply_project_legend
from .review import run_sheet_review, summarize_reviews
from .resolver import resolve_exact_blocks
from .ideal_md import write_ideal_md


def _write_or_print(payload: Any, destination: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if destination:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(text, encoding="utf-8", newline="\n")
        temporary.replace(path)
    else:
        print(text, end="")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dwg_symbols",
        description="Fail-closed DWG symbol inventory and evaluation harness.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    audit = commands.add_parser("audit", help="inventory a DWG/DXF package")
    audit.add_argument("root")
    audit.add_argument("--deep", action="store_true", help="convert and inspect files")
    audit.add_argument("--out")

    extract = commands.add_parser(
        "extract-blocks", help="extract unresolved INSERT candidates for one sheet"
    )
    extract.add_argument("drawing")
    extract.add_argument("--page", type=int, required=True)
    extract.add_argument("--out", required=True)

    legends = commands.add_parser(
        "extract-legends",
        help="extract H2 block candidates and H3 vector legend rows for one sheet",
    )
    legends.add_argument("drawing")
    legends.add_argument("--page", type=int, required=True)
    legends.add_argument("--out", required=True)

    resolve = commands.add_parser(
        "resolve-exact",
        help="run H2/H3 and confirm exact block-definition legend matches",
    )
    resolve.add_argument("drawing")
    resolve.add_argument("--page", type=int, required=True)
    resolve.add_argument("--out", required=True)

    geometry = commands.add_parser(
        "resolve-geometry",
        help="run H4 exact blocks plus probable closed geometry profiles",
    )
    geometry.add_argument("drawing")
    geometry.add_argument("--page", type=int, required=True)
    geometry.add_argument("--out", required=True)

    context = commands.add_parser(
        "resolve-context",
        help="run H4 block, geometry and conservative layer/label context",
    )
    context.add_argument("drawing")
    context.add_argument("--page", type=int, required=True)
    context.add_argument("--out", required=True)

    review = commands.add_parser(
        "review-sheet",
        help="run H2-H4c and write page overlays, crops and review statistics",
    )
    review.add_argument("drawing")
    review.add_argument("--page", type=int, required=True)
    review.add_argument("--id", required=True)
    review.add_argument("--out", required=True)

    review_file = commands.add_parser(
        "review-file",
        help="run H2-H4c on every sheet and build one file-level legend catalog",
    )
    review_file.add_argument("drawing")
    review_file.add_argument("--id", required=True)
    review_file.add_argument("--out", required=True)

    review_project = commands.add_parser(
        "review-project",
        help="scan every DWG/DXF and build one project-wide legend catalog",
    )
    review_project.add_argument("root")
    review_project.add_argument("--out", required=True)
    review_project.add_argument(
        "--rebuild",
        action="store_true",
        help="ignore resumable per-document scan artifacts",
    )

    apply_project = commands.add_parser(
        "apply-project-legend",
        help="apply one project catalog to every readable DWG/DXF sheet",
    )
    apply_project.add_argument("root")
    apply_project.add_argument("catalog")
    apply_project.add_argument("--out", required=True)
    apply_project.add_argument(
        "--rebuild",
        action="store_true",
        help="ignore resumable per-document application artifacts",
    )

    apply_page = commands.add_parser(
        "apply-project-legend-page",
        help="apply the project catalog to one existing page sidecar without converting DWG",
    )
    apply_page.add_argument("page_dir")
    apply_page.add_argument("catalog")

    summarize = commands.add_parser(
        "summarize-review",
        help="aggregate completed review-sheet outputs",
    )
    summarize.add_argument("selection")
    summarize.add_argument("root")

    html_report = commands.add_parser(
        "render-review-html",
        help="write one image-rich HTML report per reviewed DWG sheet",
    )
    html_report.add_argument("selection")
    html_report.add_argument("root")

    ideal_md = commands.add_parser(
        "render-ideal-md",
        help="render IDEAL-frame markdown from an existing page sidecar without converting DWG",
    )
    ideal_md.add_argument("page_dir")
    ideal_md.add_argument("--out")

    recount = commands.add_parser(
        "recount-non-symbols",
        help="reclassify INSERT roles from existing sidecars without converting DWG",
    )
    recount.add_argument("root")
    recount.add_argument("--out")

    title_block = commands.add_parser(
        "recount-title-block",
        help="count title-block grafa from sidecar INSERT attributes without converting DWG",
    )
    title_block.add_argument("root")
    title_block.add_argument("--out")

    dimensions = commands.add_parser(
        "recount-dimensions",
        help="count dimension/elevation numbers from sidecar INSERT attributes",
    )
    dimensions.add_argument("root")
    dimensions.add_argument("--out")

    axes = commands.add_parser(
        "recount-axes",
        help="count axis letter/digit from sidecar INSERT attributes",
    )
    axes.add_argument("root")
    axes.add_argument("--out")

    dump_texts = commands.add_parser(
        "dump-sheet-texts",
        help="inventory sheet TEXT and classify axis/linear labels without HTML",
    )
    dump_texts.add_argument("drawing")
    dump_texts.add_argument("--page", type=int, required=True)
    dump_texts.add_argument("--out")

    dump_geometry = commands.add_parser(
        "dump-sheet-geometry",
        help="inventory field LINE/POLYLINE and same-sheet legend labels without HTML",
    )
    dump_geometry.add_argument("drawing")
    dump_geometry.add_argument("--page", type=int, required=True)
    dump_geometry.add_argument("--out")

    catalog = commands.add_parser(
        "catalog-symbol-types",
        help="unique block signatures, application counts and draft taxonomy shelves",
    )
    catalog.add_argument("root")
    catalog.add_argument("--out")

    validate = commands.add_parser("validate-gt", help="validate harness manifest")
    validate.add_argument("manifest")

    score = commands.add_parser("score", help="score sidecars against ground truth")
    score.add_argument("manifest")
    score.add_argument("predictions")
    score.add_argument("--out")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "audit":
        _write_or_print(audit_package(args.root, deep=args.deep), args.out)
        return 0
    if args.command == "extract-blocks":
        result = extract_block_page(args.drawing, args.page)
        page_dir = write_page_result(args.out, result)
        print(page_dir)
        return 0
    if args.command == "extract-legends":
        extraction = extract_legend_page(args.drawing, args.page)
        page_dir = write_page_result(args.out, extraction.page_result)
        write_legend_crops(page_dir, extraction.crops)
        print(page_dir)
        return 0
    if args.command == "resolve-exact":
        extraction = extract_legend_page(args.drawing, args.page)
        result = resolve_exact_blocks(extraction.page_result)
        page_dir = write_page_result(args.out, result)
        write_legend_crops(page_dir, extraction.crops)
        print(page_dir)
        return 0
    if args.command == "resolve-geometry":
        extraction = extract_legend_page(args.drawing, args.page)
        result = resolve_exact_blocks(extraction.page_result)
        result = resolve_geometry_profiles(result, extraction.primitives)
        page_dir = write_page_result(args.out, result)
        write_legend_crops(page_dir, extraction.crops)
        print(page_dir)
        return 0
    if args.command == "resolve-context":
        extraction = extract_legend_page(args.drawing, args.page)
        result = resolve_exact_blocks(extraction.page_result)
        result = resolve_geometry_profiles(result, extraction.primitives)
        result = resolve_layer_context(result, extraction.primitives)
        page_dir = write_page_result(args.out, result)
        write_legend_crops(page_dir, extraction.crops)
        print(page_dir)
        return 0
    if args.command == "review-sheet":
        review = run_sheet_review(args.drawing, args.page, args.out, args.id)
        _write_or_print(review, None)
        return 0
    if args.command == "review-file":
        catalog = review_file_legends(args.drawing, args.out, args.id)
        counts = catalog["counts"]
        print(
            f"OK: {catalog['pageCount']} sheet(s), "
            f"{counts['uniqueLegendEntries']} unique legend entries, "
            f"{counts['crossSheetCandidates']} cross-sheet candidate(s)"
        )
        return 0
    if args.command == "review-project":
        catalog = review_project_legends(
            args.root,
            args.out,
            rebuild=args.rebuild,
            progress=print,
        )
        counts = catalog["counts"]
        print(
            f"OK: {counts['documents']} document(s), "
            f"{counts['uniqueProjectEntries']} unique project legend entries, "
            f"{counts['conflicts']} conflict(s)"
        )
        return 0
    if args.command == "apply-project-legend":
        summary = apply_project_legend(
            args.root,
            args.catalog,
            args.out,
            rebuild=args.rebuild,
            progress=print,
        )
        counts = summary["counts"]
        print(
            f"OK: {counts['processedPages']} page(s), "
            f"{counts['projectProbable']} project probable match(es), "
            f"{counts['unresolvedCandidates']} unresolved candidate(s)"
        )
        return 0
    if args.command == "apply-project-legend-page":
        result = apply_project_catalog_to_page_dir(args.page_dir, args.catalog)
        probable = sum(
            1 for item in result.symbol_instances if item.status == "probable"
        )
        print(
            f"OK: {len(result.legend_entries)} legend entries, "
            f"{probable} project probable instance(s)"
        )
        return 0
    if args.command == "summarize-review":
        report = summarize_reviews(args.selection, args.root)
        print(
            f"OK: {report['documents']} sheet(s), "
            f"{report['totals']['recognized']} recognized"
        )
        return 0
    if args.command == "render-review-html":
        print(write_html_reports(args.selection, args.root))
        return 0
    if args.command == "render-ideal-md":
        path = write_ideal_md(args.page_dir, args.out)
        print(path)
        return 0
    if args.command == "recount-non-symbols":
        summary = recount_non_symbols(args.root)
        _write_or_print(summary, args.out)
        if args.out:
            print(
                f"OK: {summary['pages']} page(s), "
                f"unknown {summary['unknownBefore']} → {summary['unknownAfter']}"
            )
        return 0
    if args.command == "recount-title-block":
        summary = recount_title_block_attrs(args.root)
        _write_or_print(summary, args.out)
        if args.out:
            print(
                f"OK: {summary['pages']} page(s), "
                f"title-block inserts {summary['withTitleBlockInsert']}, "
                f"empty ШИФР {summary['cipher']['empty']}"
            )
        return 0
    if args.command == "recount-dimensions":
        summary = recount_dimension_attrs(args.root)
        _write_or_print(summary, args.out)
        if args.out:
            print(
                f"OK: {summary['pages']} page(s), "
                f"eligible {summary['eligible']}, "
                f"filled {summary['filledFromAttributes']}"
            )
        return 0
    if args.command == "recount-axes":
        summary = recount_axis_attrs(args.root)
        _write_or_print(summary, args.out)
        if args.out:
            print(
                f"OK: {summary['pages']} page(s), "
                f"eligible {summary['eligible']}, "
                f"both {summary['withBoth']}"
            )
        return 0
    if args.command == "dump-sheet-texts":
        payload = dump_sheet_texts(args.drawing, args.page)
        _write_or_print(payload, args.out)
        if args.out:
            print(
                f"OK: texts {payload['texts']}, "
                f"labels {len(payload['labels'])}"
            )
        return 0
    if args.command == "dump-sheet-geometry":
        payload = dump_sheet_geometry(args.drawing, args.page)
        _write_or_print(payload, args.out)
        if args.out:
            print(
                f"OK: primitives {payload['primitives']}, "
                f"hatches {payload.get('hatches', 0)}, "
                f"field {len(payload['fieldGeometry'])}, "
                f"labeled {payload['labeled']}"
            )
        return 0
    if args.command == "catalog-symbol-types":
        catalog_payload = catalog_symbol_types(args.root)
        _write_or_print(catalog_payload, args.out)
        if args.out:
            print(
                f"OK: {catalog_payload['pages']} page(s), "
                f"{catalog_payload['uniqueTypes']} unique type(s), "
                f"{catalog_payload['instances']} instance(s)"
            )
        return 0
    if args.command == "validate-gt":
        manifest = load_manifest(args.manifest)
        print(f"OK: {len(manifest['fixtures'])} fixture(s)")
        return 0
    if args.command == "score":
        manifest = load_manifest(args.manifest)
        _write_or_print(score_manifest(manifest, args.predictions), args.out)
        return 0
    return 2
