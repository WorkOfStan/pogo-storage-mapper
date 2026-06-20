from __future__ import annotations

import csv
import json
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, TypeAlias, cast

from openpyxl import Workbook  # pylint: disable=import-error

from pogo_storage_mapper.extract import (
    IV_NUMERIC_FIELD_NAMES,
    PokemonFragment,
    enrich_fragments_with_moves,
    enrich_fragments_with_species,
    extract_fragments,
)
from pogo_storage_mapper.metadata import MetadataCatalog, load_default_metadata_catalog
from pogo_storage_mapper.ocr import TesseractOcrEngine
from pogo_storage_mapper.runtime_support import (
    PhaseTimer,
    build_image_source_payload,
    build_input_manifest_payload,
    build_video_source_payload,
    execute_with_adaptive_retries,
)
from pogo_storage_mapper.scan_frames import (
    NON_EXTRACTABLE_CLASS,
    FrameCandidate,
    FrameScanRecord,
    FrameVisualRecord,
    ProductionSequenceScanResult,
    ScanSettings,
    SourceAsset,
    VideoExtractionResult,
    _dominant_sequence_weight,
    copy_image_frame,
    discover_inputs,
    extract_video_frames,
    group_production_sequences,
    load_jsonl_frame_candidates,
    probe_video_duration,
    refresh_production_sequence_result,
    scan_frame_visual_candidate,
    scan_production_sequence,
    scan_production_sequence_repair,
    select_cp_consensus_value,
)

ExportValue: TypeAlias = str | int | float | bool | None
IdentityKey: TypeAlias = tuple[str, ...]
VisualScanner: TypeAlias = Callable[[FrameCandidate], FrameVisualRecord]
SequenceScanner: TypeAlias = Callable[
    [list[FrameVisualRecord], ScanSettings],
    ProductionSequenceScanResult,
]
SequenceProgressCallback: TypeAlias = Callable[[FrameCandidate, tuple[str, ...]], None]

EXPORT_COLUMNS = (
    "source_file",
    "source_type",
    "first_frame_index",
    "last_frame_index",
    "first_timestamp_s",
    "last_timestamp_s",
    "display_name",
    "species_key",
    "species_name",
    "pokedex_id",
    "canonical_name",
    "catch_date",
    "catch_location",
    "catch_country",
    "cp",
    "hp_current",
    "hp_max",
    "weight_kg",
    "height_m",
    "iv_complete",
    *IV_NUMERIC_FIELD_NAMES,
    "appraisal_perfect",
    "fast_move_key",
    "fast_move_name",
    "charged_move_key",
    "charged_move_name",
    "second_charged_move_key",
    "second_charged_move_name",
    "max_move_key",
    "max_move_name",
    "is_shadow",
    "has_dynamax",
    "has_gigantamax",
    "has_tag_chips",
)

_SOURCE_COLUMNS = {
    "source_file",
    "source_type",
    "first_frame_index",
    "last_frame_index",
    "first_timestamp_s",
    "last_timestamp_s",
}
_FLAG_COLUMNS = {
    "is_shadow",
    "has_dynamax",
    "has_gigantamax",
    "has_tag_chips",
}
_TRUE_IF_ANY_COLUMNS = {"iv_complete", "appraisal_perfect", *_FLAG_COLUMNS}
_WEAK_CONFLICT_COLUMNS = {
    "display_name",
    "height_m",
    "appraisal_star_count",
    *_TRUE_IF_ANY_COLUMNS,
}
_SOFT_CONFLICT_COLUMNS = {"cp"}
_CANONICAL_IDENTITY_COLUMNS = {
    "species_key",
    "species_name",
    "pokedex_id",
    "canonical_name",
}
_CRITICAL_CONFLICT_COLUMNS = (
    set(EXPORT_COLUMNS)
    - _SOURCE_COLUMNS
    - _WEAK_CONFLICT_COLUMNS
    - _SOFT_CONFLICT_COLUMNS
)
_FIELD_TO_COLUMN = {
    "display_name_text": "display_name",
    "species_key": "species_key",
    "species_name": "species_name",
    "pokedex_id": "pokedex_id",
    "canonical_name_text": "canonical_name",
    "catch_date_text": "catch_date",
    "location_text": "catch_location",
    "catch_country_text": "catch_country",
    "cp": "cp",
    "hp_current": "hp_current",
    "hp_max": "hp_max",
    "weight_kg": "weight_kg",
    "height_m": "height_m",
    "iv_complete": "iv_complete",
    "iv_attack": "iv_attack",
    "iv_defense": "iv_defense",
    "iv_stamina": "iv_stamina",
    "iv_sum": "iv_sum",
    "appraisal_star_count": "appraisal_star_count",
    "appraisal_perfect": "appraisal_perfect",
    "fast_move_key": "fast_move_key",
    "fast_move_name": "fast_move_name",
    "charged_move_key": "charged_move_key",
    "charged_move_name": "charged_move_name",
    "second_charged_move_key": "second_charged_move_key",
    "second_charged_move_name": "second_charged_move_name",
    "max_move_key": "max_move_key",
    "max_move_name": "max_move_name",
    "is_shadow": "is_shadow",
    "has_dynamax": "has_dynamax",
    "has_gigantamax": "has_gigantamax",
    "has_tag_chips": "has_tag_chips",
}
_INTERNAL_ANCHOR_FIELDS = {"iv_star_agreement"}
_APPRAISAL_ANCHOR_COLUMNS = {
    "hp_current",
    "hp_max",
    "weight_kg",
    "species_key",
    "catch_date",
    "catch_location",
    "iv_attack",
    "iv_defense",
    "iv_stamina",
}
_IV_TRIPLET_COLUMNS = ("iv_attack", "iv_defense", "iv_stamina")
_COMPLETE_IV_LOCK_COLUMNS = (
    "iv_complete",
    *_IV_TRIPLET_COLUMNS,
    "iv_sum",
    "appraisal_star_count",
    "appraisal_perfect",
)
_DETAIL_ANCHOR_COLUMN_GROUPS = (
    ("fast_move_key", "charged_move_key"),
    ("max_move_key",),
)
_NORMAL_MOVE_COLUMNS = (
    "fast_move_key",
    "fast_move_name",
    "charged_move_key",
    "charged_move_name",
)
_APPRAISAL_TEXT_CONSENSUS_COLUMNS = {"catch_location"}
_CROSS_SEQUENCE_WEIGHT_CORRECTED_NOTE = (
    "Weight was corrected from adjacent source-local same-HP production sequences."
)
_CONSENSUS_MIN_COUNT = 3
_CONSENSUS_MIN_RATIO = 2


@dataclass(frozen=True, slots=True)
class ExportWarning:
    kind: str
    message: str
    source_file: str = ""
    first_frame_index: int | None = None
    last_frame_index: int | None = None

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "kind": self.kind,
            "message": self.message,
        }
        if self.source_file:
            payload["source_file"] = self.source_file
        if self.first_frame_index is not None:
            payload["first_frame_index"] = self.first_frame_index
        if self.last_frame_index is not None:
            payload["last_frame_index"] = self.last_frame_index
        return payload


@dataclass(frozen=True, slots=True)
class FrameLifecycleEvent:
    action: str
    reason: str
    source_file: str
    frame_index: int
    frame_name: str
    phase: str = ""

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "action": self.action,
            "reason": self.reason,
            "source_file": self.source_file,
            "frame_index": self.frame_index,
            "frame_name": self.frame_name,
        }
        if self.phase:
            payload["phase"] = self.phase
        return payload


@dataclass(slots=True)
class ExportAssemblyResult:
    rows: list[dict[str, ExportValue]]
    warnings: list[ExportWarning] = field(default_factory=list)
    rejected_sequence_count: int = 0
    row_diagnostics: list[dict[str, object]] = field(default_factory=list)
    unresolved_pokemon_like_sequence_count: int = 0


@dataclass(slots=True)
class ExportReport:
    rows: list[dict[str, ExportValue]] = field(default_factory=list)
    processed_files: list[Path] = field(default_factory=list)
    failed_files: list[Path] = field(default_factory=list)
    warnings: list[ExportWarning] = field(default_factory=list)
    worker_count: int = 1
    retry_count: int = 0
    sequence_worker_count: int = 1
    sequence_retry_count: int = 0
    repair_worker_count: int = 1
    repair_retry_count: int = 0
    repaired_sequence_count: int = 0
    frame_count: int = 0
    visual_frame_count: int = 0
    scanned_frame_count: int = 0
    sequence_count: int = 0
    max_export_frame_files: int = 0
    bounded_extraction_enabled: bool = False
    peak_export_frame_files: int = 0
    deleted_list_or_non_extractable_frames: int = 0
    deleted_sequence_frames: int = 0
    deleted_unsequenced_visual_frames: int = 0
    retained_frame_count: int = 0
    frame_lifecycle_events: list[FrameLifecycleEvent] = field(default_factory=list)
    row_diagnostics: list[dict[str, object]] = field(default_factory=list)
    unresolved_pokemon_like_sequence_count: int = 0
    bounded_extraction_soft_limit_exceeded: bool = False
    timing_summary: dict[str, object] = field(default_factory=dict)
    worker_events: list[dict[str, object]] = field(default_factory=list)
    bounded_chunk_events: list[dict[str, object]] = field(default_factory=list)
    scan_operation_summary: dict[str, object] = field(default_factory=dict)

    def summary_line(self) -> str:
        return (
            f"Exported {len(self.rows)} Pokemon row(s) from "
            f"{len(self.processed_files)} source file(s), "
            f"skipped {self.sequence_count - len(self.rows)} sequence(s), "
            f"failed {len(self.failed_files)} source file(s)."
        )


@dataclass(slots=True)
class _FrameCollectionResult:
    frames: list[FrameCandidate]
    processed_files: list[Path]
    failed_files: list[Path]
    warnings: list[ExportWarning]
    source_payloads: dict[str, object]


@dataclass(slots=True)
class _VisualProcessingResult:
    records: list[FrameVisualRecord]
    warnings: list[ExportWarning]
    worker_count: int
    retry_count: int
    worker_events: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class _SequenceProcessingResult:
    records: list[ProductionSequenceScanResult]
    warnings: list[ExportWarning]
    worker_count: int
    retry_count: int
    worker_events: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class _SequenceRepairResult:
    records: list[ProductionSequenceScanResult]
    warnings: list[ExportWarning]
    repaired_count: int = 0
    worker_count: int = 1
    retry_count: int = 0
    worker_events: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class _BoundedFrameFileStats:
    max_export_frame_files: int = 0
    bounded_extraction_enabled: bool = False
    peak_export_frame_files: int = 0
    deleted_list_or_non_extractable_frames: int = 0
    deleted_sequence_frames: int = 0
    deleted_unsequenced_visual_frames: int = 0
    retained_frame_count: int = 0
    frame_lifecycle_events: list[FrameLifecycleEvent] = field(default_factory=list)
    bounded_extraction_soft_limit_exceeded: bool = False


@dataclass(slots=True)
class _BoundedProcessingResult:
    scan_results: list[tuple[int, int, ProductionSequenceScanResult]]
    processed_files: list[Path]
    failed_files: list[Path]
    warnings: list[ExportWarning]
    source_payloads: dict[str, object]
    frame_count: int = 0
    visual_frame_count: int = 0
    sequence_count: int = 0
    worker_count: int = 1
    retry_count: int = 0
    sequence_worker_count: int = 1
    sequence_retry_count: int = 0
    repair_worker_count: int = 1
    repair_retry_count: int = 0
    repaired_sequence_count: int = 0
    bounded_frame_file_stats: _BoundedFrameFileStats = field(
        default_factory=_BoundedFrameFileStats
    )
    worker_events: list[dict[str, object]] = field(default_factory=list)
    bounded_chunk_events: list[dict[str, object]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _VideoFrameTimeline:
    timestamps_s: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class _VideoFrameTimelineProbe:
    status: str
    frame_count: int = 0
    reason: str = ""
    timeline: _VideoFrameTimeline | None = None

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "method": "ffprobe_show_frames",
            "status": self.status,
            "frame_count": self.frame_count,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True, slots=True)
class _TimeSeekWindow:
    seek_start_s: float
    seek_duration_s: float
    select_start_s: float
    select_end_s: float


@dataclass(slots=True)
class _BoundedChunkExtractionResult:
    extraction: VideoExtractionResult
    method: str
    fallback_reason: str = ""
    seek_start_s: float | None = None
    seek_duration_s: float | None = None


@dataclass(slots=True)
class _SequenceLifecycleScan:
    result: ProductionSequenceScanResult
    warnings: list[ExportWarning]
    repaired_count: int = 0
    repair_worker_count: int = 1
    repair_retry_count: int = 0
    worker_events: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class _SequenceLifecycleResult:
    records: list[tuple[int, int, ProductionSequenceScanResult]]
    warnings: list[ExportWarning]
    worker_count: int = 1
    retry_count: int = 0
    repair_worker_count: int = 1
    repair_retry_count: int = 0
    repaired_count: int = 0
    worker_events: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class _RowCandidate:
    row: dict[str, ExportValue]
    identity_key: IdentityKey | None
    warnings: list[ExportWarning] = field(default_factory=list)
    accepted: bool = False
    anchor_kind: str = "support"
    scan_start_frame_index: int = 0
    fragment_types: set[str] = field(default_factory=set)
    column_value_counts: dict[str, Counter[ExportValue]] = field(default_factory=dict)
    source_detail_frame_indexes: tuple[int, ...] = ()
    source_appraisal_frame_indexes: tuple[int, ...] = ()


