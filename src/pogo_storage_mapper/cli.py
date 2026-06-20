from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from pogo_storage_mapper.classify import (
    run_inventory_classification,
    run_inventory_validation,
)
from pogo_storage_mapper.doctor import collect_doctor_report, render_doctor_report
from pogo_storage_mapper.export import run_production_export
from pogo_storage_mapper.metadata_sync import sync_metadata_catalog
from pogo_storage_mapper.scan_frames import ScanSettings, run_frame_scan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pogo-storage-mapper",
        description="Offline Pokemon GO frame scanning and audit artifact generation.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor", help="Check required local tools.")

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate a CSV/XLSX Pokemon inventory before classification.",
    )
    validate_parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Input Pokemon inventory CSV or XLSX.",
    )

    classify_parser = subparsers.add_parser(
        "classify",
        help="Classify an exported Pokemon inventory into KEEP/REVIEW/LET-GO rows.",
    )
    classify_parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Input Pokemon inventory CSV or XLSX.",
    )
    classify_parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Output directory for classified CSV/XLSX files and artifacts.",
    )

    sync_parser = subparsers.add_parser(
        "sync-metadata",
        help="Developer-only network sync for the packaged offline metadata catalog.",
    )
    sync_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=(
            "Catalog output path. Defaults to the packaged "
            "pogo_storage_mapper/data/metadata_catalog.json file."
        ),
    )

    scan_parser = subparsers.add_parser(
        "scan-frames",
        help="Extract/copy frames, classify them, and write manual audit artifacts.",
    )
    scan_parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Input MP4, image file/folder, or frames.jsonl.",
    )
    scan_parser.add_argument(
        "--output", required=True, type=Path, help="Output directory."
    )
    scan_parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=None,
        help="Optional artifact directory. Defaults to <output>/artifacts.",
    )
    scan_parser.add_argument(
        "--ocr-lang", default="eng", help="Tesseract language pack."
    )
    scan_parser.add_argument(
        "--ocr-mode",
        choices=("balanced", "full", "off"),
        default="balanced",
        help=(
            "OCR coverage. Balanced reads feature-critical regions; "
            "full reads every region."
        ),
    )
    scan_parser.add_argument(
        "--workers",
        default="auto",
        help="Frame analysis worker count or 'auto'. Auto uses all logical CPUs.",
    )
    scan_parser.add_argument(
        "--max-frame-attempts",
        type=int,
        default=3,
        help="Maximum attempts for a failed frame analysis task.",
    )
    scan_parser.add_argument(
        "--visible-crop",
        action="store_true",
        help=(
            "Write red rectangle debug overlays for visual and OCR crop regions "
            "beside each analyzed frame."
        ),
    )

    export_parser = subparsers.add_parser(
        "export",
        help="Export source-local Pokemon rows to CSV and XLSX.",
    )
    export_parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="Input MP4, image file/folder, or frames.jsonl.",
    )
    export_parser.add_argument(
        "--output", required=True, type=Path, help="Output directory."
    )
    export_parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=None,
        help="Optional artifact directory. Defaults to <output>/artifacts.",
    )
    export_parser.add_argument(
        "--ocr-lang", default="eng", help="Tesseract language pack."
    )
    export_parser.add_argument(
        "--ocr-mode",
        choices=("balanced", "full", "off"),
        default="balanced",
        help=(
            "OCR coverage for production sequence scans. Balanced keeps "
            "selective OCR enabled; off skips OCR-backed export evidence."
        ),
    )
    export_parser.add_argument(
        "--workers",
        default="auto",
        help=(
            "Visual frame analysis worker count or 'auto'. Auto uses all logical CPUs."
        ),
    )
    export_parser.add_argument(
        "--max-frame-attempts",
        type=int,
        default=3,
        help="Maximum attempts for a failed visual frame analysis task.",
    )
    export_parser.add_argument(
        "--max-export-frame-files",
        type=int,
        default=0,
        help=(
            "Soft limit for temporary export frame image files. "
            "Use 0 or a negative value for unlimited extraction."
        ),
    )
    export_parser.add_argument(
        "--visible-crop",
        action="store_true",
        help=(
            "Write red rectangle debug overlays for visual and OCR crop regions "
            "beside each analyzed frame."
        ),
    )

    return parser


def parse_workers(raw_workers: str | int | None) -> int | None:
    if raw_workers is None:
        return None
    if isinstance(raw_workers, int):
        if raw_workers < 1:
            msg = "Worker count must be at least 1."
            raise ValueError(msg)
        return raw_workers

    normalized = raw_workers.strip().casefold()
    if normalized == "auto":
        return None
    if not normalized.isdigit():
        msg = "Workers must be 'auto' or a positive integer."
        raise ValueError(msg)

    workers = int(normalized)
    if workers < 1:
        msg = "Worker count must be at least 1."
        raise ValueError(msg)
    return workers


def _scan_settings(args: argparse.Namespace) -> ScanSettings:
    if args.max_frame_attempts < 1:
        msg = "Maximum frame attempts must be at least 1."
        raise ValueError(msg)
    return ScanSettings(
        input_path=args.input,
        output_dir=args.output,
        artifacts_dir=args.artifacts_dir,
        ocr_lang=args.ocr_lang,
        ocr_mode=args.ocr_mode,
        workers=parse_workers(args.workers),
        max_frame_attempts=args.max_frame_attempts,
        visible_crop=getattr(args, "visible_crop", False),
        max_export_frame_files=getattr(args, "max_export_frame_files", 0),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "doctor":
            print(render_doctor_report(collect_doctor_report()))
            return 0

        if args.command == "validate":
            validation_report = run_inventory_validation(args.input)
            print(validation_report.summary_line())
            return 1 if validation_report.errors else 0

        if args.command == "classify":
            classify_report = run_inventory_classification(args.input, args.output)
            print(classify_report.summary_line())
            return 0

        if args.command == "sync-metadata":
            print(sync_metadata_catalog(args.output).summary_line())
            return 0

        if args.command == "scan-frames":
            scan_report = run_frame_scan(_scan_settings(args))
            print(scan_report.summary_line())
            return 0 if not scan_report.failed_files else 1

        if args.command == "export":
            export_report = run_production_export(_scan_settings(args))
            print(export_report.summary_line())
            return 0 if not export_report.failed_files else 1
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"Unsupported command: {args.command}")
    return 2