@dataclass(slots=True)
class _ExportLiveLogger:
    path: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def reset(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "timestamp\tworker_id\tphase\tframe_name\tsource_name\tfields\n",
            encoding="utf-8",
        )

    def log_frame(
        self,
        phase: str,
        frame: FrameCandidate,
        fields: Iterable[str] = (),
    ) -> None:
        fields_text = ",".join(sorted(fields))
        line = (
            f"{datetime.now().astimezone().isoformat(timespec='milliseconds')}\t"
            f"{threading.get_ident()}\t"
            f"{phase}\t"
            f"{frame.frame_path.name}\t"
            f"{frame.source_asset.path}\t"
            f"{fields_text}\n"
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    def dump(self, message: str) -> None:
        line = (
            f"{datetime.now().astimezone().isoformat(timespec='milliseconds')}\t"
            f"{threading.get_ident()}\t"
            f"dump\t\t\t"
            f"{message}\n"
        )
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()

    def log_worker_event(self, event: dict[str, object]) -> None:
        details = " ".join(f"{key}={value}" for key, value in sorted(event.items()))
        self.dump(f"worker {details}")


def _record_worker_event(
    events: list[dict[str, object]],
    phase: str,
    event: dict[str, object],
    live_logger: _ExportLiveLogger | None,
) -> None:
    payload = {"phase": phase, **event}
    events.append(payload)
    if live_logger is not None and event.get("event") != "batch_start":
        live_logger.log_worker_event(payload)


def run_production_export(
    settings: ScanSettings,
    *,
    visual_scanner: VisualScanner = scan_frame_visual_candidate,
    sequence_scanner: SequenceScanner = scan_production_sequence,
    catalog: MetadataCatalog | None = None,
) -> ExportReport:
    started = time.perf_counter()
    phase_timer = PhaseTimer()

    artifacts_dir = export_artifacts_dir(settings)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    live_log = _ExportLiveLogger(settings.output_dir / "export.log")
    live_log.reset()

    if _bounded_export_enabled(settings):
        return _run_bounded_production_export(
            settings,
            artifacts_dir,
            started=started,
            phase_timer=phase_timer,
            visual_scanner=visual_scanner,
            sequence_scanner=sequence_scanner,
            catalog=catalog,
            live_log=live_log,
        )

    collected = phase_timer.run(
        "input_collection",
        lambda: _collect_frame_candidates(settings, artifacts_dir),
    )
    report = ExportReport(
        processed_files=collected.processed_files,
        failed_files=collected.failed_files,
        warnings=list(collected.warnings),
        frame_count=len(collected.frames),
        max_export_frame_files=settings.max_export_frame_files,
    )
    if not TesseractOcrEngine(lang=settings.ocr_lang).is_available():
        report.warnings.append(
            ExportWarning(
                "environment",
                "Tesseract is unavailable; OCR-backed export fields may be blank.",
            )
        )

    visual_processing = phase_timer.run(
        "visual_analysis",
        lambda: _process_visual_frames_with_retry(
            collected.frames,
            settings,
            visual_scanner=visual_scanner,
            live_logger=live_log,
        ),
    )
    report.visual_frame_count = len(visual_processing.records)
    report.worker_count = visual_processing.worker_count
    report.retry_count = visual_processing.retry_count
    report.worker_events.extend(visual_processing.worker_events)
    report.warnings.extend(visual_processing.warnings)

    sequences = phase_timer.run(
        "sequence_grouping",
        lambda: group_production_sequences(visual_processing.records),
    )
    report.sequence_count = len(sequences)

    sequence_processing = phase_timer.run(
        "sequence_scanning",
        lambda: _scan_production_sequences(
            sequences,
            settings,
            sequence_scanner,
            progress_callback=lambda frame, fields: live_log.log_frame(
                "sequence", frame, fields
            ),
            live_logger=live_log,
        ),
    )
    report.sequence_worker_count = sequence_processing.worker_count
    report.sequence_retry_count = sequence_processing.retry_count
    report.worker_events.extend(sequence_processing.worker_events)
    report.warnings.extend(sequence_processing.warnings)
    scan_results = sequence_processing.records
    if sequence_scanner is scan_production_sequence:
        repair_processing = phase_timer.run(
            "sequence_repair",
            lambda: _repair_production_sequences(
                sequences,
                scan_results,
                settings,
                progress_callback=lambda frame, fields: live_log.log_frame(
                    "repair", frame, fields
                ),
                live_logger=live_log,
            ),
        )
        scan_results = repair_processing.records
        report.repair_worker_count = repair_processing.worker_count
        report.repair_retry_count = repair_processing.retry_count
        report.repaired_sequence_count = repair_processing.repaired_count
        report.worker_events.extend(repair_processing.worker_events)
        report.warnings.extend(repair_processing.warnings)
    return _complete_production_export(
        settings,
        artifacts_dir,
        report,
        scan_results,
        source_payloads=collected.source_payloads,
        started=started,
        phase_timer=phase_timer,
        sequence_scanner=sequence_scanner,
        catalog=catalog,
        live_log=live_log,
    )


def _complete_production_export(
    settings: ScanSettings,
    artifacts_dir: Path,
    report: ExportReport,
    scan_results: list[ProductionSequenceScanResult],
    *,
    source_payloads: dict[str, object],
    started: float,
    phase_timer: PhaseTimer,
    sequence_scanner: SequenceScanner,
    catalog: MetadataCatalog | None,
    live_log: _ExportLiveLogger,
) -> ExportReport:
    if sequence_scanner is scan_production_sequence:
        weight_warnings = phase_timer.run(
            "cross_sequence_stabilization",
            lambda: _stabilize_same_hp_sequence_weights(scan_results),
        )
        report.warnings.extend(weight_warnings)
    for result in scan_results:
        report.warnings.extend(
            ExportWarning("production_scan", warning) for warning in result.warnings
        )
    report.scanned_frame_count = sum(len(result.records) for result in scan_results)
    if report.bounded_extraction_enabled:
        _log_bounded_complete_iv_extraction(live_log, scan_results)
    report.scan_operation_summary = _scan_operation_summary(
        scan_results, report.frame_lifecycle_events
    )

    export_catalog = catalog or load_default_metadata_catalog()
    fragment_sequences = [
        _enriched_fragments(result.records, export_catalog) for result in scan_results
    ]
    assembly = phase_timer.run(
        "row_assembly",
        lambda: assemble_export_rows(fragment_sequences),
    )
    report.rows = assembly.rows
    report.warnings.extend(assembly.warnings)
    report.row_diagnostics = assembly.row_diagnostics
    report.unresolved_pokemon_like_sequence_count = (
        assembly.unresolved_pokemon_like_sequence_count
    )
    if report.bounded_extraction_enabled:
        _log_bounded_row_iv_summary(live_log, report.rows)

    _write_export_artifacts(
        settings=settings,
        artifacts_dir=artifacts_dir,
        report=report,
        source_payloads=source_payloads,
        phase_totals_s=phase_timer.totals_s,
        run_total_s=time.perf_counter() - started,
        rejected_sequence_count=assembly.rejected_sequence_count,
        live_log_path=live_log.path,
    )
    return report


def export_artifacts_dir(settings: ScanSettings) -> Path:
    return (
        settings.artifacts_dir
        if settings.artifacts_dir is not None
        else settings.output_dir / "artifacts"
    )


def assemble_export_rows(
    fragment_sequences: Iterable[Iterable[PokemonFragment]],
) -> ExportAssemblyResult:
    warnings: list[ExportWarning] = []
    candidates = [
        _row_candidate_from_fragments(sequence) for sequence in fragment_sequences
    ]
    for candidate in candidates:
        warnings.extend(candidate.warnings)

    accepted, merge_warnings = _merge_identity_candidates_with_support(candidates)
    warnings.extend(merge_warnings)
    rows = [candidate.row for candidate in accepted if candidate.accepted]
    row_diagnostics = _row_candidate_diagnostics(candidates, accepted)
    return ExportAssemblyResult(
        rows=rows,
        warnings=warnings,
        rejected_sequence_count=len(candidates) - len(rows),
        row_diagnostics=row_diagnostics,
        unresolved_pokemon_like_sequence_count=sum(
            1
            for diagnostic in row_diagnostics
            if diagnostic.get("outcome") == "rejected"
            and diagnostic.get("pokemon_like") is True
        ),
    )


def write_export_csv(path: Path, rows: Iterable[dict[str, ExportValue]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(EXPORT_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(row))


def write_export_xlsx(path: Path, rows: Iterable[dict[str, ExportValue]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    worksheet = cast(Any, workbook.active)
    worksheet.title = "Pokemon"
    worksheet.append(list(EXPORT_COLUMNS))
    for row in rows:
        worksheet.append([_cell_value(row.get(column)) for column in EXPORT_COLUMNS])
    worksheet.freeze_panes = "A2"
    worksheet.auto_filter.ref = worksheet.dimensions
    workbook.save(path)


def _collect_frame_candidates(
    settings: ScanSettings, artifacts_dir: Path
) -> _FrameCollectionResult:
    warnings: list[ExportWarning] = []
    processed_files: list[Path] = []
    failed_files: list[Path] = []
    source_payloads: dict[str, object] = {}
    frames: list[FrameCandidate] = []

    for asset in discover_inputs(settings.input_path):
        asset_name = _source_file_name(asset)
        source_frames_dir = artifacts_dir / _source_artifact_stem(asset_name) / "frames"
        try:
            if asset.source_type == "frames_jsonl":
                jsonl_load = load_jsonl_frame_candidates(asset, artifacts_dir)
                warnings.extend(
                    ExportWarning("input", warning) for warning in jsonl_load.warnings
                )
                source_payloads.update(jsonl_load.source_payloads)
                source_payloads[asset.path.name] = jsonl_load.input_payload
                frames.extend(jsonl_load.frames)
            elif asset.source_type == "video":
                extraction = extract_video_frames(asset, source_frames_dir)
                warnings.extend(
                    ExportWarning("input", warning) for warning in extraction.warnings
                )
                source_payloads[asset_name] = build_video_source_payload(
                    asset.source_type, extraction
                )
                frames.extend(extraction.frames)
            else:
                image_frames = copy_image_frame(asset, source_frames_dir)
                source_payloads[asset_name] = build_image_source_payload(
                    asset.source_type, image_frames
                )
                frames.extend(image_frames)
            processed_files.append(asset.path)
        except Exception as exc:  # noqa: BLE001
            failed_files.append(asset.path)
            warnings.append(ExportWarning("input", f"{asset.path.name}: {exc}"))

    return _FrameCollectionResult(
        frames=frames,
        processed_files=processed_files,
        failed_files=failed_files,
        warnings=warnings,
        source_payloads=source_payloads,
    )


def _bounded_export_enabled(settings: ScanSettings) -> bool:
    return settings.max_export_frame_files > 0


def _run_bounded_production_export(
    settings: ScanSettings,
    artifacts_dir: Path,
    *,
    started: float,
    phase_timer: PhaseTimer,
    visual_scanner: VisualScanner,
    sequence_scanner: SequenceScanner,
    catalog: MetadataCatalog | None,
    live_log: _ExportLiveLogger,
) -> ExportReport:
    bounded_processing = _process_bounded_export_sources(
        settings,
        artifacts_dir,
        phase_timer,
        visual_scanner=visual_scanner,
        sequence_scanner=sequence_scanner,
        live_log=live_log,
    )
    report = ExportReport(
        processed_files=bounded_processing.processed_files,
        failed_files=bounded_processing.failed_files,
        warnings=list(bounded_processing.warnings),
        worker_count=bounded_processing.worker_count,
        retry_count=bounded_processing.retry_count,
        sequence_worker_count=bounded_processing.sequence_worker_count,
        sequence_retry_count=bounded_processing.sequence_retry_count,
        repair_worker_count=bounded_processing.repair_worker_count,
        repair_retry_count=bounded_processing.repair_retry_count,
        repaired_sequence_count=bounded_processing.repaired_sequence_count,
        frame_count=bounded_processing.frame_count,
        visual_frame_count=bounded_processing.visual_frame_count,
        sequence_count=bounded_processing.sequence_count,
        worker_events=list(bounded_processing.worker_events),
        bounded_chunk_events=list(bounded_processing.bounded_chunk_events),
    )
    _apply_bounded_frame_file_stats(report, bounded_processing.bounded_frame_file_stats)
    if not TesseractOcrEngine(lang=settings.ocr_lang).is_available():
        report.warnings.append(
            ExportWarning(
                "environment",
                "Tesseract is unavailable; OCR-backed export fields may be blank.",
            )
        )

    return _complete_production_export(
        settings,
        artifacts_dir,
        report,
        [
            result
            for _source_order, _first_frame_index, result in sorted(
                bounded_processing.scan_results,
                key=lambda item: (item[0], item[1]),
            )
        ],
        source_payloads=bounded_processing.source_payloads,
        started=started,
        phase_timer=phase_timer,
        sequence_scanner=sequence_scanner,
        catalog=catalog,
        live_log=live_log,
    )


def _apply_bounded_frame_file_stats(
    report: ExportReport, stats: _BoundedFrameFileStats
) -> None:
    report.max_export_frame_files = stats.max_export_frame_files
    report.bounded_extraction_enabled = stats.bounded_extraction_enabled
    report.peak_export_frame_files = stats.peak_export_frame_files
    report.deleted_list_or_non_extractable_frames = (
        stats.deleted_list_or_non_extractable_frames
    )
    report.deleted_sequence_frames = stats.deleted_sequence_frames
    report.deleted_unsequenced_visual_frames = stats.deleted_unsequenced_visual_frames
    report.retained_frame_count = stats.retained_frame_count
    report.frame_lifecycle_events = list(stats.frame_lifecycle_events)
    report.bounded_extraction_soft_limit_exceeded = (
        stats.bounded_extraction_soft_limit_exceeded
    )


def _normalize_bounded_chunk_extraction_result(
    result: _BoundedChunkExtractionResult | VideoExtractionResult,
) -> _BoundedChunkExtractionResult:
    if isinstance(result, _BoundedChunkExtractionResult):
        return result
    return _BoundedChunkExtractionResult(result, str(result.used_hwaccel or "unknown"))


def _process_bounded_export_sources(
    settings: ScanSettings,
    artifacts_dir: Path,
    phase_timer: PhaseTimer,
    *,
    visual_scanner: VisualScanner,
    sequence_scanner: SequenceScanner,
    live_log: _ExportLiveLogger,
) -> _BoundedProcessingResult:
    processing = _BoundedProcessingResult(
        scan_results=[],
        processed_files=[],
        failed_files=[],
        warnings=[],
        source_payloads={},
        bounded_frame_file_stats=_BoundedFrameFileStats(
            max_export_frame_files=settings.max_export_frame_files,
            bounded_extraction_enabled=True,
        ),
    )
    assets = phase_timer.run(
        "input_collection", lambda: discover_inputs(settings.input_path)
    )
    for source_order, asset in enumerate(assets):
        try:
            _process_bounded_export_asset(
                asset,
                source_order,
                settings,
                artifacts_dir,
                phase_timer,
                visual_scanner=visual_scanner,
                sequence_scanner=sequence_scanner,
                live_log=live_log,
                processing=processing,
            )
            processing.processed_files.append(asset.path)
        except Exception as exc:  # noqa: BLE001
            processing.failed_files.append(asset.path)
            processing.warnings.append(
                ExportWarning("input", f"{asset.path.name}: {exc}")
            )
    return processing


def _process_bounded_export_asset(
    asset: SourceAsset,
    source_order: int,
    settings: ScanSettings,
    artifacts_dir: Path,
    phase_timer: PhaseTimer,
    *,
    visual_scanner: VisualScanner,
    sequence_scanner: SequenceScanner,
    live_log: _ExportLiveLogger,
    processing: _BoundedProcessingResult,
) -> None:
    asset_name = _source_file_name(asset)
    source_frames_dir = artifacts_dir / _source_artifact_stem(asset_name) / "frames"
    if asset.source_type == "frames_jsonl":
        jsonl_load = phase_timer.run(
            "jsonl_frame_loading",
            lambda: load_jsonl_frame_candidates(asset, artifacts_dir),
        )
        processing.warnings.extend(
            ExportWarning("input", warning) for warning in jsonl_load.warnings
        )
        processing.source_payloads.update(jsonl_load.source_payloads)
        processing.source_payloads[asset.path.name] = jsonl_load.input_payload
        _process_bounded_complete_frame_set(
            jsonl_load.frames,
            source_order,
            source_frames_dir,
            settings,
            phase_timer,
            visual_scanner=visual_scanner,
            sequence_scanner=sequence_scanner,
            live_log=live_log,
            processing=processing,
        )
        return

    if asset.source_type == "video":
        _process_bounded_video_asset(
            asset,
            source_order,
            source_frames_dir,
            settings,
            phase_timer,
            visual_scanner=visual_scanner,
            sequence_scanner=sequence_scanner,
            live_log=live_log,
            processing=processing,
        )
        return

    image_frames = phase_timer.run(
        "image_copy", lambda: copy_image_frame(asset, source_frames_dir)
    )
    processing.source_payloads[asset_name] = build_image_source_payload(
        asset.source_type, image_frames
    )
    _record_bounded_frame_peak(processing.bounded_frame_file_stats, source_frames_dir)
    _process_bounded_complete_frame_set(
        image_frames,
        source_order,
        source_frames_dir,
        settings,
        phase_timer,
        visual_scanner=visual_scanner,
        sequence_scanner=sequence_scanner,
        live_log=live_log,
        processing=processing,
    )


def _process_bounded_complete_frame_set(
    frames: list[FrameCandidate],
    source_order: int,
    source_frames_dir: Path,
    settings: ScanSettings,
    phase_timer: PhaseTimer,
    *,
    visual_scanner: VisualScanner,
    sequence_scanner: SequenceScanner,
    live_log: _ExportLiveLogger,
    processing: _BoundedProcessingResult,
) -> None:
    processing.frame_count += len(frames)
    _record_extraction_lifecycle_events(frames, processing.bounded_frame_file_stats)
    visual_processing = phase_timer.run(
        "visual_analysis",
        lambda: _process_visual_frames_with_retry(
            frames,
            settings,
            visual_scanner=visual_scanner,
            live_logger=live_log,
        ),
    )
    _add_visual_processing_counts(processing, visual_processing)
    _record_visual_lifecycle_events(
        visual_processing.records, processing.bounded_frame_file_stats
    )
    _delete_classified_non_sequence_frames(
        visual_processing.records,
        source_frames_dir,
        processing.bounded_frame_file_stats,
    )
    sequences = phase_timer.run(
        "sequence_grouping",
        lambda: group_production_sequences(visual_processing.records),
    )
    completed_sequences = [
        (source_order, _sequence_first_index(sequence), sequence)
        for sequence in sequences
    ]
    _process_bounded_completed_sequences(
        completed_sequences,
        source_frames_dir,
        settings,
        phase_timer,
        sequence_scanner=sequence_scanner,
        live_log=live_log,
        processing=processing,
    )
    processed_frame_indexes = {
        record.frame_index
        for _source_order, _first_index, sequence in completed_sequences
        for record in sequence
    }
    _delete_unsequenced_visual_frame_files(
        visual_processing.records,
        processed_frame_indexes,
        source_frames_dir,
        processing.bounded_frame_file_stats,
        live_log,
    )


# pylint: disable-next=too-many-statements
def _process_bounded_video_asset(
    asset: SourceAsset,
    source_order: int,
    source_frames_dir: Path,
    settings: ScanSettings,
    phase_timer: PhaseTimer,
    *,
    visual_scanner: VisualScanner,
    sequence_scanner: SequenceScanner,
    live_log: _ExportLiveLogger,
    processing: _BoundedProcessingResult,
) -> None:
    asset_name = _source_file_name(asset)
    frame_count = phase_timer.run(
        "video_frame_count_probe", lambda: _probe_video_frame_count(asset.path)
    )
    if frame_count is None or frame_count <= 0:
        extraction = phase_timer.run(
            "frame_extraction",
            lambda: extract_video_frames(asset, source_frames_dir),
        )
        fallback_warning = (
            f"{asset.path.name}: could not determine video frame count; "
            "fell back to unlimited export frame extraction."
        )
        processing.warnings.append(
            ExportWarning("bounded_extraction", fallback_warning)
        )
        extraction.warnings.append(fallback_warning)
        processing.source_payloads[asset_name] = build_video_source_payload(
            asset.source_type, extraction
        )
        _record_bounded_frame_peak(
            processing.bounded_frame_file_stats, source_frames_dir
        )
        _process_bounded_complete_frame_set(
            extraction.frames,
            source_order,
            source_frames_dir,
            settings,
            phase_timer,
            visual_scanner=visual_scanner,
            sequence_scanner=sequence_scanner,
            live_log=live_log,
            processing=processing,
        )
        return

    source_frames_dir.mkdir(parents=True, exist_ok=True)
    _clear_export_frame_files(source_frames_dir)
    duration = phase_timer.run(
        "video_duration_probe", lambda: probe_video_duration(asset.path)
    )
    timeline_probe = phase_timer.run(
        "video_frame_timeline_probe",
        lambda: _probe_video_frame_timeline(asset.path, frame_count),
    )
    live_log.dump(
        "bounded extraction timeline "
        f"status={timeline_probe.status} "
        f"frame_count={timeline_probe.frame_count} "
        f"reason={timeline_probe.reason or 'none'}"
    )
    extraction_warnings: list[str] = []
    processing.frame_count += frame_count
    processing.source_payloads[asset_name] = _bounded_video_source_payload(
        asset.source_type,
        frame_count,
        extraction_warnings,
        timeline_probe=timeline_probe,
    )

    seen_records: list[FrameVisualRecord] = []
    processed_frame_indexes: set[int] = set()
    oldest_unextracted = frame_count
    first_chunk = True
    while oldest_unextracted > 0:
        chunk_size = _bounded_chunk_size(
            settings.max_export_frame_files,
            source_frames_dir,
            first_chunk=first_chunk,
        )
        chunk_last_index = oldest_unextracted - 1
        chunk_first_index = max(0, chunk_last_index - chunk_size + 1)
        frame_files_before_extraction = _count_export_frame_files(source_frames_dir)

        def extract_current_chunk(
            first_frame_index: int = chunk_first_index,
            last_frame_index: int = chunk_last_index,
        ) -> _BoundedChunkExtractionResult | VideoExtractionResult:
            return _extract_video_frame_chunk(
                asset,
                source_frames_dir,
                first_frame_index,
                last_frame_index,
                total_frame_count=frame_count,
                duration_s=duration,
                timeline=timeline_probe.timeline,
            )

        extraction_result = phase_timer.run(
            "frame_extraction",
            extract_current_chunk,
        )
        chunk_extraction = _normalize_bounded_chunk_extraction_result(extraction_result)
        extraction = chunk_extraction.extraction
        frame_files_after_extraction = _count_export_frame_files(source_frames_dir)
        previous_warning_count = len(extraction_warnings)
        _extend_unique_warnings(extraction_warnings, extraction.warnings)
        processing.warnings.extend(
            ExportWarning("input", warning)
            for warning in extraction_warnings[previous_warning_count:]
        )
        _record_bounded_frame_peak(
            processing.bounded_frame_file_stats, source_frames_dir
        )
        _warn_if_bounded_soft_limit_exceeded(
            processing.bounded_frame_file_stats,
            source_frames_dir,
            processing.warnings,
            asset_name,
        )
        live_log.dump(
            f"bounded chunk extracted frames {chunk_first_index}-{chunk_last_index} "
            f"method={chunk_extraction.method} "
            f"fallback_reason={chunk_extraction.fallback_reason or 'none'}"
        )
        extracted_frames = tuple(extraction.frames)

        def process_extracted_frames(
            frames: tuple[FrameCandidate, ...] = extracted_frames,
        ) -> _VisualProcessingResult:
            return _process_visual_frames_with_retry(
                list(frames),
                settings,
                visual_scanner=visual_scanner,
                live_logger=live_log,
            )

        visual_processing = phase_timer.run(
            "visual_analysis",
            process_extracted_frames,
        )
        _add_visual_processing_counts(processing, visual_processing)
        _record_extraction_lifecycle_events(
            extraction.frames, processing.bounded_frame_file_stats
        )
        _record_visual_lifecycle_events(
            visual_processing.records, processing.bounded_frame_file_stats
        )
        _delete_classified_non_sequence_frames(
            visual_processing.records,
            source_frames_dir,
            processing.bounded_frame_file_stats,
        )
        frame_files_after_visual_cleanup = _count_export_frame_files(source_frames_dir)
        seen_records.extend(visual_processing.records)
        oldest_unextracted = chunk_first_index
        _process_bounded_seen_sequences(
            seen_records,
            processed_frame_indexes,
            oldest_unextracted,
            source_order,
            source_frames_dir,
            settings,
            phase_timer,
            sequence_scanner=sequence_scanner,
            live_log=live_log,
            processing=processing,
        )
        frame_files_after_sequence_release = _count_export_frame_files(
            source_frames_dir
        )
        chunk_event = {
            "source_file": asset_name,
            "first_frame_index": chunk_first_index,
            "last_frame_index": chunk_last_index,
            "requested_chunk_size": chunk_size,
            "extracted_frame_count": len(extraction.frames),
            "extraction_method": chunk_extraction.method,
            "extraction_fallback_reason": chunk_extraction.fallback_reason,
            "seek_start_s": _rounded_optional_float(chunk_extraction.seek_start_s),
            "seek_duration_s": _rounded_optional_float(
                chunk_extraction.seek_duration_s
            ),
            "frame_files_before_extraction": frame_files_before_extraction,
            "frame_files_after_extraction": frame_files_after_extraction,
            "frame_files_after_visual_cleanup": frame_files_after_visual_cleanup,
            "frame_files_after_sequence_release": frame_files_after_sequence_release,
        }
        processing.bounded_chunk_events.append(chunk_event)
        live_log.dump(
            "bounded chunk accounting "
            + " ".join(f"{key}={value}" for key, value in chunk_event.items())
        )
        first_chunk = False

    _delete_unsequenced_visual_frame_files(
        seen_records,
        processed_frame_indexes,
        source_frames_dir,
        processing.bounded_frame_file_stats,
        live_log,
    )
    final_frame_files = _count_export_frame_files(source_frames_dir)
    if processing.bounded_chunk_events:
        final_chunk_event = processing.bounded_chunk_events[-1]
        final_chunk_event["frame_files_after_final_cleanup"] = final_frame_files
    live_log.dump(f"bounded final cleanup frame_files={final_frame_files}")
    processing.source_payloads[asset_name] = _bounded_video_source_payload(
        asset.source_type,
        frame_count,
        extraction_warnings,
        timeline_probe=timeline_probe,
        chunk_events=[
            event
            for event in processing.bounded_chunk_events
            if event.get("source_file") == asset_name
        ],
    )


def _add_visual_processing_counts(
    processing: _BoundedProcessingResult, visual_processing: _VisualProcessingResult
) -> None:
    processing.visual_frame_count += len(visual_processing.records)
    processing.worker_count = max(
        processing.worker_count, visual_processing.worker_count
    )
    processing.retry_count += visual_processing.retry_count
    processing.worker_events.extend(visual_processing.worker_events)
    processing.warnings.extend(visual_processing.warnings)


# pylint: disable-next=too-many-arguments,too-many-positional-arguments
def _process_bounded_seen_sequences(
    seen_records: list[FrameVisualRecord],
    processed_frame_indexes: set[int],
    oldest_unextracted: int,
    source_order: int,
    source_frames_dir: Path,
    settings: ScanSettings,
    phase_timer: PhaseTimer,
    *,
    sequence_scanner: SequenceScanner,
    live_log: _ExportLiveLogger,
    processing: _BoundedProcessingResult,
) -> None:
    sequences = phase_timer.run(
        "sequence_grouping", lambda: group_production_sequences(seen_records)
    )
    completed_sequences: list[tuple[int, int, list[FrameVisualRecord]]] = []
    for sequence in sequences:
        frame_indexes = {record.frame_index for record in sequence}
        if frame_indexes <= processed_frame_indexes:
            continue
        if frame_indexes & processed_frame_indexes:
            continue
        first_index = min(frame_indexes)
        if oldest_unextracted > 0 and first_index == oldest_unextracted:
            if len(sequence) > settings.max_export_frame_files:
                _mark_bounded_soft_limit_exceeded(
                    processing.bounded_frame_file_stats,
                    processing.warnings,
                    sequence[0].source_file,
                    (
                        "A single open production sequence exceeded "
                        f"{settings.max_export_frame_files} frame file(s); "
                        "kept the extra files temporarily to preserve correctness."
                    ),
                )
            continue
        completed_sequences.append((source_order, first_index, sequence))

    completed_sequences.sort(key=lambda item: item[1], reverse=True)
    _process_bounded_completed_sequences(
        completed_sequences,
        source_frames_dir,
        settings,
        phase_timer,
        sequence_scanner=sequence_scanner,
        live_log=live_log,
        processing=processing,
    )
    for _source_order, _first_index, sequence in completed_sequences:
        processed_frame_indexes.update(record.frame_index for record in sequence)


def _process_bounded_completed_sequences(
    sequences: list[tuple[int, int, list[FrameVisualRecord]]],
    source_frames_dir: Path,
    settings: ScanSettings,
    phase_timer: PhaseTimer,
    *,
    sequence_scanner: SequenceScanner,
    live_log: _ExportLiveLogger,
    processing: _BoundedProcessingResult,
) -> None:
    if not sequences:
        return
    processing.sequence_count += len(sequences)
    lifecycle = phase_timer.run(
        "sequence_scanning",
        lambda: _scan_sequences_with_optional_repair(
            sequences,
            settings,
            sequence_scanner,
            sequence_progress_callback=lambda frame, fields: (
                _log_bounded_frame_progress(
                    live_log,
                    processing.bounded_frame_file_stats,
                    "sequence",
                    frame,
                    fields,
                )
            ),
            repair_progress_callback=lambda frame, fields: _log_bounded_frame_progress(
                live_log,
                processing.bounded_frame_file_stats,
                "repair",
                frame,
                fields,
            ),
            release_callback=lambda sequence: _release_bounded_sequence_frame_files(
                sequence,
                source_frames_dir,
                processing.bounded_frame_file_stats,
                live_log,
            ),
            live_logger=live_log,
        ),
    )
    processing.scan_results.extend(lifecycle.records)
    processing.warnings.extend(lifecycle.warnings)
    processing.sequence_worker_count = max(
        processing.sequence_worker_count, lifecycle.worker_count
    )
    processing.sequence_retry_count += lifecycle.retry_count
    processing.worker_events.extend(lifecycle.worker_events)
    processing.repair_worker_count = max(
        processing.repair_worker_count, lifecycle.repair_worker_count
    )
    processing.repair_retry_count += lifecycle.repair_retry_count
    processing.repaired_sequence_count += lifecycle.repaired_count


def _scan_sequences_with_optional_repair(
    sequences: list[tuple[int, int, list[FrameVisualRecord]]],
    settings: ScanSettings,
    sequence_scanner: SequenceScanner,
    *,
    sequence_progress_callback: SequenceProgressCallback | None = None,
    repair_progress_callback: SequenceProgressCallback | None = None,
    release_callback: Callable[[list[FrameVisualRecord]], None] | None = None,
    live_logger: _ExportLiveLogger | None = None,
) -> _SequenceLifecycleResult:
    records: list[tuple[int, int, ProductionSequenceScanResult]] = []
    warnings: list[ExportWarning] = []
    worker_events: list[dict[str, object]] = []
    repaired_count = 0
    repair_worker_count = 1
    repair_retry_count = 0

    def process_item(
        item: tuple[int, int, list[FrameVisualRecord]],
    ) -> _SequenceLifecycleScan:
        _source_order, _first_index, sequence = item
        return _scan_sequence_with_optional_repair(
            sequence,
            settings,
            sequence_scanner,
            sequence_progress_callback=sequence_progress_callback,
            repair_progress_callback=repair_progress_callback,
            live_logger=live_logger,
        )

    def on_success(
        item: tuple[int, int, list[FrameVisualRecord]],
        scan: _SequenceLifecycleScan,
        attempt: int,
    ) -> None:
        del attempt
        nonlocal repaired_count, repair_worker_count, repair_retry_count
        source_order, first_index, sequence = item
        records.append((source_order, first_index, scan.result))
        warnings.extend(scan.warnings)
        worker_events.extend(scan.worker_events)
        repaired_count += scan.repaired_count
        repair_worker_count = max(repair_worker_count, scan.repair_worker_count)
        repair_retry_count += scan.repair_retry_count
        if release_callback is not None:
            release_callback(sequence)

    def on_final_failure(
        item: tuple[int, int, list[FrameVisualRecord]], exc: Exception, attempt: int
    ) -> None:
        del attempt
        _source_order, _first_index, sequence = item
        warnings.append(_sequence_scan_failure_warning(sequence, exc))
        if release_callback is not None:
            release_callback(sequence)

    retry_summary = execute_with_adaptive_retries(
        sequences,
        requested_workers=settings.workers,
        max_attempts=settings.max_frame_attempts,
        process_item=process_item,
        on_success=on_success,
        on_final_failure=on_final_failure,
        warnings=warnings,
        build_retry_warning=lambda pending_count, worker_count: ExportWarning(
            "sequence_scanning",
            (
                f"{pending_count} production sequence task(s) failed with "
                f"{worker_count} workers; requeued with reduced concurrency."
            ),
        ),
        on_diagnostic=lambda event: _record_worker_event(
            worker_events, "sequence_scanning", event, live_logger
        ),
    )
    records.sort(key=lambda item: (item[0], item[1]))
    return _SequenceLifecycleResult(
        records,
        warnings,
        retry_summary.worker_count,
        retry_summary.retry_count,
        repair_worker_count,
        repair_retry_count,
        repaired_count,
        worker_events,
    )


def _scan_sequence_with_optional_repair(
    sequence: list[FrameVisualRecord],
    settings: ScanSettings,
    sequence_scanner: SequenceScanner,
    *,
    sequence_progress_callback: SequenceProgressCallback | None,
    repair_progress_callback: SequenceProgressCallback | None,
    live_logger: _ExportLiveLogger | None = None,
) -> _SequenceLifecycleScan:
    result = _scan_production_sequence_item(
        sequence,
        settings,
        sequence_scanner,
        sequence_progress_callback,
    )
    if sequence_scanner is not scan_production_sequence:
        return _SequenceLifecycleScan(result, [])

    repair = _repair_production_sequences(
        [sequence],
        [result],
        settings,
        progress_callback=repair_progress_callback,
        live_logger=live_logger,
    )
    return _SequenceLifecycleScan(
        repair.records[0] if repair.records else result,
        repair.warnings,
        repair.repaired_count,
        repair.worker_count,
        repair.retry_count,
        repair.worker_events,
    )


def _sequence_first_index(sequence: Sequence[FrameVisualRecord]) -> int:
    return min((record.frame_index for record in sequence), default=0)


def _delete_classified_non_sequence_frames(
    records: Sequence[FrameVisualRecord],
    source_frames_dir: Path,
    stats: _BoundedFrameFileStats,
) -> None:
    for record in records:
        if record.raw_classification not in {"list", NON_EXTRACTABLE_CLASS}:
            continue
        if _delete_export_frame_file(record.frame, source_frames_dir):
            stats.deleted_list_or_non_extractable_frames += 1
            _record_frame_lifecycle(
                stats,
                record.frame,
                "deleted",
                f"classified:{record.raw_classification}",
                "cleanup",
            )
        else:
            _record_frame_lifecycle(
                stats,
                record.frame,
                "skipped",
                f"classified:{record.raw_classification}:already_absent",
                "cleanup",
            )


def _record_extraction_lifecycle_events(
    frames: Sequence[FrameCandidate], stats: _BoundedFrameFileStats
) -> None:
    for frame in frames:
        _record_frame_lifecycle(stats, frame, "processed", "extracted", "extraction")


def _record_visual_lifecycle_events(
    records: Sequence[FrameVisualRecord], stats: _BoundedFrameFileStats
) -> None:
    for record in records:
        _record_frame_lifecycle(
            stats, record.frame, "processed", "visual_analysis", "visual"
        )


def _log_bounded_frame_progress(
    live_log: _ExportLiveLogger,
    stats: _BoundedFrameFileStats,
    phase: str,
    frame: FrameCandidate,
    fields: tuple[str, ...],
) -> None:
    live_log.log_frame(phase, frame, fields)
    reason = ",".join(fields) if fields else "no_fields"
    action = "skipped" if reason.startswith(("skip:", "stop:")) else "processed"
    _record_frame_lifecycle(stats, frame, action, reason, phase)


def _delete_unsequenced_visual_frame_files(
    records: Sequence[FrameVisualRecord],
    processed_frame_indexes: set[int],
    source_frames_dir: Path,
    stats: _BoundedFrameFileStats,
    live_log: _ExportLiveLogger,
) -> None:
    deleted_count = 0
    retained_count = 0
    seen_paths: set[Path] = set()
    for record in records:
        if record.frame_index in processed_frame_indexes:
            continue
        safe_path = _safe_export_frame_file_path(record.frame, source_frames_dir)
        if safe_path is None:
            if record.frame.frame_path.is_file():
                retained_count += 1
                stats.retained_frame_count += 1
                _record_frame_lifecycle(
                    stats,
                    record.frame,
                    "retained",
                    "unsafe_or_non_export_frame_path",
                    "cleanup",
                )
            continue
        if safe_path in seen_paths:
            continue
        seen_paths.add(safe_path)
        if _unlink_file(safe_path):
            deleted_count += 1
            stats.deleted_unsequenced_visual_frames += 1
            _record_frame_lifecycle(
                stats,
                record.frame,
                "deleted",
                "visual_not_in_completed_sequence",
                "cleanup",
            )
        else:
            _record_frame_lifecycle(
                stats,
                record.frame,
                "skipped",
                "visual_not_in_completed_sequence:already_absent",
                "cleanup",
            )
    if deleted_count or retained_count:
        live_log.dump(
            "bounded cleanup finalized "
            f"{deleted_count} unsequenced visual frame file(s), "
            f"retained {retained_count}"
        )


def _record_frame_lifecycle(
    stats: _BoundedFrameFileStats,
    frame: FrameCandidate,
    action: str,
    reason: str,
    phase: str,
) -> None:
    stats.frame_lifecycle_events.append(
        FrameLifecycleEvent(
            action=action,
            reason=reason,
            source_file=frame.source_asset.path.name,
            frame_index=frame.frame_index,
            frame_name=frame.frame_path.name,
            phase=phase,
        )
    )


def _delete_sequence_frame_files(
    sequence: Sequence[FrameVisualRecord],
    source_frames_dir: Path,
    stats: _BoundedFrameFileStats,
) -> int:
    seen_paths: set[Path] = set()
    deleted_count = 0
    for record in sequence:
        safe_path = _safe_export_frame_file_path(record.frame, source_frames_dir)
        if safe_path is None or safe_path in seen_paths:
            continue
        seen_paths.add(safe_path)
        if _unlink_file(safe_path):
            stats.deleted_sequence_frames += 1
            deleted_count += 1
            _record_frame_lifecycle(
                stats,
                record.frame,
                "deleted",
                "completed_sequence_released",
                "cleanup",
            )
    return deleted_count


def _release_bounded_sequence_frame_files(
    sequence: Sequence[FrameVisualRecord],
    source_frames_dir: Path,
    stats: _BoundedFrameFileStats,
    live_log: _ExportLiveLogger,
) -> None:
    deleted_count = _delete_sequence_frame_files(sequence, source_frames_dir, stats)
    if not sequence:
        return
    first_index = min(record.frame_index for record in sequence)
    last_index = max(record.frame_index for record in sequence)
    live_log.dump(
        "bounded cleanup released "
        f"{deleted_count} frame file(s) for sequence {first_index}-{last_index}"
    )


def _delete_export_frame_file(frame: FrameCandidate, source_frames_dir: Path) -> bool:
    safe_path = _safe_export_frame_file_path(frame, source_frames_dir)
    return False if safe_path is None else _unlink_file(safe_path)


def _safe_export_frame_file_path(
    frame: FrameCandidate, source_frames_dir: Path
) -> Path | None:
    try:
        root = source_frames_dir.resolve()
        target = frame.frame_path.resolve()
        original = frame.source_asset.path.resolve()
    except OSError:
        return None
    if target == original or not target.is_relative_to(root):
        return None
    if not frame.frame_path.name.startswith("frame_"):
        return None
    if "__" in frame.frame_path.stem:
        return None
    return frame.frame_path


def _unlink_file(path: Path) -> bool:
    try:
        if path.is_file():
            path.unlink()
            return True
    except FileNotFoundError:
        return False
    return False


def _clear_export_frame_files(source_frames_dir: Path) -> None:
    source_frames_dir.mkdir(parents=True, exist_ok=True)
    for frame_path in source_frames_dir.glob("frame_*"):
        if frame_path.is_file():
            frame_path.unlink()


def _count_export_frame_files(source_frames_dir: Path) -> int:
    return sum(
        1
        for frame_path in source_frames_dir.glob("frame_*")
        if frame_path.is_file() and "__" not in frame_path.stem
    )


def _record_bounded_frame_peak(
    stats: _BoundedFrameFileStats, source_frames_dir: Path
) -> None:
    stats.peak_export_frame_files = max(
        stats.peak_export_frame_files,
        _count_export_frame_files(source_frames_dir),
    )


def _warn_if_bounded_soft_limit_exceeded(
    stats: _BoundedFrameFileStats,
    source_frames_dir: Path,
    warnings: list[ExportWarning],
    source_file: str,
) -> None:
    active_count = _count_export_frame_files(source_frames_dir)
    if active_count <= stats.max_export_frame_files:
        return
    _mark_bounded_soft_limit_exceeded(
        stats,
        warnings,
        source_file,
        (
            f"Temporary export frame file count reached {active_count}, "
            f"above the configured soft limit of {stats.max_export_frame_files}; "
            "kept needed frames to preserve correctness."
        ),
    )


def _mark_bounded_soft_limit_exceeded(
    stats: _BoundedFrameFileStats,
    warnings: list[ExportWarning],
    source_file: str,
    message: str,
) -> None:
    if not stats.bounded_extraction_soft_limit_exceeded:
        warnings.append(ExportWarning("bounded_extraction", message, source_file))
    stats.bounded_extraction_soft_limit_exceeded = True


def _bounded_chunk_size(
    max_export_frame_files: int, source_frames_dir: Path, *, first_chunk: bool
) -> int:
    base_size = (
        max_export_frame_files if first_chunk else max(1, max_export_frame_files // 5)
    )
    if first_chunk:
        return max(1, base_size)
    active_count = _count_export_frame_files(source_frames_dir)
    if active_count >= max_export_frame_files:
        return max(1, base_size)
    return max(1, min(base_size, max_export_frame_files - active_count))


def _probe_video_frame_count(source_path: Path) -> int | None:
    commands = [
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_read_frames",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source_path),
        ],
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=nb_frames",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source_path),
        ],
    ]
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        for line in completed.stdout.splitlines():
            value = line.strip()
            if value.isdigit() and int(value) > 0:
                return int(value)
    return None


def _probe_video_frame_timeline(
    source_path: Path, expected_frame_count: int
) -> _VideoFrameTimelineProbe:
    if expected_frame_count <= 0:
        return _VideoFrameTimelineProbe(
            "unavailable", reason="expected_frame_count_not_positive"
        )
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "frame=best_effort_timestamp_time",
        "-of",
        "json",
        str(source_path),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return _VideoFrameTimelineProbe(
            "unavailable",
            reason=f"ffprobe_failed:{exc.__class__.__name__}",
        )

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _VideoFrameTimelineProbe("unavailable", reason="invalid_ffprobe_json")

    frames = payload.get("frames")
    if not isinstance(frames, list):
        return _VideoFrameTimelineProbe("unavailable", reason="missing_frames")
    timestamps: list[float] = []
    for frame in frames:
        if not isinstance(frame, dict):
            return _VideoFrameTimelineProbe(
                "unavailable",
                frame_count=len(timestamps),
                reason="invalid_frame_payload",
            )
        value = frame.get("best_effort_timestamp_time")
        if value is None:
            return _VideoFrameTimelineProbe(
                "unavailable",
                frame_count=len(timestamps),
                reason="missing_timestamp",
            )
        try:
            timestamp_s = float(value)
        except (TypeError, ValueError):
            return _VideoFrameTimelineProbe(
                "unavailable",
                frame_count=len(timestamps),
                reason="invalid_timestamp",
            )
        if timestamps and timestamp_s <= timestamps[-1]:
            return _VideoFrameTimelineProbe(
                "unavailable",
                frame_count=len(timestamps) + 1,
                reason="non_monotonic_timestamps",
            )
        timestamps.append(timestamp_s)

    if len(timestamps) != expected_frame_count:
        return _VideoFrameTimelineProbe(
            "unavailable",
            frame_count=len(timestamps),
            reason=(
                "frame_count_mismatch:"
                f"expected={expected_frame_count}:actual={len(timestamps)}"
            ),
        )
    return _VideoFrameTimelineProbe(
        "available",
        frame_count=len(timestamps),
        timeline=_VideoFrameTimeline(tuple(timestamps)),
    )


def _bounded_video_source_payload(
    source_type: str,
    frame_count: int,
    warnings: list[str],
    *,
    timeline_probe: _VideoFrameTimelineProbe | None = None,
    chunk_events: Sequence[dict[str, object]] = (),
) -> dict[str, object]:
    extraction_payload: dict[str, object] = {
        "requested_hwaccel": "auto",
        "used_hwaccel": "bounded_chunks",
    }
    if timeline_probe is not None:
        extraction_payload["timeline_probe"] = timeline_probe.to_json_dict()
    if chunk_events:
        method_counts = Counter(
            str(event.get("extraction_method") or "unknown") for event in chunk_events
        )
        extraction_payload["seeked_chunk_count"] = method_counts.get("time_seek", 0)
        extraction_payload["fallback_chunk_count"] = method_counts.get(
            "range_select", 0
        )
        extraction_payload["chunk_count"] = len(chunk_events)
    return {
        "source_type": source_type,
        "frame_count": frame_count,
        "warnings": warnings,
        "video_extraction": extraction_payload,
    }


def _extract_video_frame_chunk(
    source_asset: SourceAsset,
    source_frames_dir: Path,
    first_frame_index: int,
    last_frame_index: int,
    *,
    total_frame_count: int,
    duration_s: float,
    timeline: _VideoFrameTimeline | None = None,
) -> _BoundedChunkExtractionResult:
    source_frames_dir.mkdir(parents=True, exist_ok=True)
    for frame_index in range(first_frame_index, last_frame_index + 1):
        _unlink_file(_video_frame_path(source_frames_dir, frame_index))

    fallback_reason = "timeline_unavailable"
    if timeline is not None:
        try:
            fast_result = _try_extract_video_frame_chunk_with_timeline(
                source_asset,
                source_frames_dir,
                first_frame_index,
                last_frame_index,
                total_frame_count=total_frame_count,
                duration_s=duration_s,
                timeline=timeline,
            )
        except (
            OSError,
            subprocess.CalledProcessError,
            RuntimeError,
            ValueError,
        ) as exc:
            fallback_reason = f"time_seek_failed:{exc.__class__.__name__}"
        else:
            if fast_result is not None:
                return fast_result
            fallback_reason = "time_seek_validation_failed"

    extraction = _extract_video_frame_chunk_with_range_select(
        source_asset,
        source_frames_dir,
        first_frame_index,
        last_frame_index,
        total_frame_count=total_frame_count,
        duration_s=duration_s,
    )
    return _BoundedChunkExtractionResult(
        extraction,
        "range_select",
        fallback_reason=fallback_reason,
    )


def _extract_video_frame_chunk_with_range_select(
    source_asset: SourceAsset,
    source_frames_dir: Path,
    first_frame_index: int,
    last_frame_index: int,
    *,
    total_frame_count: int,
    duration_s: float,
) -> VideoExtractionResult:
    for frame_index in range(first_frame_index, last_frame_index + 1):
        _unlink_file(_video_frame_path(source_frames_dir, frame_index))

    pattern = source_frames_dir / "frame_%06d.png"
    command = _build_ffmpeg_extract_range_command(
        source_asset.path,
        pattern,
        first_frame_index,
        last_frame_index,
    )
    subprocess.run(command, check=True, capture_output=True, text=True)
    indexed_paths = [
        (frame_index, _video_frame_path(source_frames_dir, frame_index))
        for frame_index in range(first_frame_index, last_frame_index + 1)
    ]
    existing_paths = [(index, path) for index, path in indexed_paths if path.is_file()]
    if not existing_paths:
        msg = (
            f"{source_asset.path.name}: FFmpeg extracted no frames for "
            f"{first_frame_index}-{last_frame_index}."
        )
        raise RuntimeError(msg)
    warnings: list[str] = []
    if len(existing_paths) != len(indexed_paths):
        warnings.append(
            f"{source_asset.path.name}: extracted {len(existing_paths)} of "
            f"{len(indexed_paths)} requested bounded frame(s)."
        )
    return VideoExtractionResult(
        [
            FrameCandidate(
                source_asset,
                path,
                frame_index,
                _frame_timestamp_s(frame_index, total_frame_count, duration_s),
            )
            for frame_index, path in existing_paths
        ],
        "bounded_chunks",
        warnings,
    )


def _try_extract_video_frame_chunk_with_timeline(
    source_asset: SourceAsset,
    source_frames_dir: Path,
    first_frame_index: int,
    last_frame_index: int,
    *,
    total_frame_count: int,
    duration_s: float,
    timeline: _VideoFrameTimeline,
) -> _BoundedChunkExtractionResult | None:
    window = _time_seek_window_for_chunk(timeline, first_frame_index, last_frame_index)
    temp_dir = source_frames_dir / (
        f"__bounded_chunk_{first_frame_index}_{last_frame_index}"
    )
    _clear_bounded_chunk_temp_dir(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    pattern = temp_dir / "frame_%06d.png"
    command = _build_ffmpeg_extract_time_window_command(
        source_asset.path,
        pattern,
        first_frame_index,
        window,
    )
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
        expected_names = {
            _video_frame_path(temp_dir, frame_index).name
            for frame_index in range(first_frame_index, last_frame_index + 1)
        }
        actual_paths = sorted(temp_dir.glob("frame_*.png"))
        actual_names = {path.name for path in actual_paths if path.is_file()}
        if actual_names != expected_names:
            return None
        for frame_index in range(first_frame_index, last_frame_index + 1):
            source_path = _video_frame_path(temp_dir, frame_index)
            target_path = _video_frame_path(source_frames_dir, frame_index)
            _unlink_file(target_path)
            source_path.replace(target_path)
    finally:
        _clear_bounded_chunk_temp_dir(temp_dir)

    indexed_paths = [
        (frame_index, _video_frame_path(source_frames_dir, frame_index))
        for frame_index in range(first_frame_index, last_frame_index + 1)
    ]
    return _BoundedChunkExtractionResult(
        VideoExtractionResult(
            [
                FrameCandidate(
                    source_asset,
                    path,
                    frame_index,
                    _frame_timestamp_s(frame_index, total_frame_count, duration_s),
                )
                for frame_index, path in indexed_paths
                if path.is_file()
            ],
            "bounded_chunks_time_seek",
            [],
        ),
        "time_seek",
        seek_start_s=window.seek_start_s,
        seek_duration_s=window.seek_duration_s,
    )


def _build_ffmpeg_extract_range_command(
    source_path: Path,
    pattern: Path,
    first_frame_index: int,
    last_frame_index: int,
) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-vf",
        f"select=between(n\\,{first_frame_index}\\,{last_frame_index})",
        "-vsync",
        "0",
        "-start_number",
        str(first_frame_index + 1),
        str(pattern),
    ]


def _build_ffmpeg_extract_time_window_command(
    source_path: Path,
    pattern: Path,
    first_frame_index: int,
    window: _TimeSeekWindow,
) -> list[str]:
    return [
        "ffmpeg",
        "-y",
        "-ss",
        _format_ffmpeg_seconds(window.seek_start_s),
        "-i",
        str(source_path),
        "-t",
        _format_ffmpeg_seconds(window.seek_duration_s),
        "-vf",
        (
            "select="
            f"gte(t\\,{_format_ffmpeg_seconds(window.select_start_s)})"
            "*"
            f"lt(t\\,{_format_ffmpeg_seconds(window.select_end_s)})"
        ),
        "-vsync",
        "0",
        "-start_number",
        str(first_frame_index + 1),
        str(pattern),
    ]


def _time_seek_window_for_chunk(
    timeline: _VideoFrameTimeline, first_frame_index: int, last_frame_index: int
) -> _TimeSeekWindow:
    timestamps = timeline.timestamps_s
    if (
        first_frame_index < 0
        or last_frame_index < first_frame_index
        or last_frame_index >= len(timestamps)
    ):
        msg = (
            "Invalid bounded frame chunk for timeline: "
            f"{first_frame_index}-{last_frame_index}."
        )
        raise ValueError(msg)
    start_timestamp_s = timestamps[first_frame_index]
    if last_frame_index + 1 < len(timestamps):
        end_timestamp_s = timestamps[last_frame_index + 1]
    else:
        end_timestamp_s = timestamps[last_frame_index] + _timeline_frame_interval_s(
            timeline, last_frame_index
        )
    chunk_span_s = max(0.001, end_timestamp_s - start_timestamp_s)
    safety_margin_s = max(0.25, min(2.0, chunk_span_s * 0.1))
    seek_start_s = max(0.0, start_timestamp_s - safety_margin_s)
    select_start_s = max(0.0, start_timestamp_s - seek_start_s)
    select_end_s = max(select_start_s + 0.001, end_timestamp_s - seek_start_s)
    seek_duration_s = select_end_s + safety_margin_s
    return _TimeSeekWindow(
        seek_start_s,
        seek_duration_s,
        select_start_s,
        select_end_s,
    )


def _timeline_frame_interval_s(
    timeline: _VideoFrameTimeline, frame_index: int
) -> float:
    timestamps = timeline.timestamps_s
    if frame_index + 1 < len(timestamps):
        return max(0.001, timestamps[frame_index + 1] - timestamps[frame_index])
    if frame_index > 0:
        return max(0.001, timestamps[frame_index] - timestamps[frame_index - 1])
    return 1.0 / 30.0


def _format_ffmpeg_seconds(value: float) -> str:
    return f"{max(0.0, value):.6f}"


def _clear_bounded_chunk_temp_dir(temp_dir: Path) -> None:
    if not temp_dir.exists():
        return
    for path in temp_dir.glob("*"):
        if path.is_file():
            path.unlink()
    try:
        temp_dir.rmdir()
    except OSError:
        pass


def _video_frame_path(source_frames_dir: Path, frame_index: int) -> Path:
    return source_frames_dir / f"frame_{frame_index + 1:06d}.png"


def _frame_timestamp_s(
    frame_index: int, total_frame_count: int, duration_s: float
) -> float:
    denominator = max(1, total_frame_count - 1)
    if not duration_s:
        return float(frame_index)
    return duration_s * frame_index / denominator


def _extend_unique_warnings(target: list[str], new_warnings: Iterable[str]) -> None:
    for warning in new_warnings:
        if warning not in target:
            target.append(warning)


def _process_visual_frames_with_retry(
    frames: list[FrameCandidate],
    settings: ScanSettings,
    *,
    visual_scanner: VisualScanner,
    live_logger: _ExportLiveLogger | None = None,
) -> _VisualProcessingResult:
    records: list[FrameVisualRecord] = []
    warnings: list[ExportWarning] = []
    worker_events: list[dict[str, object]] = []

    def process_frame(frame: FrameCandidate) -> FrameVisualRecord:
        if live_logger is not None:
            live_logger.log_frame("visual", frame)
        return visual_scanner(frame)

    retry_summary = execute_with_adaptive_retries(
        frames,
        requested_workers=settings.workers,
        max_attempts=settings.max_frame_attempts,
        process_item=process_frame,
        on_success=lambda frame, record, attempt: records.append(record),
        on_final_failure=lambda frame, exc, attempt: warnings.append(
            ExportWarning(
                "visual_analysis",
                (
                    f"{_source_file_name(frame.source_asset)} "
                    f"frame {frame.frame_index}: {exc}"
                ),
            )
        ),
        warnings=warnings,
        build_retry_warning=lambda pending_count, worker_count: ExportWarning(
            "visual_analysis",
            (
                f"{pending_count} visual frame task(s) failed with {worker_count} "
                "workers; requeued with reduced concurrency."
            ),
        ),
        on_diagnostic=lambda event: _record_worker_event(
            worker_events, "visual_analysis", event, live_logger
        ),
    )

    records.sort(key=lambda record: (record.source_file, record.frame_index))
    return _VisualProcessingResult(
        records,
        warnings,
        retry_summary.worker_count,
        retry_summary.retry_count,
        worker_events,
    )


def _scan_production_sequences(
    sequences: list[list[FrameVisualRecord]],
    settings: ScanSettings,
    sequence_scanner: SequenceScanner,
    *,
    progress_callback: SequenceProgressCallback | None = None,
    live_logger: _ExportLiveLogger | None = None,
) -> _SequenceProcessingResult:
    results: list[tuple[int, ProductionSequenceScanResult]] = []
    warnings: list[ExportWarning] = []
    worker_events: list[dict[str, object]] = []
    retry_summary = execute_with_adaptive_retries(
        list(enumerate(sequences)),
        requested_workers=settings.workers,
        max_attempts=settings.max_frame_attempts,
        process_item=lambda item: _scan_production_sequence_item(
            item[1],
            settings,
            sequence_scanner,
            progress_callback,
        ),
        on_success=lambda item, result, attempt: results.append((item[0], result)),
        on_final_failure=lambda item, exc, attempt: warnings.append(
            _sequence_scan_failure_warning(item[1], exc)
        ),
        warnings=warnings,
        build_retry_warning=lambda pending_count, worker_count: ExportWarning(
            "sequence_scanning",
            (
                f"{pending_count} production sequence task(s) failed with "
                f"{worker_count} workers; requeued with reduced concurrency."
            ),
        ),
        on_diagnostic=lambda event: _record_worker_event(
            worker_events, "sequence_scanning", event, live_logger
        ),
    )

    results.sort(key=lambda item: item[0])
    return _SequenceProcessingResult(
        [result for _index, result in results],
        warnings,
        retry_summary.worker_count,
        retry_summary.retry_count,
        worker_events,
    )


def _repair_production_sequences(
    sequences: list[list[FrameVisualRecord]],
    scan_results: list[ProductionSequenceScanResult],
    settings: ScanSettings,
    *,
    progress_callback: SequenceProgressCallback | None = None,
    live_logger: _ExportLiveLogger | None = None,
) -> _SequenceRepairResult:
    repaired = list(scan_results)
    warnings: list[ExportWarning] = []
    worker_events: list[dict[str, object]] = []
    repaired_count = 0
    repair_items = [
        (index, sequence)
        for index, (sequence, result) in enumerate(
            zip(sequences, scan_results, strict=True)
        )
        if _production_sequence_needs_repair(result)
    ]

    def process_item(
        item: tuple[int, list[FrameVisualRecord]],
    ) -> ProductionSequenceScanResult:
        _index, sequence = item
        return scan_production_sequence_repair(
            sequence,
            settings,
            progress_callback=progress_callback,
        )

    def on_success(
        item: tuple[int, list[FrameVisualRecord]],
        result: ProductionSequenceScanResult,
        attempt: int,
    ) -> None:
        del attempt
        nonlocal repaired_count
        index, _sequence = item
        repaired[index] = result
        repaired_count += 1

    retry_summary = execute_with_adaptive_retries(
        repair_items,
        requested_workers=settings.workers,
        max_attempts=settings.max_frame_attempts,
        process_item=process_item,
        on_success=on_success,
        on_final_failure=lambda item, exc, _attempt: warnings.append(
            _sequence_repair_failure_warning(item[1], exc)
        ),
        warnings=warnings,
        build_retry_warning=lambda pending_count, worker_count: ExportWarning(
            "sequence_repair",
            (
                f"{pending_count} production sequence repair task(s) failed "
                f"with {worker_count} workers; requeued with reduced concurrency."
            ),
        ),
        on_diagnostic=lambda event: _record_worker_event(
            worker_events, "sequence_repair", event, live_logger
        ),
    )

    return _SequenceRepairResult(
        repaired,
        warnings,
        repaired_count,
        retry_summary.worker_count,
        retry_summary.retry_count,
        worker_events,
    )


def _stabilize_same_hp_sequence_weights(
    scan_results: list[ProductionSequenceScanResult],
) -> list[ExportWarning]:
    warnings: list[ExportWarning] = []
    current_key: tuple[str, str] | None = None
    current_run: list[ProductionSequenceScanResult] = []

    def flush_run() -> None:
        if current_run:
            warnings.extend(_stabilize_same_hp_weight_run(current_run))
            current_run.clear()

    for result in scan_results:
        source_file = _sequence_result_source_file(result)
        hp = _sequence_result_hp(result)
        key = (source_file, hp) if source_file and hp else None
        if key is None:
            flush_run()
            current_key = None
            continue
        if current_key is not None and key != current_key:
            flush_run()
        current_key = key
        current_run.append(result)

    flush_run()
    return warnings


def _stabilize_same_hp_weight_run(
    run: list[ProductionSequenceScanResult],
) -> list[ExportWarning]:
    if len(run) < 2:
        return []

    weight = _dominant_sequence_weight(
        current_weight
        for result in run
        for record in result.records
        if (current_weight := _record_weight_value(record)) is not None
    )
    if weight is None:
        return []

    ignored_values: set[ExportValue] = set()
    changed = False
    for result in run:
        result_changed = False
        for record in result.records:
            current_weight = _record_weight_value(record)
            if current_weight is None or current_weight == weight:
                continue
            ignored_values.add(current_weight)
            record.values["weight_kg"] = weight
            record.features["has_weight"] = True
            record.signals["cross_sequence_weight_corrected"] = True
            record.signals["cross_sequence_weight_value"] = cast(Any, weight)
            record.signals["cross_sequence_weight_original_value"] = cast(
                Any, current_weight
            )
            if _CROSS_SEQUENCE_WEIGHT_CORRECTED_NOTE not in record.notes:
                record.notes.append(_CROSS_SEQUENCE_WEIGHT_CORRECTED_NOTE)
            result_changed = True
        if result_changed:
            refresh_production_sequence_result(result)
            changed = True

    if not changed:
        return []

    source_file = _sequence_result_source_file(run[0])
    first_frame, last_frame = _sequence_result_span(run)
    return [
        ExportWarning(
            "sequence_weight",
            (
                "Corrected same-HP production sequence weight to "
                f"{weight}; ignored outlier(s): {_joined_values(ignored_values)}."
            ),
            source_file,
            first_frame,
            last_frame,
        )
    ]


def _sequence_result_source_file(result: ProductionSequenceScanResult) -> str:
    for record in result.records:
        if record.source_file:
            return record.source_file
    return ""


def _sequence_result_hp(result: ProductionSequenceScanResult) -> str:
    accepted_hp = result.accepted_fields.get("hp")
    if isinstance(accepted_hp, str) and accepted_hp.strip():
        return accepted_hp.strip()

    hp_values = {
        value.strip()
        for record in result.records
        if isinstance(value := record.values.get("hp"), str) and value.strip()
    }
    return next(iter(hp_values)) if len(hp_values) == 1 else ""


def _record_weight_value(record: FrameScanRecord) -> str | None:
    value = record.values.get("weight_kg")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _sequence_result_span(
    run: list[ProductionSequenceScanResult],
) -> tuple[int | None, int | None]:
    frame_indexes = [record.frame_index for result in run for record in result.records]
    if not frame_indexes:
        return None, None
    return min(frame_indexes), max(frame_indexes)


def _production_sequence_needs_repair(
    result: ProductionSequenceScanResult,
) -> bool:
    accepted = result.accepted_fields
    has_physical_identity = {"hp", "weight"}.issubset(accepted)
    if not result.completed:
        return has_physical_identity
    if result.sequence_type != "detail/raw=detail":
        return False
    if len(result.records) > 2:
        return False
    return has_physical_identity and "moves" in accepted


def _scan_production_sequence_item(
    sequence: list[FrameVisualRecord],
    settings: ScanSettings,
    sequence_scanner: SequenceScanner,
    progress_callback: SequenceProgressCallback | None,
) -> ProductionSequenceScanResult:
    if progress_callback is not None and sequence_scanner is scan_production_sequence:
        return scan_production_sequence(
            sequence,
            settings,
            progress_callback=progress_callback,
        )
    return sequence_scanner(sequence, settings)


def _sequence_scan_failure_warning(
    sequence: list[FrameVisualRecord], exc: Exception
) -> ExportWarning:
    return ExportWarning(
        "sequence_scanning",
        f"Skipped production sequence after scan failure: {exc}",
        sequence[0].source_file if sequence else "",
        min((record.frame_index for record in sequence), default=None),
        max((record.frame_index for record in sequence), default=None),
    )


def _sequence_repair_failure_warning(
    sequence: list[FrameVisualRecord], exc: Exception
) -> ExportWarning:
    return ExportWarning(
        "sequence_repair",
        f"Kept initial production sequence scan after repair failure: {exc}",
        sequence[0].source_file if sequence else "",
        min((record.frame_index for record in sequence), default=None),
        max((record.frame_index for record in sequence), default=None),
    )


def _row_candidate_from_fragments(
    fragment_sequence: Iterable[PokemonFragment],
) -> _RowCandidate:
    fragments = [
        fragment
        for fragment in fragment_sequence
        if fragment.fragment_type in {"detail", "appraisal"}
    ]
    if not fragments:
        return _RowCandidate(
            row=_empty_row(),
            identity_key=None,
            warnings=[
                ExportWarning(
                    "row_assembly",
                    "Skipped production sequence without detail/appraisal fragments.",
                )
            ],
        )

    fragments.sort(
        key=lambda fragment: (fragment.source_file, fragment.frame_index),
        reverse=True,
    )
    detail_frame_indexes = _fragment_frame_indexes(fragments, "detail")
    appraisal_frame_indexes = _fragment_frame_indexes(fragments, "appraisal")
    row = _empty_row()
    warnings: list[ExportWarning] = []
    _add_source_span(row, fragments)
    internal_fields, conflicts, value_counts = _apply_first_clean_fragment_values(
        row, fragments, warnings
    )
    _apply_derived_flags(row)
    if conflicts:
        return _RowCandidate(
            row,
            _identity_key(row),
            warnings,
            anchor_kind="conflict",
            scan_start_frame_index=fragments[0].frame_index,
            fragment_types={fragment.fragment_type for fragment in fragments},
            column_value_counts=value_counts,
            source_detail_frame_indexes=detail_frame_indexes,
            source_appraisal_frame_indexes=appraisal_frame_indexes,
        )

    identity_key = _identity_key(row)
    anchor_kind = _anchor_kind(row, internal_fields)
    if identity_key is None:
        missing_core = [
            column
            for column in ("hp_current", "hp_max", "weight_kg")
            if not _present(row[column])
        ]
        warnings.append(
            _span_warning(
                "row_assembly",
                (
                    "Rejected incomplete export row; missing core identity field(s): "
                    f"{', '.join(missing_core)}."
                ),
                fragments,
            )
        )
        return _RowCandidate(
            row,
            identity_key,
            warnings,
            anchor_kind=anchor_kind,
            scan_start_frame_index=fragments[0].frame_index,
            fragment_types={fragment.fragment_type for fragment in fragments},
            column_value_counts=value_counts,
            source_detail_frame_indexes=detail_frame_indexes,
            source_appraisal_frame_indexes=appraisal_frame_indexes,
        )

    if anchor_kind == "support":
        missing_anchor = _missing_anchor_fields(row, internal_fields, fragments)
        warnings.append(
            _span_warning(
                "row_assembly",
                (
                    "Kept support-only production evidence; missing anchor field(s): "
                    f"{', '.join(missing_anchor)}."
                ),
                fragments,
            )
        )
        return _RowCandidate(
            row,
            identity_key,
            warnings,
            anchor_kind=anchor_kind,
            scan_start_frame_index=fragments[0].frame_index,
            fragment_types={fragment.fragment_type for fragment in fragments},
            column_value_counts=value_counts,
            source_detail_frame_indexes=detail_frame_indexes,
            source_appraisal_frame_indexes=appraisal_frame_indexes,
        )

    if anchor_kind == "appraisal" and "iv_star_agreement" not in internal_fields:
        if row["iv_complete"] is not True:
            row["iv_complete"] = False
        warnings.append(
            _span_warning(
                "row_assembly",
                (
                    "Accepted appraisal anchor without IV/star agreement; "
                    "iv_complete remains false."
                ),
                fragments,
            )
        )

    return _RowCandidate(
        row,
        identity_key,
        warnings,
        accepted=True,
        anchor_kind=anchor_kind,
        scan_start_frame_index=fragments[0].frame_index,
        fragment_types={fragment.fragment_type for fragment in fragments},
        column_value_counts=value_counts,
        source_detail_frame_indexes=detail_frame_indexes,
        source_appraisal_frame_indexes=appraisal_frame_indexes,
    )


def _fragment_frame_indexes(
    fragments: Iterable[PokemonFragment],
    fragment_type: str,
) -> tuple[int, ...]:
    return tuple(
        sorted(
            fragment.frame_index
            for fragment in fragments
            if fragment.fragment_type == fragment_type
        )
    )


@dataclass(frozen=True, slots=True)
class _FragmentColumnValue:
    value: ExportValue
    fragment_type: str
    frame_index: int


@dataclass(frozen=True, slots=True)
class _CompleteIvFragmentEvidence:
    fragment: PokemonFragment
    triplet: tuple[int, int, int]
    values: dict[str, ExportValue]


# pylint: disable-next=too-many-branches
def _apply_first_clean_fragment_values(
    row: dict[str, ExportValue],
    fragments: list[PokemonFragment],
    warnings: list[ExportWarning],
) -> tuple[
    set[str],
    dict[str, set[ExportValue]],
    dict[str, Counter[ExportValue]],
]:
    internal_fields: set[str] = set()
    values_by_column: dict[str, list[_FragmentColumnValue]] = {}

    for fragment in fragments:
        for field_name, field_value in fragment.fields.items():
            if field_name in _INTERNAL_ANCHOR_FIELDS:
                if field_value.value is True:
                    internal_fields.add(field_name)
                continue

            column = _FIELD_TO_COLUMN.get(field_name)
            if column is None:
                continue
            value = field_value.value
            if not _present(value):
                continue
            values_by_column.setdefault(column, []).append(
                _FragmentColumnValue(
                    value=value,
                    fragment_type=fragment.fragment_type,
                    frame_index=fragment.frame_index,
                )
            )

    critical_conflicts: dict[str, set[ExportValue]] = {}
    value_counts = {
        column: Counter(item.value for item in column_values)
        for column, column_values in values_by_column.items()
    }
    locked_columns = _apply_complete_iv_fragment_values(
        row, fragments, values_by_column, warnings
    )
    for column, column_values in sorted(values_by_column.items()):
        if column in locked_columns:
            continue
        if column in _TRUE_IF_ANY_COLUMNS:
            bool_values = [
                item.value for item in column_values if isinstance(item.value, bool)
            ]
            if any(value is True for value in bool_values):
                row[column] = True
                continue
            if column == "iv_complete" and any(value is False for value in bool_values):
                row[column] = False
                continue

        selected, ignored, unresolved = _select_fragment_column_value(
            column,
            column_values,
            values_by_column,
        )
        if _present(selected):
            row[column] = selected
            if ignored:
                warnings.append(
                    _span_warning(
                        "row_assembly",
                        _ignored_consensus_message(column, ignored),
                        fragments,
                    )
                )
            continue
        if not unresolved:
            continue
        if column in _CRITICAL_CONFLICT_COLUMNS:
            warnings.append(
                _span_warning(
                    "row_assembly",
                    f"Rejected conflicting export evidence for {column}: "
                    f"{_joined_values(unresolved)}.",
                    fragments,
                )
            )
            row[column] = None
            critical_conflicts[column] = unresolved
            continue

        warnings.append(
            _span_warning(
                "row_assembly",
                _ignored_consensus_message(column, unresolved),
                fragments,
            )
        )
        if column in {"cp", "display_name"}:
            row[column] = None
    _enforce_iv_complete_invariant(
        row,
        lambda message: warnings.append(
            _span_warning("row_assembly", message, fragments)
        ),
    )
    return internal_fields, critical_conflicts, value_counts


def _apply_complete_iv_fragment_values(
    row: dict[str, ExportValue],
    fragments: list[PokemonFragment],
    values_by_column: dict[str, list[_FragmentColumnValue]],
    warnings: list[ExportWarning],
) -> set[str]:
    selected = _select_complete_iv_fragment(fragments)
    if selected is None:
        return set()

    locked_columns: set[str] = set()
    for column, value in selected.values.items():
        row[column] = value
        locked_columns.add(column)
    row["iv_complete"] = True
    locked_columns.add("iv_complete")

    ignored = _ignored_weaker_iv_values(selected, values_by_column, locked_columns)
    if ignored:
        warnings.append(
            _span_warning(
                "row_assembly",
                (
                    "Ignored incomplete or conflicting IV evidence after selecting "
                    "complete same-frame IV from frame "
                    f"{selected.fragment.frame_index}: "
                    f"{'; '.join(ignored)}."
                ),
                fragments,
            )
        )
    return locked_columns


def _select_complete_iv_fragment(
    fragments: list[PokemonFragment],
) -> _CompleteIvFragmentEvidence | None:
    candidates = [
        candidate
        for fragment in fragments
        if (candidate := _complete_iv_fragment_evidence(fragment)) is not None
    ]
    if not candidates:
        return None
    triplets = {candidate.triplet for candidate in candidates}
    if len(triplets) != 1:
        return None
    return candidates[0]


def _complete_iv_fragment_evidence(
    fragment: PokemonFragment,
) -> _CompleteIvFragmentEvidence | None:
    if fragment.fragment_type != "appraisal":
        return None
    iv_complete = fragment.fields.get("iv_complete")
    if iv_complete is None or iv_complete.value is not True:
        return None

    triplet_values: list[int] = []
    for column in _IV_TRIPLET_COLUMNS:
        value = _int_fragment_value(fragment, column)
        if value is None:
            return None
        triplet_values.append(value)

    values: dict[str, ExportValue] = dict(
        zip(_IV_TRIPLET_COLUMNS, triplet_values, strict=True)
    )
    values["iv_complete"] = True
    for column in ("iv_sum", "appraisal_star_count"):
        value = _int_fragment_value(fragment, column)
        if value is not None:
            values[column] = value
    appraisal_perfect = fragment.fields.get("appraisal_perfect")
    if appraisal_perfect is not None and isinstance(appraisal_perfect.value, bool):
        values["appraisal_perfect"] = appraisal_perfect.value

    return _CompleteIvFragmentEvidence(
        fragment=fragment,
        triplet=cast(tuple[int, int, int], tuple(triplet_values)),
        values=values,
    )


def _int_fragment_value(fragment: PokemonFragment, field_name: str) -> int | None:
    field_value = fragment.fields.get(field_name)
    if field_value is None:
        return None
    value = field_value.value
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _ignored_weaker_iv_values(
    selected: _CompleteIvFragmentEvidence,
    values_by_column: dict[str, list[_FragmentColumnValue]],
    locked_columns: set[str],
) -> list[str]:
    ignored: list[str] = []
    for column in _COMPLETE_IV_LOCK_COLUMNS:
        if column == "iv_complete" or column not in locked_columns:
            continue
        selected_value = selected.values.get(column)
        column_values = {
            item.value
            for item in values_by_column.get(column, ())
            if item.frame_index != selected.fragment.frame_index
            and item.value != selected_value
            and _present(item.value)
        }
        if column_values:
            ignored.append(f"{column}={_joined_values(column_values)}")
    return ignored


def _enforce_iv_complete_invariant(
    row: dict[str, ExportValue], warn: Callable[[str], None]
) -> None:
    if row["iv_complete"] is not True:
        return
    if all(_present(row[column]) for column in _IV_TRIPLET_COLUMNS):
        return
    row["iv_complete"] = False
    warn(
        "Set iv_complete=false because the final row does not contain a complete "
        "IV triplet."
    )


def _select_fragment_column_value(
    column: str,
    values: list[_FragmentColumnValue],
    related_values: dict[str, list[_FragmentColumnValue]] | None = None,
) -> tuple[ExportValue | None, set[ExportValue], set[ExportValue]]:
    if column == "height_m":
        detail_values = [
            item.value for item in values if item.fragment_type == "detail"
        ]
        if detail_values:
            selected, ignored, unresolved = _select_consensus_value(detail_values)
            if _present(selected):
                all_ignored = {item.value for item in values if item.value != selected}
                return selected, all_ignored, set()
            return selected, ignored, unresolved
    if column == "appraisal_star_count":
        return _select_appraisal_star_count_value(values, related_values or {})
    if column in _APPRAISAL_TEXT_CONSENSUS_COLUMNS:
        selected, ignored = _select_dominant_appraisal_text_value(values)
        if selected is not None:
            return selected, ignored, set()
    raw_values = [item.value for item in values]
    if column == "cp":
        return _select_cp_consensus_value(raw_values)
    return _select_consensus_value(raw_values)


def _select_dominant_appraisal_text_value(
    values: list[_FragmentColumnValue],
) -> tuple[ExportValue | None, set[ExportValue]]:
    appraisal_values = [
        item.value
        for item in values
        if item.fragment_type == "appraisal" and _present(item.value)
    ]
    if not appraisal_values:
        return None, set()
    counted = Counter(appraisal_values)
    if len(counted) == 1:
        return appraisal_values[0], set()
    ranked = counted.most_common()
    selected, selected_count = ranked[0]
    runner_up_count = ranked[1][1]
    if selected_count >= 2 and selected_count > runner_up_count:
        ignored = {item.value for item in values if _present(item.value)} - {selected}
        return selected, ignored
    return None, set()


def _apply_derived_flags(row: dict[str, ExportValue]) -> None:
    if (
        row["charged_move_key"] == "frustration"
        or row["second_charged_move_key"] == "frustration"
    ):
        row["is_shadow"] = True
    if row["has_gigantamax"] is True:
        row["has_dynamax"] = False


def _anchor_kind(row: dict[str, ExportValue], internal_fields: set[str]) -> str:
    if _has_appraisal_anchor(row, internal_fields):
        return "appraisal"
    if _has_detail_anchor(row):
        return "detail"
    return "support"


def _has_appraisal_anchor(
    row: dict[str, ExportValue], internal_fields: set[str]
) -> bool:
    del internal_fields
    return all(_present(row[column]) for column in _APPRAISAL_ANCHOR_COLUMNS)


def _has_detail_anchor(row: dict[str, ExportValue]) -> bool:
    return any(
        all(_present(row[column]) for column in group)
        for group in _DETAIL_ANCHOR_COLUMN_GROUPS
    )


def _missing_anchor_fields(
    row: dict[str, ExportValue],
    internal_fields: set[str],
    fragments: list[PokemonFragment],
) -> list[str]:
    del internal_fields
    fragment_types = {fragment.fragment_type for fragment in fragments}
    if "appraisal" in fragment_types:
        missing = [
            column
            for column in sorted(_APPRAISAL_ANCHOR_COLUMNS)
            if not _present(row[column])
        ]
        return missing or ["appraisal anchor"]

    missing = ["resolved_moves"] if not _has_detail_anchor(row) else []
    return missing or ["detail anchor"]


def _merge_identity_candidates_with_support(
    candidates: Iterable[_RowCandidate],
) -> tuple[list[_RowCandidate], list[ExportWarning]]:
    candidate_list = list(candidates)
    physical_keys_by_hp = _unique_physical_identity_keys_by_hp(candidate_list)
    initial_grouped: dict[IdentityKey | None, list[_RowCandidate]] = {}
    for candidate in candidate_list:
        identity_key = candidate.identity_key
        if identity_key is None and not candidate.accepted:
            identity_key = _matching_physical_identity_key(
                candidate, physical_keys_by_hp
            )
        initial_grouped.setdefault(identity_key, []).append(candidate)

    recovered_groups = _recovered_same_hp_detail_appraisal_groups(initial_grouped)
    recovered_candidate_ids = {
        candidate_id
        for _identity_key, recovered, _warning in recovered_groups
        for candidate_id in recovered
    }
    grouped: dict[IdentityKey | None, list[_RowCandidate]] = {
        identity_key: candidates
        for identity_key, recovered, _warning in recovered_groups
        if (candidates := list(recovered.values()))
    }
    for identity_key, matching in initial_grouped.items():
        remaining = [
            candidate
            for candidate in matching
            if id(candidate) not in recovered_candidate_ids
        ]
        if remaining:
            grouped.setdefault(identity_key, []).extend(remaining)

    merged: list[_RowCandidate] = []
    warnings = [warning for _identity_key, _recovered, warning in recovered_groups]
    for identity_key, matching in grouped.items():
        accepted = [candidate for candidate in matching if candidate.accepted]
        if identity_key is None:
            merged.extend(accepted)
            continue

        mergeable = sorted(
            matching,
            key=lambda candidate: candidate.scan_start_frame_index,
            reverse=True,
        )

        if not accepted:
            candidate = _merge_identity_candidates(identity_key, mergeable)
            warnings.extend(candidate.warnings)
            if candidate.accepted and _is_exportable_partial_row(candidate.row):
                warnings.append(
                    _row_span_warning(
                        "row_assembly",
                        (
                            "Accepted partial export row from complementary "
                            "unresolved production evidence."
                        ),
                        mergeable,
                    )
                )
                merged.append(candidate)
            continue

        if len(mergeable) == 1:
            for candidate in mergeable:
                if not candidate.accepted:
                    continue
                missing_moves_warnings: list[ExportWarning] = []
                _add_missing_moves_warning(
                    candidate.row,
                    [candidate],
                    missing_moves_warnings,
                )
                candidate.warnings.extend(missing_moves_warnings)
                warnings.extend(missing_moves_warnings)
                merged.append(candidate)
            continue

        candidate = _merge_identity_candidates(identity_key, mergeable)
        warnings.extend(candidate.warnings)
        if candidate.accepted:
            merged.append(candidate)

    merged.sort(
        key=lambda candidate: (
            str(candidate.row["source_file"] or ""),
            int(candidate.row["first_frame_index"] or 0),
        )
    )
    return merged, warnings


def _recovered_same_hp_detail_appraisal_groups(
    grouped: dict[IdentityKey | None, list[_RowCandidate]],
) -> list[tuple[IdentityKey, dict[int, _RowCandidate], ExportWarning]]:
    recovery_pool: list[_RowCandidate] = []
    for identity_key, matching in grouped.items():
        if identity_key is not None and len(matching) > 1:
            continue
        recovery_pool.extend(matching)

    candidates_by_hp: dict[tuple[str, int, int], list[_RowCandidate]] = {}
    for candidate in recovery_pool:
        hp_key = _same_hp_recovery_key(candidate)
        if hp_key is not None:
            candidates_by_hp.setdefault(hp_key, []).append(candidate)

    recovered: list[tuple[IdentityKey, dict[int, _RowCandidate], ExportWarning]] = []
    for hp_key, same_hp_candidates in candidates_by_hp.items():
        pair = _select_recovery_pair(same_hp_candidates)
        if pair is None:
            continue
        detail, appraisal = pair
        identity_key = _recovered_identity_key(hp_key, detail, appraisal)
        recovered_candidates = {
            id(appraisal): appraisal,
            id(detail): replace(detail, accepted=False),
        }
        recovered.append(
            (
                identity_key,
                recovered_candidates,
                _recovered_match_warning(detail, appraisal),
            )
        )
    return recovered


def _same_hp_recovery_key(candidate: _RowCandidate) -> tuple[str, int, int] | None:
    row = candidate.row
    source_file = row["source_file"]
    hp_current = row["hp_current"]
    hp_max = row["hp_max"]
    if not (
        isinstance(source_file, str)
        and isinstance(hp_current, int)
        and not isinstance(hp_current, bool)
        and isinstance(hp_max, int)
        and not isinstance(hp_max, bool)
    ):
        return None
    return (source_file, hp_current, hp_max)


def _select_recovery_pair(
    candidates: list[_RowCandidate],
) -> tuple[_RowCandidate, _RowCandidate] | None:
    appraisal_candidates = [
        candidate for candidate in candidates if _eligible_recovery_appraisal(candidate)
    ]
    detail_candidates = [
        candidate for candidate in candidates if _eligible_recovery_detail(candidate)
    ]
    compatible_pairs = [
        (detail, appraisal)
        for detail in detail_candidates
        for appraisal in appraisal_candidates
        if _compatible_recovery_pair(detail, appraisal)
    ]
    if len(appraisal_candidates) == 1 and len(detail_candidates) == 1:
        return compatible_pairs[0] if compatible_pairs else None

    species_pairs = [
        pair for pair in compatible_pairs if _same_present_species_key(*pair)
    ]
    if len(species_pairs) == 1:
        return species_pairs[0]
    return None


def _eligible_recovery_appraisal(candidate: _RowCandidate) -> bool:
    row = candidate.row
    return (
        candidate.accepted
        and candidate.anchor_kind == "appraisal"
        and "appraisal" in candidate.fragment_types
        and row["iv_complete"] is True
        and _has_appraisal_anchor(row, set())
    )


def _eligible_recovery_detail(candidate: _RowCandidate) -> bool:
    return (
        candidate.anchor_kind == "detail"
        and "detail" in candidate.fragment_types
        and _has_normal_moves(candidate.row)
    )


def _compatible_recovery_pair(detail: _RowCandidate, appraisal: _RowCandidate) -> bool:
    if not _recovery_physical_key_mismatch(detail, appraisal):
        return False
    if not _same_recovery_source_hp(detail, appraisal):
        return False
    if not _compatible_present_values(detail.row, appraisal.row):
        return False
    return True


def _recovery_physical_key_mismatch(
    detail: _RowCandidate, appraisal: _RowCandidate
) -> bool:
    detail_key = detail.identity_key
    appraisal_key = appraisal.identity_key
    if detail_key == appraisal_key:
        return False
    if detail_key is None or appraisal_key is None:
        return True
    return (
        len(detail_key) == 5
        and len(appraisal_key) == 5
        and detail_key[0] == "physical"
        and appraisal_key[0] == "physical"
        and detail_key[1:4] == appraisal_key[1:4]
    )


def _same_recovery_source_hp(detail: _RowCandidate, appraisal: _RowCandidate) -> bool:
    return _same_hp_recovery_key(detail) == _same_hp_recovery_key(appraisal)


def _compatible_present_values(
    detail_row: dict[str, ExportValue], appraisal_row: dict[str, ExportValue]
) -> bool:
    for column in _CRITICAL_CONFLICT_COLUMNS - {"weight_kg"}:
        detail_value = detail_row[column]
        appraisal_value = appraisal_row[column]
        if (
            _present(detail_value)
            and _present(appraisal_value)
            and detail_value != appraisal_value
        ):
            return False
    return True


def _same_present_species_key(detail: _RowCandidate, appraisal: _RowCandidate) -> bool:
    detail_species = detail.row["species_key"]
    appraisal_species = appraisal.row["species_key"]
    return (
        isinstance(detail_species, str)
        and bool(detail_species)
        and isinstance(appraisal_species, str)
        and detail_species == appraisal_species
    )


def _recovered_identity_key(
    hp_key: tuple[str, int, int],
    detail: _RowCandidate,
    appraisal: _RowCandidate,
) -> IdentityKey:
    source_file, hp_current, hp_max = hp_key
    first_frame = min(
        _required_int_row_value(detail.row, "first_frame_index"),
        _required_int_row_value(appraisal.row, "first_frame_index"),
    )
    last_frame = max(
        _required_int_row_value(detail.row, "last_frame_index"),
        _required_int_row_value(appraisal.row, "last_frame_index"),
    )
    return (
        "recovered_same_hp",
        source_file,
        str(hp_current),
        str(hp_max),
        str(first_frame),
        str(last_frame),
    )


def _recovered_match_warning(
    detail: _RowCandidate, appraisal: _RowCandidate
) -> ExportWarning:
    return _row_span_warning(
        "row_assembly",
        (
            "Recovered detail/appraisal match despite physical-key mismatch: "
            f"detail weight={_recovery_weight_text(detail)}, "
            f"appraisal weight={_recovery_weight_text(appraisal)}."
        ),
        [detail, appraisal],
    )


def _recovery_weight_text(candidate: _RowCandidate) -> str:
    value = candidate.row["weight_kg"]
    if isinstance(value, int | float) and not isinstance(value, bool):
        return f"{float(value):g}"
    return "missing"


def _is_exportable_partial_row(row: dict[str, ExportValue]) -> bool:
    return (
        _identity_key(row) is not None
        and _has_anchor_identity(row)
        and _has_strong_corroboration(row)
    )


def _row_candidate_diagnostics(
    candidates: list[_RowCandidate],
    accepted: list[_RowCandidate],
) -> list[dict[str, object]]:
    exported_keys = {
        candidate.identity_key for candidate in accepted if candidate.accepted
    }
    diagnostics: list[dict[str, object]] = []
    for candidate in candidates:
        if candidate.accepted:
            outcome = "accepted_candidate"
        elif candidate.identity_key in exported_keys or _candidate_within_exported_span(
            candidate, accepted
        ):
            outcome = "merged_support"
        else:
            outcome = "rejected"
        row = candidate.row
        diagnostics.append(
            {
                "outcome": outcome,
                "pokemon_like": _pokemon_like_candidate(row),
                "source_file": row["source_file"],
                "first_frame_index": row["first_frame_index"],
                "last_frame_index": row["last_frame_index"],
                "anchor_kind": candidate.anchor_kind,
                "identity_key": (
                    list(candidate.identity_key)
                    if candidate.identity_key is not None
                    else None
                ),
                "fragment_types": sorted(candidate.fragment_types),
                "source_detail_frames": list(candidate.source_detail_frame_indexes),
                "source_appraisal_frames": list(
                    candidate.source_appraisal_frame_indexes
                ),
                "present_fields": sorted(
                    column
                    for column in EXPORT_COLUMNS
                    if column not in _SOURCE_COLUMNS and _present(row[column])
                ),
                "missing_fields": sorted(
                    column
                    for column in EXPORT_COLUMNS
                    if column not in _SOURCE_COLUMNS and not _present(row[column])
                ),
                "moves_status": _candidate_moves_status(candidate),
                "field_values": {
                    column: row[column]
                    for column in EXPORT_COLUMNS
                    if column not in _SOURCE_COLUMNS and _present(row[column])
                },
                "skip_reasons": [warning.message for warning in candidate.warnings],
            }
        )
    return diagnostics


def _candidate_moves_status(candidate: _RowCandidate) -> str:
    if _has_normal_moves(candidate.row):
        return "resolved"
    if "detail" in candidate.fragment_types:
        return "detail_without_resolved_moves"
    if "appraisal" in candidate.fragment_types:
        return "appraisal_without_detail_moves"
    return "no_detail_candidate"


def _candidate_within_exported_span(
    candidate: _RowCandidate, accepted: Iterable[_RowCandidate]
) -> bool:
    source_file = candidate.row["source_file"]
    first_frame = candidate.row["first_frame_index"]
    last_frame = candidate.row["last_frame_index"]
    if not (
        isinstance(source_file, str)
        and isinstance(first_frame, int)
        and isinstance(last_frame, int)
    ):
        return False
    for exported in accepted:
        row = exported.row
        if row["source_file"] != source_file:
            continue
        exported_first = row["first_frame_index"]
        exported_last = row["last_frame_index"]
        if not isinstance(exported_first, int) or not isinstance(exported_last, int):
            continue
        if exported_first <= first_frame and last_frame <= exported_last:
            return True
    return False


def _pokemon_like_candidate(row: dict[str, ExportValue]) -> bool:
    if _has_anchor_identity(row) or _has_strong_corroboration(row):
        return True
    return all(_present(row[column]) for column in ("hp_current", "hp_max"))


def _unique_physical_identity_keys_by_hp(
    candidates: Iterable[_RowCandidate],
) -> dict[tuple[str, str, str], IdentityKey]:
    grouped: dict[tuple[str, str, str], set[IdentityKey]] = {}
    for candidate in candidates:
        key = candidate.identity_key
        if key is None or len(key) != 5 or key[0] != "physical":
            continue
        source_file, hp_current, hp_max = key[1], key[2], key[3]
        grouped.setdefault((source_file, hp_current, hp_max), set()).add(key)

    return {
        hp_key: next(iter(identity_keys))
        for hp_key, identity_keys in grouped.items()
        if len(identity_keys) == 1
    }


def _matching_physical_identity_key(
    candidate: _RowCandidate,
    physical_keys_by_hp: dict[tuple[str, str, str], IdentityKey],
) -> IdentityKey | None:
    row = candidate.row
    source_file = row["source_file"]
    hp_current = row["hp_current"]
    hp_max = row["hp_max"]
    if not (
        isinstance(source_file, str)
        and isinstance(hp_current, int)
        and isinstance(hp_max, int)
    ):
        return None
    return physical_keys_by_hp.get((source_file, str(hp_current), str(hp_max)))


# pylint: disable-next=too-many-branches,too-many-statements
def _merge_identity_candidates(
    identity_key: IdentityKey,
    candidates: list[_RowCandidate],
) -> _RowCandidate:
    accepted_candidates = [candidate for candidate in candidates if candidate.accepted]
    row = _empty_row()
    row["source_file"] = _first_present_row_value(candidates, "source_file")
    row["source_type"] = _first_present_row_value(candidates, "source_type")
    row["first_frame_index"] = min(
        _required_int_row_value(candidate.row, "first_frame_index")
        for candidate in candidates
    )
    row["last_frame_index"] = max(
        _required_int_row_value(candidate.row, "last_frame_index")
        for candidate in candidates
    )
    row["first_timestamp_s"] = round(
        min(
            _required_float_row_value(candidate.row, "first_timestamp_s")
            for candidate in candidates
        ),
        6,
    )
    row["last_timestamp_s"] = round(
        max(
            _required_float_row_value(candidate.row, "last_timestamp_s")
            for candidate in candidates
        ),
        6,
    )

    warnings: list[ExportWarning] = []
    critical_conflicts: dict[str, set[ExportValue]] = {}
    support_candidates = [
        candidate for candidate in candidates if not candidate.accepted
    ]

    for column in EXPORT_COLUMNS:
        if column in _SOURCE_COLUMNS:
            continue

        accepted_values = _candidate_column_values(accepted_candidates, column)
        support_values = _candidate_column_values(support_candidates, column)
        if column == "cp":
            primary_values = accepted_values + support_values
        elif column == "height_m":
            primary_values = accepted_values + support_values
        else:
            primary_values = accepted_values or support_values

        if column in _TRUE_IF_ANY_COLUMNS:
            bool_values = [
                item.value
                for item in accepted_values + support_values
                if isinstance(item.value, bool)
            ]
            if any(value is True for value in bool_values):
                row[column] = True
                continue
            if column == "iv_complete" and any(value is False for value in bool_values):
                row[column] = False
                continue

        if column == "cp":
            support_cp = _support_cp_for_noisy_low_appraisal(
                accepted_values, support_values
            )
            if support_cp is not None:
                row[column] = support_cp
                ignored = {
                    item.value
                    for item in accepted_values + support_values
                    if item.value != support_cp and _present(item.value)
                }
                if ignored:
                    warnings.append(
                        _row_span_warning(
                            "row_assembly",
                            _ignored_consensus_message(column, ignored),
                            candidates,
                        )
                    )
                continue

        if column in _CANONICAL_IDENTITY_COLUMNS:
            selected_identity, ignored_identity = _appraisal_identity_value(
                accepted_values + support_values
            )
            if selected_identity is not None:
                row[column] = selected_identity
                if ignored_identity:
                    warnings.append(
                        _row_span_warning(
                            "row_assembly",
                            (
                                f"Ignored conflicting detail identity evidence for "
                                f"{column}: {_joined_values(ignored_identity)}."
                            ),
                            candidates,
                        )
                    )
                continue

        selected, ignored, unresolved = _select_fragment_column_value(
            column,
            primary_values,
            {
                "iv_sum": (
                    accepted_values
                    if column == "iv_sum"
                    else _candidate_column_values(accepted_candidates, "iv_sum")
                    + _candidate_column_values(support_candidates, "iv_sum")
                ),
                "appraisal_perfect": _candidate_column_values(
                    accepted_candidates, "appraisal_perfect"
                )
                + _candidate_column_values(support_candidates, "appraisal_perfect"),
            },
        )
        if _present(selected):
            row[column] = selected
            ignored.update(
                item.value
                for item in support_values
                if item.value != selected and _present(item.value)
            )
            if ignored:
                warnings.append(
                    _row_span_warning(
                        "row_assembly",
                        _ignored_consensus_message(column, ignored),
                        candidates,
                    )
                )
            continue

        if not unresolved:
            continue
        if accepted_values and column in _CRITICAL_CONFLICT_COLUMNS:
            critical_conflicts[column] = unresolved
            warnings.append(
                _row_span_warning(
                    "row_assembly",
                    f"Rejected conflicting export evidence for {column}: "
                    f"{_joined_values(unresolved)}.",
                    candidates,
                )
            )
            row[column] = None
            continue

        warnings.append(
            _row_span_warning(
                "row_assembly",
                _ignored_consensus_message(column, unresolved),
                candidates,
            )
        )
        if column in {"cp", "display_name"}:
            row[column] = None
    _enforce_iv_complete_invariant(
        row,
        lambda message: warnings.append(
            _row_span_warning("row_assembly", message, candidates)
        ),
    )
    if critical_conflicts:
        return _RowCandidate(
            row,
            identity_key,
            warnings,
            source_detail_frame_indexes=_candidate_source_frame_indexes(
                candidates, "detail"
            ),
            source_appraisal_frame_indexes=_candidate_source_frame_indexes(
                candidates, "appraisal"
            ),
        )

    _apply_derived_flags(row)
    _add_missing_moves_warning(row, candidates, warnings)
    return _RowCandidate(
        row,
        identity_key,
        warnings,
        accepted=True,
        anchor_kind=_merged_anchor_kind(candidates),
        scan_start_frame_index=max(
            candidate.scan_start_frame_index for candidate in candidates
        ),
        fragment_types={
            fragment_type
            for candidate in candidates
            for fragment_type in candidate.fragment_types
        },
        source_detail_frame_indexes=_candidate_source_frame_indexes(
            candidates, "detail"
        ),
        source_appraisal_frame_indexes=_candidate_source_frame_indexes(
            candidates, "appraisal"
        ),
    )


def _candidate_source_frame_indexes(
    candidates: Iterable[_RowCandidate],
    fragment_type: str,
) -> tuple[int, ...]:
    frame_indexes: set[int] = set()
    for candidate in candidates:
        if fragment_type == "detail":
            frame_indexes.update(candidate.source_detail_frame_indexes)
        elif fragment_type == "appraisal":
            frame_indexes.update(candidate.source_appraisal_frame_indexes)
    return tuple(sorted(frame_indexes))


def _merged_anchor_kind(candidates: Iterable[_RowCandidate]) -> str:
    anchor_kinds = {candidate.anchor_kind for candidate in candidates}
    if "detail" in anchor_kinds and "appraisal" in anchor_kinds:
        return "detail+appraisal"
    if "appraisal" in anchor_kinds:
        return "appraisal"
    if "detail" in anchor_kinds:
        return "detail"
    if "conflict" in anchor_kinds:
        return "conflict"
    return "support"


def _has_normal_moves(row: dict[str, ExportValue]) -> bool:
    return all(_present(row[column]) for column in _NORMAL_MOVE_COLUMNS)


def _add_missing_moves_warning(
    row: dict[str, ExportValue],
    candidates: list[_RowCandidate],
    warnings: list[ExportWarning],
) -> None:
    if _has_normal_moves(row):
        return
    detail_candidates = [
        candidate for candidate in candidates if "detail" in candidate.fragment_types
    ]
    move_candidates = [
        candidate for candidate in detail_candidates if _has_normal_moves(candidate.row)
    ]
    if move_candidates:
        reason = (
            "missing moves: rejected candidate - move-bearing detail "
            "candidate(s) were considered but not merged: "
            f"{_candidate_frame_summary(move_candidates)}."
        )
    elif detail_candidates:
        reason = (
            "missing moves: rejected candidate - matching detail candidate(s) "
            "existed but did not contain resolved moves: "
            f"{_candidate_frame_summary(detail_candidates)}."
        )
    else:
        reason = "missing moves: no candidate - no matching detail candidate existed."
    warnings.append(_row_span_warning("row_assembly", reason, candidates))


def _candidate_frame_summary(candidates: Iterable[_RowCandidate]) -> str:
    spans = []
    for candidate in candidates:
        first_frame = candidate.row["first_frame_index"]
        last_frame = candidate.row["last_frame_index"]
        if first_frame == last_frame:
            spans.append(str(first_frame))
        else:
            spans.append(f"{first_frame}-{last_frame}")
    return ", ".join(spans)


def _candidate_column_values(
    candidates: Iterable[_RowCandidate],
    column: str,
) -> list[_FragmentColumnValue]:
    values: list[_FragmentColumnValue] = []
    for candidate in candidates:
        if column == "cp":
            counts = candidate.column_value_counts.get(column, Counter())
            if counts:
                for value, evidence_count in counts.items():
                    if not _present(value):
                        continue
                    for _index in range(evidence_count):
                        values.append(
                            _FragmentColumnValue(
                                value=value,
                                fragment_type=_candidate_fragment_type(candidate),
                                frame_index=candidate.scan_start_frame_index,
                            )
                        )
                continue
        value = candidate.row[column]
        if not _present(value):
            continue
        values.append(
            _FragmentColumnValue(
                value=value,
                fragment_type=_candidate_fragment_type(candidate),
                frame_index=candidate.scan_start_frame_index,
            )
        )
    return values


def _appraisal_identity_value(
    values: list[_FragmentColumnValue],
) -> tuple[ExportValue | None, set[ExportValue]]:
    appraisal_values = [
        item.value
        for item in values
        if item.fragment_type == "appraisal" and _present(item.value)
    ]
    if not appraisal_values:
        return None, set()
    counted = Counter(appraisal_values)
    if len(counted) != 1:
        return None, set()
    selected = next(iter(counted))
    ignored = {item.value for item in values if _present(item.value)} - {selected}
    return selected, ignored


def _candidate_fragment_type(candidate: _RowCandidate) -> str:
    if "detail" in candidate.fragment_types:
        return "detail"
    if "appraisal" in candidate.fragment_types:
        return "appraisal"
    return ""


def _support_cp_for_noisy_low_appraisal(
    accepted_values: list[_FragmentColumnValue],
    support_values: list[_FragmentColumnValue],
) -> ExportValue | None:
    accepted_selected, _accepted_ignored, _accepted_unresolved = (
        _select_cp_consensus_value(item.value for item in accepted_values)
    )
    support_selected, _support_ignored, _support_unresolved = (
        _select_cp_consensus_value(item.value for item in support_values)
    )
    if not (
        isinstance(accepted_selected, int)
        and not isinstance(accepted_selected, bool)
        and isinstance(support_selected, int)
        and not isinstance(support_selected, bool)
    ):
        return None
    if accepted_selected < 100 <= support_selected:
        return support_selected
    return None


def _select_consensus_value(
    values: Iterable[ExportValue],
) -> tuple[ExportValue | None, set[ExportValue], set[ExportValue]]:
    present_values = [value for value in values if _present(value)]
    if not present_values:
        return None, set(), set()

    counted = Counter(present_values)
    if len(counted) == 1:
        return present_values[0], set(), set()

    ranked = counted.most_common()
    selected, selected_count = ranked[0]
    runner_up_count = ranked[1][1]
    if (
        selected_count >= _CONSENSUS_MIN_COUNT
        and selected_count >= runner_up_count * _CONSENSUS_MIN_RATIO
    ):
        return selected, set(counted) - {selected}, set()
    return None, set(), set(counted)


def _select_appraisal_star_count_value(
    values: list[_FragmentColumnValue],
    related_values: dict[str, list[_FragmentColumnValue]],
) -> tuple[ExportValue | None, set[ExportValue], set[ExportValue]]:
    raw_values = [item.value for item in values]
    iv_sum_values = [item.value for item in related_values.get("iv_sum", ())]
    iv_sum, _ignored_iv, _unresolved_iv = _select_consensus_value(iv_sum_values)
    if isinstance(iv_sum, int) and not isinstance(iv_sum, bool):
        expected_star_count = _appraisal_star_count_for_iv_sum(iv_sum)
        counted = Counter(raw_values)
        if expected_star_count in counted:
            return expected_star_count, set(counted) - {expected_star_count}, set()

    return _select_consensus_value(raw_values)


def _appraisal_star_count_for_iv_sum(iv_sum: int) -> int:
    if iv_sum >= 37:
        return 3
    if iv_sum >= 30:
        return 2
    if iv_sum >= 23:
        return 1
    return 0


def _select_cp_consensus_value(
    values: Iterable[ExportValue],
) -> tuple[ExportValue | None, set[ExportValue], set[ExportValue]]:
    selected, ignored, unresolved = select_cp_consensus_value(values)
    return (
        selected,
        cast(set[ExportValue], ignored),
        cast(set[ExportValue], unresolved),
    )


def _ignored_consensus_message(
    column: str,
    ignored_values: Iterable[ExportValue],
) -> str:
    if column == "cp":
        return (
            "Ignored conflicting optional export evidence for cp: "
            f"{_joined_values(ignored_values)}."
        )
    if column in _WEAK_CONFLICT_COLUMNS:
        return (
            f"Ignored conflicting weak export evidence for {column}: "
            f"{_joined_values(ignored_values)}."
        )
    return (
        f"Ignored conflicting export evidence for {column} after consensus: "
        f"{_joined_values(ignored_values)}."
    )


def _first_present_row_value(
    candidates: Iterable[_RowCandidate], column: str
) -> ExportValue:
    for candidate in candidates:
        value = candidate.row[column]
        if _present(value):
            return value
    return None


def _add_source_span(
    row: dict[str, ExportValue], fragments: list[PokemonFragment]
) -> None:
    first = fragments[0]
    row["source_file"] = first.source_file
    row["source_type"] = first.source_type
    row["first_frame_index"] = min(fragment.frame_index for fragment in fragments)
    row["last_frame_index"] = max(fragment.frame_index for fragment in fragments)
    row["first_timestamp_s"] = round(
        min(fragment.timestamp_s for fragment in fragments), 6
    )
    row["last_timestamp_s"] = round(
        max(fragment.timestamp_s for fragment in fragments), 6
    )


def _span_warning(
    kind: str, message: str, fragments: list[PokemonFragment]
) -> ExportWarning:
    return ExportWarning(
        kind,
        message,
        fragments[0].source_file,
        min(fragment.frame_index for fragment in fragments),
        max(fragment.frame_index for fragment in fragments),
    )


def _row_span_warning(
    kind: str, message: str, candidates: list[_RowCandidate]
) -> ExportWarning:
    return ExportWarning(
        kind,
        message,
        str(candidates[0].row["source_file"] or ""),
        min(
            _required_int_row_value(candidate.row, "first_frame_index")
            for candidate in candidates
        ),
        max(
            _required_int_row_value(candidate.row, "last_frame_index")
            for candidate in candidates
        ),
    )


def _has_strong_corroboration(row: dict[str, ExportValue]) -> bool:
    if _present(row["cp"]):
        return True
    if all(
        _present(row[column]) for column in ("iv_attack", "iv_defense", "iv_stamina")
    ):
        return True
    if all(
        _present(row[column])
        for column in ("canonical_name", "catch_date", "catch_location")
    ):
        return True
    if (
        _present(row["fast_move_key"]) and _present(row["charged_move_key"])
    ) or _present(row["max_move_key"]):
        return True
    return row["has_tag_chips"] is True


def _has_anchor_identity(row: dict[str, ExportValue]) -> bool:
    if _present(row["species_key"]) or _present(row["pokedex_id"]):
        return True
    if all(
        _present(row[column])
        for column in ("canonical_name", "catch_date", "catch_location")
    ):
        return True
    if (
        _present(row["fast_move_key"]) and _present(row["charged_move_key"])
    ) or _present(row["max_move_key"]):
        return True
    return False


def _identity_key(row: dict[str, ExportValue]) -> IdentityKey | None:
    source_file = row["source_file"]
    hp_current = row["hp_current"]
    hp_max = row["hp_max"]
    weight = row["weight_kg"]
    if not (
        isinstance(source_file, str)
        and isinstance(hp_current, int)
        and isinstance(hp_max, int)
        and isinstance(weight, int | float)
    ):
        return None
    return (
        "physical",
        source_file,
        str(hp_current),
        str(hp_max),
        f"{float(weight):.3f}",
    )


def _log_bounded_complete_iv_extraction(
    live_log: _ExportLiveLogger, scan_results: Iterable[ProductionSequenceScanResult]
) -> None:
    for result in scan_results:
        for record in result.records:
            if not record.features.get("has_iv_complete"):
                continue
            values = [record.values.get(column) for column in _IV_TRIPLET_COLUMNS]
            if not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in values
            ):
                continue
            live_log.dump(
                "bounded structured IV extracted "
                f"frame={record.frame_index} "
                f"iv={values[0]}/{values[1]}/{values[2]} "
                f"sum={record.values.get('iv_sum')}"
            )


def _log_bounded_row_iv_summary(
    live_log: _ExportLiveLogger, rows: Iterable[dict[str, ExportValue]]
) -> None:
    for row in rows:
        source_file = row.get("source_file") or ""
        species = row.get("species_key") or row.get("canonical_name") or ""
        iv_values = [row.get(column) for column in _IV_TRIPLET_COLUMNS]
        if all(_present(value) for value in iv_values):
            live_log.dump(
                "bounded row IV merged "
                f"source={source_file} species={species} "
                f"iv={iv_values[0]}/{iv_values[1]}/{iv_values[2]} "
                f"sum={row.get('iv_sum')}"
            )
            continue
        live_log.dump(
            "bounded row missing IV "
            f"source={source_file} species={species} "
            f"iv_complete={row.get('iv_complete')} "
            f"iv={iv_values[0]}/{iv_values[1]}/{iv_values[2]} "
            f"sum={row.get('iv_sum')}"
        )


def _enriched_fragments(
    records: Iterable[FrameScanRecord],
    catalog: MetadataCatalog,
) -> list[PokemonFragment]:
    fragments = extract_fragments(records)
    enrich_fragments_with_species(fragments, catalog)
    enrich_fragments_with_moves(fragments, catalog)
    return fragments


def _scan_operation_summary(
    scan_results: Iterable[ProductionSequenceScanResult],
    lifecycle_events: Iterable[FrameLifecycleEvent],
) -> dict[str, object]:
    timing_totals: dict[str, float] = {}
    timing_counts: Counter[str] = Counter()
    ocr_group_counts: Counter[str] = Counter()
    ocr_field_counts: Counter[str] = Counter()
    slowest_frames: list[dict[str, object]] = []
    sequence_rows: list[dict[str, object]] = []
    scanned_record_count = 0

    for sequence_index, result in enumerate(scan_results):
        sequence_total_s = 0.0
        sequence_ocr_s = 0.0
        frame_indexes: list[int] = []
        for record in result.records:
            scanned_record_count += 1
            frame_indexes.append(record.frame_index)
            total_s = _record_timing_value(record, "total_s")
            ocr_s = _record_timing_value(record, "ocr_s")
            sequence_total_s += total_s
            sequence_ocr_s += ocr_s
            for key in ("image_load_s", "visual_analysis_s", "ocr_s", "total_s"):
                value = _record_timing_value(record, key)
                if value:
                    timing_totals[key] = timing_totals.get(key, 0.0) + value
                    timing_counts[key] += 1
            slowest_frames.append(
                {
                    "source_file": record.source_file,
                    "frame_index": record.frame_index,
                    "raw_classification": record.raw_classification,
                    "total_s": round(total_s, 6),
                    "ocr_s": round(ocr_s, 6),
                }
            )
        for fields in result.requested_ocr_fields_by_frame.values():
            group = ",".join(fields) if fields else "no_ocr_fields"
            ocr_group_counts[group] += 1
            for field_name in fields:
                ocr_field_counts[field_name] += 1
        sequence_rows.append(
            {
                "sequence_index": sequence_index,
                "sequence_type": result.sequence_type,
                "completed": result.completed,
                "record_count": len(result.records),
                "first_frame_index": min(frame_indexes) if frame_indexes else None,
                "last_frame_index": max(frame_indexes) if frame_indexes else None,
                "total_s": round(sequence_total_s, 6),
                "ocr_s": round(sequence_ocr_s, 6),
                "completion_reason": result.completion_reason,
            }
        )

    return {
        "scanned_record_count": scanned_record_count,
        "record_timing_totals_s": _rounded_counter(timing_totals),
        "record_timing_averages_s": {
            key: round(timing_totals[key] / timing_counts[key], 6)
            for key in sorted(timing_totals)
            if timing_counts[key]
        },
        "requested_ocr_field_groups": dict(ocr_group_counts.most_common(20)),
        "requested_ocr_fields": dict(ocr_field_counts.most_common(20)),
        "frame_probe_groups": _frame_probe_group_summary(lifecycle_events),
        "slowest_frames": sorted(
            slowest_frames,
            key=_slowest_frame_sort_key,
        )[:10],
        "top_expensive_sequences": sorted(
            sequence_rows,
            key=_sequence_summary_sort_key,
        )[:10],
    }


def _slowest_frame_sort_key(item: dict[str, object]) -> tuple[float, str, int]:
    total_s = item.get("total_s")
    frame_index = item.get("frame_index")
    return (
        -total_s if isinstance(total_s, (int, float)) else 0.0,
        str(item.get("source_file", "")),
        frame_index if isinstance(frame_index, int) else 0,
    )


def _sequence_summary_sort_key(item: dict[str, object]) -> tuple[float, int]:
    total_s = item.get("total_s")
    sequence_index = item.get("sequence_index")
    return (
        -total_s if isinstance(total_s, (int, float)) else 0.0,
        sequence_index if isinstance(sequence_index, int) else 0,
    )


def _record_timing_value(record: FrameScanRecord, key: str) -> float:
    value = record.timing.get(key, 0.0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    return float(value)


def _frame_probe_group_summary(
    events: Iterable[FrameLifecycleEvent],
) -> dict[str, object]:
    processed: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    deleted: Counter[str] = Counter()
    for event in events:
        if event.action == "processed" and event.phase in {"sequence", "repair"}:
            processed[event.reason] += 1
        elif event.action == "skipped" and event.phase in {"sequence", "repair"}:
            skipped[event.reason] += 1
        elif event.action == "deleted":
            deleted[event.reason] += 1
    return {
        "processed": dict(processed.most_common(20)),
        "skipped": dict(skipped.most_common(20)),
        "deleted": dict(deleted.most_common(20)),
    }


def _export_performance_summary(
    *,
    settings: ScanSettings,
    report: ExportReport,
    phase_totals_s: dict[str, float],
    run_total_s: float,
) -> dict[str, object]:
    return {
        "total_runtime_s": round(run_total_s, 6),
        "phase_totals_s": {
            key: round(value, 6)
            for key, value in sorted(phase_totals_s.items(), key=lambda item: item[0])
        },
        "slowest_operation_groups": _slowest_operation_groups(report, phase_totals_s),
        "frames": {
            "total_extracted": report.frame_count,
            "visual_processed": report.visual_frame_count,
            "sequence_scanned": report.scanned_frame_count,
            "deleted": (
                report.deleted_list_or_non_extractable_frames
                + report.deleted_sequence_frames
                + report.deleted_unsequenced_visual_frames
            ),
            "retained": report.retained_frame_count,
        },
        "bounded_frame_files": {
            "enabled": report.bounded_extraction_enabled,
            "max_export_frame_files": report.max_export_frame_files,
            "peak_export_frame_files": report.peak_export_frame_files,
            "peak_frame_file_percent_of_total_frames": _percentage(
                report.peak_export_frame_files, report.frame_count
            ),
            "temporary_over_cap_frame_files": (
                max(0, report.peak_export_frame_files - report.max_export_frame_files)
                if report.max_export_frame_files > 0
                else 0
            ),
            "retained_frame_count": report.retained_frame_count,
            "retained_frame_percent_of_total_frames": _percentage(
                report.retained_frame_count, report.frame_count
            ),
            "soft_limit_exceeded": report.bounded_extraction_soft_limit_exceeded,
            "deleted_list_or_non_extractable_frames": (
                report.deleted_list_or_non_extractable_frames
            ),
            "deleted_sequence_frames": report.deleted_sequence_frames,
            "deleted_unsequenced_visual_frames": (
                report.deleted_unsequenced_visual_frames
            ),
            "chunk_count": len(report.bounded_chunk_events),
            "seeked_chunk_count": _bounded_chunk_method_count(
                report.bounded_chunk_events, "time_seek"
            ),
            "fallback_chunk_count": _bounded_chunk_method_count(
                report.bounded_chunk_events, "range_select"
            ),
            "chunks": report.bounded_chunk_events,
        },
        "workers": {
            "configured": "auto" if settings.workers is None else settings.workers,
            "auto_resolution": (
                "auto uses os.cpu_count() logical CPUs, capped by queued item count"
            ),
            "visual_worker_count": report.worker_count,
            "sequence_worker_count": report.sequence_worker_count,
            "repair_worker_count": report.repair_worker_count,
            "retry_count": report.retry_count,
            "sequence_retry_count": report.sequence_retry_count,
            "repair_retry_count": report.repair_retry_count,
            "summary_by_phase": _worker_event_summary(report.worker_events),
            "events": report.worker_events,
        },
        "sequences": {
            "sequence_count": report.sequence_count,
            "repaired_sequence_count": report.repaired_sequence_count,
            "unresolved_pokemon_like_sequence_count": (
                report.unresolved_pokemon_like_sequence_count
            ),
            "exported_row_count": len(report.rows),
        },
        "scan_operations": report.scan_operation_summary,
        "accuracy_note": (
            "Diagnostics are aggregated after existing extraction decisions and do not "
            "lower OCR or probe quality."
        ),
    }


def _slowest_operation_groups(
    report: ExportReport, phase_totals_s: dict[str, float]
) -> list[dict[str, object]]:
    call_counts = {
        "frame_extraction": (
            len(report.bounded_chunk_events)
            if report.bounded_chunk_events
            else len(report.processed_files)
        ),
        "visual_analysis": report.visual_frame_count,
        "sequence_grouping": max(1, len(report.bounded_chunk_events)),
        "sequence_scanning": report.sequence_count,
        "sequence_repair": report.repaired_sequence_count,
        "row_assembly": 1,
        "csv_writing": 1,
        "xlsx_writing": 1,
    }
    parallel_phases = {"visual_analysis", "sequence_scanning", "sequence_repair"}
    rows: list[dict[str, object]] = []
    for name, total_s in sorted(
        phase_totals_s.items(), key=lambda item: item[1], reverse=True
    ):
        call_count = call_counts.get(name, 1)
        rows.append(
            {
                "operation": name,
                "total_s": round(total_s, 6),
                "call_count": call_count,
                "average_s": round(total_s / call_count, 6) if call_count > 0 else None,
                "parallelized": name in parallel_phases,
            }
        )
    return rows[:12]


def _worker_event_summary(
    events: Iterable[dict[str, object]],
) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for event in events:
        phase = str(event.get("phase", "unknown"))
        phase_summary = summary.setdefault(
            phase,
            {
                "batch_count": 0,
                "retry_reduction_count": 0,
                "max_resolved_worker_count": 0,
                "max_active_worker_count": 0,
                "total_queued_items": 0,
            },
        )
        phase_summary["max_resolved_worker_count"] = max(
            phase_summary["max_resolved_worker_count"],
            _int_event_value(event, "resolved_worker_count"),
        )
        phase_summary["max_active_worker_count"] = max(
            phase_summary["max_active_worker_count"],
            _int_event_value(event, "active_worker_count"),
        )
        if event.get("event") == "batch_start":
            phase_summary["batch_count"] += 1
            phase_summary["total_queued_items"] += _int_event_value(
                event, "queued_item_count"
            )
        elif event.get("event") == "retry_reduced":
            phase_summary["retry_reduction_count"] += 1
    return summary


def _int_event_value(event: dict[str, object], key: str) -> int:
    value = event.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _rounded_counter(counter: Mapping[str, float]) -> dict[str, float]:
    return {key: round(value, 6) for key, value in sorted(counter.items())}


def _percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(100 * numerator / denominator, 3)


def _bounded_chunk_method_count(
    chunk_events: Iterable[dict[str, object]], method: str
) -> int:
    return sum(
        1
        for event in chunk_events
        if str(event.get("extraction_method") or "") == method
    )


def _rounded_optional_float(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 6)


def _write_export_artifacts(
    *,
    settings: ScanSettings,
    artifacts_dir: Path,
    report: ExportReport,
    source_payloads: dict[str, object],
    phase_totals_s: dict[str, float],
    run_total_s: float,
    rejected_sequence_count: int,
    live_log_path: Path,
) -> None:
    csv_path = settings.output_dir / "pokemon.csv"
    xlsx_path = settings.output_dir / "pokemon.xlsx"
    timing_path = artifacts_dir / "timing_profile.json"
    performance_summary_path = artifacts_dir / "performance_summary.json"
    manifest_path = artifacts_dir / "export_manifest.json"
    warnings_path = artifacts_dir / "warnings.jsonl"
    frame_lifecycle_path = artifacts_dir / "frame_lifecycle.jsonl"
    row_diagnostics_path = artifacts_dir / "row_diagnostics.jsonl"

    artifact_phase_totals = dict(phase_totals_s)
    artifact_started = time.perf_counter()

    def write_step(name: str, callback: Callable[[], None]) -> None:
        step_started = time.perf_counter()
        callback()
        artifact_phase_totals[name] = artifact_phase_totals.get(name, 0.0) + (
            time.perf_counter() - step_started
        )

    write_step("csv_writing", lambda: write_export_csv(csv_path, report.rows))
    write_step("xlsx_writing", lambda: write_export_xlsx(xlsx_path, report.rows))
    write_step(
        "warning_artifact_writing",
        lambda: _write_warnings_jsonl(warnings_path, report.warnings),
    )
    write_step(
        "frame_lifecycle_writing",
        lambda: _write_jsonl(
            frame_lifecycle_path,
            (event.to_json_dict() for event in report.frame_lifecycle_events),
        ),
    )
    write_step(
        "row_diagnostics_writing",
        lambda: _write_jsonl(row_diagnostics_path, report.row_diagnostics),
    )
    artifact_phase_totals["artifact_writing"] = time.perf_counter() - artifact_started
    run_total_with_artifacts = run_total_s + artifact_phase_totals["artifact_writing"]
    timing_profile = _export_timing_profile(
        report=report,
        phase_totals_s=artifact_phase_totals,
        run_total_s=run_total_with_artifacts,
    )
    report.timing_summary = timing_profile["timing_summary"]  # type: ignore[assignment]
    _write_json(timing_path, timing_profile)
    performance_summary = _export_performance_summary(
        settings=settings,
        report=report,
        phase_totals_s=artifact_phase_totals,
        run_total_s=run_total_with_artifacts,
    )
    _write_json(performance_summary_path, performance_summary)
    _write_json(
        manifest_path,
        {
            **build_input_manifest_payload(
                settings,
                processed_files=report.processed_files,
                failed_files=report.failed_files,
            ),
            "warning_count": len(report.warnings),
            "frame_count": report.frame_count,
            "visual_frame_count": report.visual_frame_count,
            "scanned_frame_count": report.scanned_frame_count,
            "max_export_frame_files": report.max_export_frame_files,
            "bounded_extraction_enabled": report.bounded_extraction_enabled,
            "peak_export_frame_files": report.peak_export_frame_files,
            "deleted_list_or_non_extractable_frames": (
                report.deleted_list_or_non_extractable_frames
            ),
            "deleted_sequence_frames": report.deleted_sequence_frames,
            "deleted_unsequenced_visual_frames": (
                report.deleted_unsequenced_visual_frames
            ),
            "retained_frame_count": report.retained_frame_count,
            "frame_lifecycle_summary": _frame_lifecycle_summary(
                report.frame_lifecycle_events
            ),
            "unresolved_pokemon_like_sequence_count": (
                report.unresolved_pokemon_like_sequence_count
            ),
            "bounded_extraction_soft_limit_exceeded": (
                report.bounded_extraction_soft_limit_exceeded
            ),
            "sequence_worker_count": report.sequence_worker_count,
            "sequence_retry_count": report.sequence_retry_count,
            "repair_worker_count": report.repair_worker_count,
            "repair_retry_count": report.repair_retry_count,
            "repaired_sequence_count": report.repaired_sequence_count,
            "sequence_count": report.sequence_count,
            "exported_row_count": len(report.rows),
            "rejected_sequence_count": rejected_sequence_count,
            "timing_summary": report.timing_summary,
            "sources": source_payloads,
            "artifacts": {
                "pokemon_csv": str(csv_path),
                "pokemon_xlsx": str(xlsx_path),
                "export_log": str(live_log_path),
                "warnings_jsonl": str(warnings_path),
                "frame_lifecycle_jsonl": str(frame_lifecycle_path),
                "row_diagnostics_jsonl": str(row_diagnostics_path),
                "timing_profile": str(timing_path),
                "performance_summary": str(performance_summary_path),
                "export_manifest": str(manifest_path),
            },
        },
    )


def _export_timing_profile(
    *,
    report: ExportReport,
    phase_totals_s: dict[str, float],
    run_total_s: float,
) -> dict[str, object]:
    return {
        "timing_summary": {
            "run_total_s": round(run_total_s, 6),
            "frame_count": report.frame_count,
            "visual_frame_count": report.visual_frame_count,
            "scanned_frame_count": report.scanned_frame_count,
            "max_export_frame_files": report.max_export_frame_files,
            "bounded_extraction_enabled": report.bounded_extraction_enabled,
            "peak_export_frame_files": report.peak_export_frame_files,
            "deleted_list_or_non_extractable_frames": (
                report.deleted_list_or_non_extractable_frames
            ),
            "deleted_sequence_frames": report.deleted_sequence_frames,
            "deleted_unsequenced_visual_frames": (
                report.deleted_unsequenced_visual_frames
            ),
            "retained_frame_count": report.retained_frame_count,
            "frame_lifecycle_summary": _frame_lifecycle_summary(
                report.frame_lifecycle_events
            ),
            "unresolved_pokemon_like_sequence_count": (
                report.unresolved_pokemon_like_sequence_count
            ),
            "bounded_extraction_soft_limit_exceeded": (
                report.bounded_extraction_soft_limit_exceeded
            ),
            "worker_count": report.worker_count,
            "retry_count": report.retry_count,
            "sequence_worker_count": report.sequence_worker_count,
            "sequence_retry_count": report.sequence_retry_count,
            "repair_worker_count": report.repair_worker_count,
            "repair_retry_count": report.repair_retry_count,
            "repaired_sequence_count": report.repaired_sequence_count,
            "sequence_count": report.sequence_count,
            "exported_row_count": len(report.rows),
            "warning_count": len(report.warnings),
        },
        "run_phase_totals_s": {
            key: round(value, 6)
            for key, value in sorted(phase_totals_s.items(), key=lambda item: item[0])
        },
    }


def _write_warnings_jsonl(path: Path, warnings: Iterable[ExportWarning]) -> None:
    _write_jsonl(path, (warning.to_json_dict() for warning in warnings))


def _write_jsonl(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True))
            handle.write("\n")


def _frame_lifecycle_summary(
    events: Iterable[FrameLifecycleEvent],
) -> dict[str, object]:
    action_counts: Counter[str] = Counter()
    reason_counts: dict[str, Counter[str]] = {}
    for event in events:
        action_counts[event.action] += 1
        reason_counts.setdefault(event.action, Counter())[event.reason] += 1
    return {
        "actions": dict(sorted(action_counts.items())),
        "reasons": {
            action: dict(sorted(counts.items()))
            for action, counts in sorted(reason_counts.items())
        },
    }


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _csv_row(row: dict[str, ExportValue]) -> dict[str, ExportValue | str]:
    return {column: _cell_value(row.get(column)) for column in EXPORT_COLUMNS}


def _cell_value(value: ExportValue) -> ExportValue | str:
    return "" if value is None else value


def _empty_row() -> dict[str, ExportValue]:
    row: dict[str, ExportValue] = {column: None for column in EXPORT_COLUMNS}
    for column in _FLAG_COLUMNS:
        row[column] = False
    return row


def _present(value: ExportValue) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))


def _required_int_row_value(row: dict[str, ExportValue], column: str) -> int:
    value = row[column]
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"Row column {column!r} must be numeric.")
    return int(value)


def _required_float_row_value(row: dict[str, ExportValue], column: str) -> float:
    value = row[column]
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"Row column {column!r} must be numeric.")
    return float(value)


def _joined_values(values: Iterable[ExportValue]) -> str:
    return ", ".join(str(value) for value in sorted(values, key=str))


def _source_file_name(source_asset: SourceAsset) -> str:
    return source_asset.source_name or source_asset.path.name


def _source_artifact_stem(source_file: str) -> str:
    return Path(source_file).stem or "frames_jsonl"
