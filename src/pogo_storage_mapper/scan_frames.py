from __future__ import annotations

import colorsys
import html
import json
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, SupportsInt, cast

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

from pogo_storage_mapper.extract import (
    IV_NUMERIC_FIELD_NAMES,
    PokemonFragment,
    enrich_fragments_with_moves,
    enrich_fragments_with_species,
    extract_fragments,
    story_text_is_complete,
    write_fragments_jsonl,
)
from pogo_storage_mapper.extract import (
    story_text_has_keywords as _story_text_has_keywords,
)
from pogo_storage_mapper.layout import (
    DYNAMAX_KEYWORDS,
    GIGANTAMAX_KEYWORDS,
    INITIAL_APPRAISAL_CP_REGIONS,
    INITIAL_APPRAISAL_HP_REGIONS,
    IV_AMBER_STAR_RATIO_MIN,
    IV_BAR_WINDOWS,
    IV_INACTIVE_STAR_GRAY_RATIO_MIN,
    IV_INCOMPLETE_NOTE,
    IV_RED_STAR_RATIO_MIN,
    IV_STAR_ZONES,
    LOWER_IV_BAR_WINDOWS,
    NUM_TRANSLATION,
    POWER_SECTION_CONTEXT_KEYWORDS,
    REGIONS,
    WEIGHT_CORRECTED_NOTE,
    WEIGHT_PROPAGATED_NOTE,
    DetailLayoutMode,
    SignalValue,
)
from pogo_storage_mapper.metadata import load_default_metadata_catalog
from pogo_storage_mapper.ocr import OcrResult, TesseractOcrEngine
from pogo_storage_mapper.runtime_support import (
    PhaseTimer,
    build_image_source_payload,
    build_input_manifest_payload,
    build_video_source_payload,
    execute_with_adaptive_retries,
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
VIDEO_EXTENSIONS = {".mp4"}
JSONL_EXTENSIONS = {".jsonl"}
MAX_POKEMON_GO_CP = 9366
NON_EXTRACTABLE_CLASS = "non_extractable"
HP_AREA_CARD_SPLIT_ROWS = (0.38, 0.395)
HP_AREA_CARD_SPLIT_MIN_RATIO = 0.04
HP_AREA_CARD_SPLIT_MIN_CONTENT_RATIO = 0.55
HP_AREA_CARD_SPLIT_MIN_ROW_HITS = 2
HORIZONTAL_CARD_GAP_MIN_RATIO = 0.05
IMAGE_FILENAME_INFO_KEY = "filename"
IMAGE_DEBUG_FILENAME_INFO_KEY = "debug_filename"

DETAIL_FEATURE_KEYS = (
    "has_CP",
    "has_display_name",
    "has_hp",
    "has_weight",
    "has_moves",
    "is_shadow",
    "has_dynamax",
    "has_gigantamax",
    "has_iv",
    "has_iv_complete",
    "has_story",
    "has_tag_chips",
    "has_height",
    "has_pokemon_art",
    "has_transition",
    "has_gender",
    "has_shiny",
    "has_lucky",
    "has_purified",
    "has_favorite",
    "has_costume_or_form_visual",
    "has_mega_or_primal_section",
    "has_scroll_position",
)
LIST_FEATURE_KEYS = (
    "has_list_grid",
    "has_list_cp",
    "has_list_display_name",
    "has_list_pokemon_art",
)
FEATURE_KEYS = DETAIL_FEATURE_KEYS + LIST_FEATURE_KEYS


@dataclass(slots=True)
class ScanSettings:
    input_path: Path
    output_dir: Path
    artifacts_dir: Path | None = None
    ocr_lang: str = "eng"
    ocr_mode: str = "balanced"
    workers: int | None = None
    max_frame_attempts: int = 3
    visible_crop: bool = False
    max_export_frame_files: int = 0


@dataclass(frozen=True, slots=True)
class SourceAsset:
    path: Path
    source_type: str
    source_name: str | None = None


@dataclass(frozen=True, slots=True)
class FrameCandidate:
    source_asset: SourceAsset
    frame_path: Path
    frame_index: int
    timestamp_s: float
    debug_frame_path: Path | None = None
    source_payload: dict[str, Any] | None = None


@dataclass(slots=True)
class VideoExtractionResult:
    frames: list[FrameCandidate]
    used_hwaccel: str
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class JsonlFrameLoadResult:
    frames: list[FrameCandidate]
    warnings: list[str] = field(default_factory=list)
    input_payload: dict[str, object] = field(default_factory=dict)
    source_payloads: dict[str, dict[str, object]] = field(default_factory=dict)


@dataclass(slots=True)
class _JsonlRowResult:
    frame: FrameCandidate | None = None
    source_file: str = ""
    source_type: str = ""
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class _DetailLayout:
    mode: DetailLayoutMode
    hp_bar_y: float | None = None
    hp_bar_score: float = 0.0


@dataclass(frozen=True, slots=True)
class _HpBarAnchorCandidate:
    y: float
    score: float
    row_count: int


@dataclass(slots=True)
class FrameScanRecord:
    source_file: str
    source_type: str
    frame_path: str
    frame_index: int
    timestamp_s: float
    classification: str
    raw_classification: str
    features: dict[str, bool]
    values: dict[str, object | None] = field(default_factory=dict)
    signals: dict[str, SignalValue] = field(default_factory=dict)
    ocr: dict[str, dict[str, object]] = field(default_factory=dict)
    timing: dict[str, float] = field(default_factory=dict)
    attempts: int = 1
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "source_file": self.source_file,
            "source_type": self.source_type,
            "frame_path": self.frame_path,
            "frame_index": self.frame_index,
            "timestamp_s": round(self.timestamp_s, 6),
            "classification": self.classification,
            "raw_classification": self.raw_classification,
            "features": self.features,
            "values": self.values,
            "signals": self.signals,
            "ocr": self.ocr,
            "timing": self.timing,
            "attempts": self.attempts,
            "notes": self.notes,
        }
        if self.error is not None:
            payload["error"] = self.error
        return payload


@dataclass(slots=True)
class ScanReport:
    records: list[FrameScanRecord] = field(default_factory=list)
    fragments: list[PokemonFragment] = field(default_factory=list)
    processed_files: list[Path] = field(default_factory=list)
    failed_files: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    worker_count: int = 1
    timing_summary: dict[str, object] = field(default_factory=dict)

    def summary_line(self) -> str:
        counts = Counter(record.classification for record in self.records)
        class_counts = ", ".join(
            f"{name}={counts[name]}"
            for name in ("list", "detail", "appraisal", NON_EXTRACTABLE_CLASS)
        )
        return (
            f"Scanned {len(self.processed_files)} source file(s), "
            f"{len(self.records)} frame(s) ({class_counts}), "
            f"failed {len(self.failed_files)} source file(s)."
        )


@dataclass(slots=True)
class _FrameProcessingResult:
    records: list[FrameScanRecord]
    warnings: list[str]
    worker_count: int
    retry_count: int


@dataclass(slots=True)
class FrameVisualRecord:
    frame: FrameCandidate
    source_file: str
    source_type: str
    frame_path: str
    frame_index: int
    timestamp_s: float
    raw_classification: str
    signals: dict[str, SignalValue]
    iv_evidence: _IvEvidence | None = None
    moves_ocr_box: list[float] = field(default_factory=list)
    motion_sample: Image.Image | None = field(default=None, repr=False)
    timing: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class ProductionSequenceScanResult:
    records: list[FrameScanRecord]
    accepted_fields: dict[str, object]
    desired_fields: set[str]
    requested_ocr_fields_by_frame: dict[int, tuple[str, ...]]
    warnings: list[str] = field(default_factory=list)
    completed: bool = False
    sequence_type: str = ""
    completion_reason: str = ""


@dataclass(frozen=True, slots=True)
class _IvEvidence:
    attack: int | None
    defense: int | None
    stamina: int | None
    iv_sum: int | None
    star_count: int | None
    badge_visible: bool
    perfect: bool
    star_agreement: bool
    panel_visible: bool
    seal_visible: bool
    bar_count: int
    panel_light_ratio: float
    seal_color_ratio: float

    @property
    def has_iv(self) -> bool:
        return self.panel_visible and self.seal_visible and self.bar_count >= 1

    @property
    def has_iv_complete(self) -> bool:
        return self.has_iv and self.bar_count == 3 and self.star_agreement


@dataclass(frozen=True, slots=True)
class _VisualScanAnalysis:
    signals: dict[str, SignalValue]
    iv_evidence: _IvEvidence
    raw_classification: str
    moves_ocr_box: list[float]
    duration_s: float


@dataclass(frozen=True, slots=True)
class _VisualSequenceAnalysis:
    signals: dict[str, SignalValue]
    raw_classification: str
    duration_s: float


@dataclass(frozen=True, slots=True)
class _ParsedOcrValues:
    cp: int | None
    hp: str | None
    weight: str | None
    height: str | None
    story_text: str
    move_text: str
    special_text: str


PRODUCTION_APPRAISAL_USEFUL_FIELD_NAMES = frozenset(
    (
        "cp",
        "display_name",
        "hp",
        "weight",
        "story",
        "iv",
        "iv_star_agreement",
        "iv_sum",
        "appraisal_star_count",
        "appraisal_perfect",
        "tag",
    )
)
PRODUCTION_DETAIL_BASE_USEFUL_FIELD_NAMES = frozenset(
    ("cp", "display_name", "hp", "weight", "height", "tag")
)
PRODUCTION_DETAIL_VISUAL_USEFUL_FIELD_NAMES = frozenset(("moves", "power"))
PRODUCTION_NON_CONFLICTING_FIELD_NAMES = frozenset(
    (
        "display_name",
        "height",
        "moves",
        "power",
        "tag",
        "is_shadow",
        "has_dynamax",
        "has_gigantamax",
        "iv_sum",
        "appraisal_star_count",
        "appraisal_perfect",
    )
)
PRODUCTION_REPAIR_MAX_FRAMES = 18
PRODUCTION_CP_PROBE_FRAME_BUDGET = 3
PRODUCTION_PHYSICAL_PROBE_FRAME_BUDGET = 3
CP_CONSENSUS_MIN_COUNT = 3
CP_CONSENSUS_MIN_RATIO = 2
PRODUCTION_OCR_FIELDS_BY_EXPORT_FIELD: dict[str, tuple[str, ...]] = {
    "cp": ("cp",),
    "hp": ("hp",),
    "weight": ("weight",),
    "height": ("height",),
    "display_name": ("display_name",),
    "moves": ("moves", "special_sections"),
    "power": ("special_sections",),
    "story": ("story",),
    "iv": (),
    "iv_star_agreement": (),
    "tag": (),
    "is_shadow": (),
    "has_dynamax": ("special_sections",),
    "has_gigantamax": ("special_sections",),
}


def discover_inputs(input_path: Path) -> list[SourceAsset]:
    if not input_path.exists():
        msg = f"Input path does not exist: {input_path}"
        raise ValueError(msg)

    paths = (
        [input_path]
        if input_path.is_file()
        else sorted(path for path in input_path.rglob("*"))
    )
    assets: list[SourceAsset] = []
    for path in paths:
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        if input_path.is_file() and suffix in JSONL_EXTENSIONS:
            assets.append(SourceAsset(path, "frames_jsonl"))
        elif suffix in VIDEO_EXTENSIONS:
            assets.append(SourceAsset(path, "video"))
        elif suffix in IMAGE_EXTENSIONS:
            assets.append(SourceAsset(path, "image"))
    if not assets:
        msg = f"No supported inputs found under: {input_path}"
        raise ValueError(msg)
    return assets


def _artifact_dir(settings: ScanSettings) -> Path:
    return (
        settings.artifacts_dir
        if settings.artifacts_dir is not None
        else settings.output_dir / "artifacts"
    )


def _source_file_name(source_asset: SourceAsset) -> str:
    return source_asset.source_name or source_asset.path.name


def _source_artifact_stem(source_file: str) -> str:
    return Path(source_file).stem or "frames_jsonl"


def _build_ffmpeg_extract_command(
    source_path: Path,
    pattern: Path,
    *,
    hwaccel: str = "none",
) -> list[str]:
    command = ["ffmpeg", "-y"]
    if hwaccel == "nvidia":
        command.extend(["-hwaccel", "cuda"])
    command.extend(["-i", str(source_path), str(pattern)])
    return command


def _clear_frame_dir(frames_dir: Path) -> None:
    frames_dir.mkdir(parents=True, exist_ok=True)
    for frame_path in frames_dir.glob("frame_*"):
        if frame_path.is_file():
            frame_path.unlink()


def _run_ffmpeg(command: list[str], frames_dir: Path) -> None:
    _clear_frame_dir(frames_dir)
    subprocess.run(command, check=True, capture_output=True, text=True)


def _shorten_stderr(stderr: str) -> str:
    collapsed = " ".join(line.strip() for line in stderr.splitlines() if line.strip())
    return collapsed[:500]


def probe_video_duration(source_path: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(source_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        return max(0.0, float(completed.stdout.strip()))
    except ValueError:
        return 0.0


def extract_video_frames(
    source_asset: SourceAsset, frames_dir: Path
) -> VideoExtractionResult:
    pattern = frames_dir / "frame_%06d.png"
    warnings: list[str] = []
    used_hwaccel = "none"
    existing_frame_paths = sorted(frames_dir.glob("frame_*.png"))
    if existing_frame_paths:
        warnings.append(
            f"{source_asset.path.name}: reused existing extracted frame artifacts."
        )
        duration = probe_video_duration(source_asset.path)
        return VideoExtractionResult(
            _frame_candidates(source_asset, existing_frame_paths, duration),
            "cached",
            warnings,
        )

    nvidia_command = _build_ffmpeg_extract_command(
        source_asset.path, pattern, hwaccel="nvidia"
    )
    cpu_command = _build_ffmpeg_extract_command(source_asset.path, pattern)

    try:
        _run_ffmpeg(nvidia_command, frames_dir)
        used_hwaccel = "nvidia"
    except (OSError, subprocess.CalledProcessError) as exc:
        warnings.append(
            f"{source_asset.path.name}: NVIDIA/CUDA FFmpeg extraction unavailable; "
            "fell back to CPU extraction."
        )
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            warnings.append(_shorten_stderr(exc.stderr))
        _run_ffmpeg(cpu_command, frames_dir)

    frame_paths = sorted(frames_dir.glob("frame_*.png"))
    duration = probe_video_duration(source_asset.path)
    return VideoExtractionResult(
        _frame_candidates(source_asset, frame_paths, duration),
        used_hwaccel,
        warnings,
    )


def _frame_candidates(
    source_asset: SourceAsset,
    frame_paths: list[Path],
    duration: float,
) -> list[FrameCandidate]:
    denominator = max(1, len(frame_paths) - 1)
    return [
        FrameCandidate(
            source_asset=source_asset,
            frame_path=frame_path,
            frame_index=index,
            timestamp_s=(duration * index / denominator) if duration else float(index),
        )
        for index, frame_path in enumerate(frame_paths)
    ]


def copy_image_frame(
    source_asset: SourceAsset, frames_dir: Path
) -> list[FrameCandidate]:
    _clear_frame_dir(frames_dir)
    suffix = source_asset.path.suffix.lower() if source_asset.path.suffix else ".png"
    frame_path = frames_dir / f"frame_000000{suffix}"
    shutil.copy2(source_asset.path, frame_path)
    return [FrameCandidate(source_asset, frame_path, 0, 0.0)]


def _row_string(row: dict[str, Any], key: str, default: str) -> str:
    value = row.get(key)
    if isinstance(value, str) and value:
        return value
    return default


def _row_int(row: dict[str, Any], key: str, default: int) -> int:
    value = row.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _row_float(row: dict[str, Any], key: str, default: float) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _resolve_jsonl_frame_path(raw_path: str, jsonl_parent: Path) -> Path | None:
    frame_path = Path(raw_path)
    if frame_path.is_absolute():
        return frame_path.resolve() if frame_path.is_file() else None
    if frame_path.is_file():
        return frame_path.resolve()
    parent_relative = jsonl_parent / frame_path
    return parent_relative.resolve() if parent_relative.is_file() else None


def _jsonl_debug_frame_path(
    *, source_frames_dir: Path, source_frame_path: Path, row_number: int
) -> Path:
    suffix = source_frame_path.suffix or ".png"
    return source_frames_dir / f"frame_{row_number:06d}{suffix}"


def _jsonl_row_warning(source_asset: SourceAsset, row_number: int, reason: str) -> str:
    return f"{source_asset.path.name}: skipped row {row_number}; {reason}"


def _jsonl_frame_from_payload(
    raw_payload: dict[str, Any],
    *,
    source_asset: SourceAsset,
    row_number: int,
    jsonl_parent: Path,
    artifacts_dir: Path,
    cleared_frame_dirs: set[Path],
) -> _JsonlRowResult:
    raw_frame_path = raw_payload.get("frame_path")
    if not isinstance(raw_frame_path, str) or not raw_frame_path:
        return _JsonlRowResult(
            warning=_jsonl_row_warning(source_asset, row_number, "missing frame_path.")
        )

    resolved_frame_path = _resolve_jsonl_frame_path(raw_frame_path, jsonl_parent)
    if resolved_frame_path is None:
        return _JsonlRowResult(
            warning=_jsonl_row_warning(
                source_asset,
                row_number,
                f"frame image not found: {raw_frame_path}",
            )
        )

    suffix = resolved_frame_path.suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        return _JsonlRowResult(
            warning=_jsonl_row_warning(
                source_asset,
                row_number,
                f"unsupported frame image type: {resolved_frame_path.name}",
            )
        )

    frame_index = _row_int(raw_payload, "frame_index", row_number - 1)
    timestamp_s = _row_float(raw_payload, "timestamp_s", float(frame_index))
    source_file = _row_string(raw_payload, "source_file", resolved_frame_path.name)
    source_type = _row_string(raw_payload, "source_type", "image")
    source_frames_dir = artifacts_dir / _source_artifact_stem(source_file) / "frames"
    if source_frames_dir not in cleared_frame_dirs:
        _clear_frame_dir(source_frames_dir)
        cleared_frame_dirs.add(source_frames_dir)
    debug_frame_path = _jsonl_debug_frame_path(
        source_frames_dir=source_frames_dir,
        source_frame_path=resolved_frame_path,
        row_number=row_number,
    )
    return _JsonlRowResult(
        frame=FrameCandidate(
            SourceAsset(Path(source_file), source_type, source_file),
            resolved_frame_path,
            frame_index,
            timestamp_s,
            debug_frame_path,
            raw_payload,
        ),
        source_file=source_file,
        source_type=source_type,
    )


def _jsonl_source_payloads(
    *,
    source_counts: Counter[str],
    source_types: dict[str, str],
    source_asset: SourceAsset,
) -> dict[str, dict[str, object]]:
    return {
        source_file: {
            "source_type": source_types[source_file],
            "frame_count": frame_count,
            "input_kind": "frames_jsonl",
            "frame_storage": "referenced_originals",
            "source_jsonl": str(source_asset.path),
            "warnings": [],
        }
        for source_file, frame_count in source_counts.items()
    }


def load_jsonl_frame_candidates(
    source_asset: SourceAsset, artifacts_dir: Path
) -> JsonlFrameLoadResult:
    warnings: list[str] = []
    frames: list[FrameCandidate] = []
    source_counts: Counter[str] = Counter()
    source_types: dict[str, str] = {}
    cleared_frame_dirs: set[Path] = set()
    row_count = 0
    skipped_rows = 0
    jsonl_parent = source_asset.path.parent

    with source_asset.path.open(encoding="utf-8") as handle:
        for row_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row_count += 1
            try:
                raw_payload = json.loads(line)
            except json.JSONDecodeError as exc:
                skipped_rows += 1
                warnings.append(
                    _jsonl_row_warning(
                        source_asset, row_number, f"invalid JSON ({exc.msg})."
                    )
                )
                continue
            if not isinstance(raw_payload, dict):
                skipped_rows += 1
                warnings.append(
                    _jsonl_row_warning(
                        source_asset, row_number, "expected a JSON object."
                    )
                )
                continue

            row_result = _jsonl_frame_from_payload(
                raw_payload,
                source_asset=source_asset,
                row_number=row_number,
                jsonl_parent=jsonl_parent,
                artifacts_dir=artifacts_dir,
                cleared_frame_dirs=cleared_frame_dirs,
            )
            if row_result.warning is not None:
                skipped_rows += 1
                warnings.append(row_result.warning)
                continue

            if row_result.frame is not None:
                frames.append(row_result.frame)
            source_counts[row_result.source_file] += 1
            source_types.setdefault(row_result.source_file, row_result.source_type)

    source_payloads = _jsonl_source_payloads(
        source_counts=source_counts,
        source_types=source_types,
        source_asset=source_asset,
    )
    input_payload: dict[str, object] = {
        "source_type": source_asset.source_type,
        "input_kind": "frames_jsonl",
        "frame_storage": "referenced_originals",
        "row_count": row_count,
        "frame_count": len(frames),
        "skipped_rows": skipped_rows,
        "warnings": warnings,
    }
    return JsonlFrameLoadResult(frames, warnings, input_payload, source_payloads)


def parse_cp_candidate(text: str | None) -> int | None:
    if not text:
        return None
    cleaned = re.sub(r"(?<=\d)\s+\|+\s*$", "", text)
    normalized = cleaned.translate(NUM_TRANSLATION)
    explicit = re.search(
        r"\bCP\s*[:#-]?\s*(?P<cp>\d{2,5})(?![\s,.]*\d)\b",
        normalized,
        re.IGNORECASE,
    )
    if explicit:
        return _valid_cp_value(explicit.group("cp"))

    fuzzy = re.search(
        r"(?:c+p+e?|c+e+p+|e+p+|c+e+)\s*(?P<cp>\d{2,5})(?![\s,.]*\d)",
        normalized.casefold(),
    )
    if fuzzy:
        return _valid_cp_value(fuzzy.group("cp"))
    if re.search(r"(?:c+p+e?|c+e+p+|e+p+|c+e+)", normalized.casefold()):
        return None

    numbers = [int(match) for match in re.findall(r"\b\d{3,5}\b", normalized)]
    plausible = [value for value in numbers if _valid_cp_value(value) is not None]
    return plausible[0] if plausible else None


def _valid_cp_value(value: str | int) -> int | None:
    cp = int(value)
    return cp if 10 <= cp <= MAX_POKEMON_GO_CP else None


def parse_hp_candidate(text: str | None) -> str | None:
    if not text:
        return None
    normalized = text.translate(NUM_TRANSLATION)
    match = re.search(
        r"(?P<numerator>\d{1,3})\s*/\s*(?P<denominator>\d{2,3})(?:\s*HP)?",
        normalized,
        re.IGNORECASE,
    )
    if match is None:
        return None
    numerator = int(match.group("numerator"))
    denominator = int(match.group("denominator"))
    if 0 <= numerator <= denominator and denominator >= 10:
        return f"{numerator}/{denominator}"
    return None


def parse_weight_candidate(text: str | None) -> str | None:
    if not text:
        return None
    normalized = text.replace(",", ".").casefold()
    matches = re.finditer(r"(?P<weight>\d{1,4}(?:\.\d{1,2})?)\s*kg\b", normalized)
    for match in matches:
        start = match.start("weight")
        if start > 0:
            prefix = normalized[start - 1]
            if not (prefix.isspace() or prefix in "(:;[{|"):
                continue
        value = float(match.group("weight"))
        if 0 < value <= 1000:
            return match.group("weight").rstrip("0").rstrip(".")
    return None


def parse_height_candidate(text: str | None) -> str | None:
    if not text:
        return None
    normalized = text.replace(",", ".").casefold()
    matches = re.finditer(r"(?P<height>\d{1,2}(?:\.\d{1,2})?)\s*m\b", normalized)
    for match in matches:
        start = match.start("height")
        if start > 0:
            prefix = normalized[start - 1]
            if not (prefix.isspace() or prefix in "(:;[{|"):
                continue
        value = float(match.group("height"))
        if 0 < value <= 100:
            return match.group("height").rstrip("0").rstrip(".")
    return None


def story_text_has_keywords(text: str | None) -> bool:
    return _story_text_has_keywords(text)


def _features() -> dict[str, bool]:
    return {key: False for key in FEATURE_KEYS}


def _safe_debug_suffix(value: str) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value.strip())
    return suffix or "crop"


def _crop_debug_path(image: Image.Image, save_as: str | Path) -> Path | None:
    if isinstance(save_as, Path):
        return save_as

    source_filename = image.info.get(IMAGE_DEBUG_FILENAME_INFO_KEY) or image.info.get(
        IMAGE_FILENAME_INFO_KEY
    )
    if not isinstance(source_filename, str) or not source_filename:
        return None

    source_path = Path(source_filename)
    suffix = _safe_debug_suffix(save_as)
    extension = source_path.suffix or ".png"
    return source_path.with_name(f"{source_path.stem}__{suffix}{extension}")


def _crop(
    image: Image.Image,
    box: list[float],
    save_as: str | Path | None = None,
) -> Image.Image:
    width, height = image.size
    x1 = max(0, min(width - 1, int(box[0] * width)))
    y1 = max(0, min(height - 1, int(box[1] * height)))
    x2 = max(x1 + 1, min(width, int(box[2] * width)))
    y2 = max(y1 + 1, min(height, int(box[3] * height)))

    if save_as is not None:
        debug_path = _crop_debug_path(image, save_as)
        if debug_path is not None:
            debug_path.parent.mkdir(parents=True, exist_ok=True)
            debug_image = image.copy()
            draw = ImageDraw.Draw(debug_image)
            line_width = max(2, min(width, height) // 160)
            draw.rectangle(
                (x1, y1, x2 - 1, y2 - 1),
                outline=(255, 0, 0),
                width=line_width,
            )
            debug_image.save(debug_path)

    return image.crop((x1, y1, x2, y2))


def _brightness(image: Image.Image) -> float:
    return ImageStat.Stat(ImageOps.grayscale(image)).mean[0] / 255


def _dark_ratio(image: Image.Image, *, threshold: int = 210) -> float:
    gray = ImageOps.grayscale(image)
    histogram = gray.histogram()
    return sum(histogram[:threshold]) / max(1, sum(histogram))


def _light_ratio(image: Image.Image, *, threshold: int = 220) -> float:
    gray = ImageOps.grayscale(image)
    histogram = gray.histogram()
    return sum(histogram[threshold:]) / max(1, sum(histogram))


def _edge_ratio(image: Image.Image, *, threshold: int = 32) -> float:
    image = _sample_image(image)
    edges = ImageOps.grayscale(image).filter(ImageFilter.FIND_EDGES)
    histogram = edges.histogram()
    return sum(histogram[threshold:]) / max(1, sum(histogram))


def _hsv_ratio(
    image: Image.Image,
    *,
    hue_min: float,
    hue_max: float,
    saturation_min: float,
    value_min: float,
) -> float:
    image = _sample_image(image)
    hsv = image.convert("HSV")
    pixels = _image_pixels(hsv)
    total = 0
    matched = 0
    for hue, saturation, value in pixels:
        total += 1
        normalized_hue = hue * 360 / 255
        normalized_saturation = saturation / 255
        normalized_value = value / 255
        if (
            hue_min <= normalized_hue <= hue_max
            and normalized_saturation >= saturation_min
            and normalized_value >= value_min
        ):
            matched += 1
    return matched / max(1, total)


def _green_ratio(image: Image.Image) -> float:
    return _hsv_ratio(
        image, hue_min=70, hue_max=165, saturation_min=0.12, value_min=0.50
    )


def _orange_ratio(image: Image.Image) -> float:
    return _hsv_ratio(
        image, hue_min=10, hue_max=55, saturation_min=0.30, value_min=0.45
    )


def _red_ratio(image: Image.Image) -> float:
    return _hsv_ratio(
        image, hue_min=340, hue_max=360, saturation_min=0.18, value_min=0.35
    ) + _hsv_ratio(image, hue_min=0, hue_max=10, saturation_min=0.18, value_min=0.35)


def _saturated_ratio(image: Image.Image) -> float:
    image = _sample_image(image)
    hsv = image.convert("HSV")
    pixels = _image_pixels(hsv)
    total = 0
    matched = 0
    for _hue, saturation, value in pixels:
        total += 1
        if saturation >= 45 and value >= 70:
            matched += 1
    return matched / max(1, total)


def _image_pixels(image: Image.Image):
    getter = getattr(image, "get_flattened_data", None)
    if callable(getter):
        return getter()
    return image.getdata()


def _sample_image(
    image: Image.Image, *, max_width: int = 180, max_height: int = 320
) -> Image.Image:
    width, height = image.size
    if width <= max_width and height <= max_height:
        return image
    ratio = min(max_width / width, max_height / height)
    new_size = (max(1, int(width * ratio)), max(1, int(height * ratio)))
    return image.resize(new_size, Image.Resampling.BILINEAR)


def _hp_bar_row_score(image: Image.Image, y: int, x_start: int, x_stop: int) -> float:
    matched = 0
    width = max(1, x_stop - x_start)
    for x in range(x_start, x_stop):
        red, green, blue = cast(tuple[int, int, int], image.getpixel((x, y)))
        high = max(red, green, blue)
        low = min(red, green, blue)
        saturation = (high - low) / max(1, high)
        brightness = high / 255
        colored_bar = saturation >= 0.08 and brightness >= 0.35
        if colored_bar:
            matched += 1
    return matched / width


def _hp_bar_anchor_candidates(image: Image.Image) -> list[_HpBarAnchorCandidate]:
    sampled = _sample_image(image, max_width=180, max_height=320).convert("RGB")
    width, height = sampled.size
    x_start = int(width * 0.18)
    x_stop = max(x_start + 1, int(width * 0.82))
    y_start = int(height * 0.10)
    y_stop = max(y_start + 1, int(height * 0.78))

    candidates: list[_HpBarAnchorCandidate] = []
    current_rows: list[tuple[int, float]] = []

    def flush_current_rows() -> None:
        if not current_rows:
            return
        row_count = len(current_rows)
        score = max(row_score for _row, row_score in current_rows)
        if score >= 0.18 and 2 <= row_count <= 16:
            first_row = current_rows[0][0]
            last_row = current_rows[-1][0]
            center_y = ((first_row + last_row) / 2) / max(1, height - 1)
            candidates.append(
                _HpBarAnchorCandidate(
                    y=center_y,
                    score=score,
                    row_count=row_count,
                )
            )
        current_rows.clear()

    for y in range(y_start, y_stop):
        score = _hp_bar_row_score(sampled, y, x_start, x_stop)
        if score >= 0.10:
            current_rows.append((y, score))
            continue
        flush_current_rows()
    flush_current_rows()

    return candidates


def _detect_hp_bar_anchor(image: Image.Image) -> tuple[float | None, float]:
    candidates = _hp_bar_anchor_candidates(image)
    if candidates:
        selected = candidates[0]
        return selected.y, selected.score

    sampled = _sample_image(image, max_width=180, max_height=320).convert("RGB")
    width, height = sampled.size
    x_start = int(width * 0.18)
    x_stop = max(x_start + 1, int(width * 0.82))
    y_start = int(height * 0.10)
    y_stop = max(y_start + 1, int(height * 0.78))
    best_score = max(
        (
            _hp_bar_row_score(sampled, y, x_start, x_stop)
            for y in range(y_start, y_stop)
        ),
        default=0.0,
    )
    return None, best_score


def _count_bright_row_bands(image: Image.Image) -> int:
    image = _sample_image(image, max_width=160, max_height=320)
    gray = ImageOps.grayscale(image)
    width, height = gray.size
    if height < 12:
        return 0
    row_hits: list[bool] = []
    for y in range(height):
        bright = 0
        for x in range(width):
            pixel = gray.getpixel((x, y))
            if isinstance(pixel, int | float) and pixel >= 218:
                bright += 1
        row_hits.append(bright / max(1, width) >= 0.35)

    bands = 0
    inside = False
    min_band_height = max(8, int(height * 0.035))
    current_height = 0
    for hit in row_hits:
        if hit:
            current_height += 1
            inside = True
        elif inside:
            if current_height >= min_band_height:
                bands += 1
            inside = False
            current_height = 0
    if inside and current_height >= min_band_height:
        bands += 1
    return bands


def _low_bright_column_run_ratio(image: Image.Image) -> float:
    image = _sample_image(image, max_width=180, max_height=180)
    gray = ImageOps.grayscale(image)
    width, height = gray.size
    if width < 12 or height < 12:
        return 0.0

    start = int(width * 0.05)
    stop = int(width * 0.95)
    current = 0
    longest = 0
    total_columns = max(1, stop - start)
    for x in range(start, stop):
        bright = 0
        for y in range(height):
            pixel = gray.getpixel((x, y))
            if isinstance(pixel, int | float) and pixel > 235:
                bright += 1
        if bright / height < 0.45:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest / total_columns


def _is_white_card_pixel(pixel: object) -> bool:
    red, green, blue = _rgb_channels(pixel)
    high = max(red, green, blue)
    low = min(red, green, blue)
    saturation = (high - low) / max(1, high)
    return high >= 235 and low >= 225 and saturation <= 0.10


def _is_hp_bar_pixel(pixel: object) -> bool:
    red, green, blue = _rgb_channels(pixel)
    hue, saturation, value = colorsys.rgb_to_hsv(
        red / 255,
        green / 255,
        blue / 255,
    )
    return 70 <= hue * 360 <= 165 and saturation >= 0.08 and value >= 0.35


def _is_hp_area_card_content_pixel(pixel: object) -> bool:
    return _is_white_card_pixel(pixel) or _is_hp_bar_pixel(pixel)


def _hp_area_card_gap_ratio_on_row(
    image: Image.Image, y: int, x_start: int, x_stop: int
) -> tuple[float, float]:
    longest = 0
    content_pixels = 0
    total_columns = max(1, x_stop - x_start)
    run_start: int | None = None

    def flush_run(end_exclusive: int) -> None:
        nonlocal longest, run_start
        if run_start is None:
            return
        run_length = end_exclusive - run_start
        if run_start > x_start and end_exclusive < x_stop:
            longest = max(longest, run_length)
        run_start = None

    for x in range(x_start, x_stop):
        if _is_hp_area_card_content_pixel(image.getpixel((x, y))):
            content_pixels += 1
            flush_run(x)
            continue
        if run_start is None:
            run_start = x
    flush_run(x_stop)
    return longest / total_columns, content_pixels / total_columns


def _hp_area_card_split_metrics(image: Image.Image) -> tuple[float, float]:
    sampled = _sample_image(image, max_width=180, max_height=320).convert("RGB")
    width, height = sampled.size
    if width < 12 or height < 12:
        return 0.0, 0.0

    x_start = int(width * 0.05)
    x_stop = max(x_start + 1, int(width * 0.95))
    best_ratio = 0.0
    best_y = 0.0
    hit_count = 0
    for normalized_y in HP_AREA_CARD_SPLIT_ROWS:
        y = max(0, min(height - 1, round(normalized_y * (height - 1))))
        gap_ratio, content_ratio = _hp_area_card_gap_ratio_on_row(
            sampled, y, x_start, x_stop
        )
        if content_ratio < HP_AREA_CARD_SPLIT_MIN_CONTENT_RATIO:
            continue
        if gap_ratio >= HP_AREA_CARD_SPLIT_MIN_RATIO:
            hit_count += 1
        if gap_ratio > best_ratio:
            best_ratio = gap_ratio
            best_y = normalized_y
    if hit_count < HP_AREA_CARD_SPLIT_MIN_ROW_HITS:
        return 0.0, 0.0
    return best_ratio, best_y


def _rgb_channels(pixel: object) -> tuple[int, int, int]:
    if isinstance(pixel, tuple):
        return int(pixel[0]), int(pixel[1]), int(pixel[2])
    value = int(cast(SupportsInt, pixel))
    return value, value, value


def _iv_fill_pixel(pixel: object) -> bool:
    red, green, blue = _rgb_channels(pixel)
    hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    hue_degrees = hue * 360
    return (
        saturation >= 0.18
        and value >= 0.35
        and (hue_degrees <= 15 or 15 <= hue_degrees <= 55 or hue_degrees >= 335)
    )


def _iv_track_pixel(pixel: object) -> bool:
    if _iv_fill_pixel(pixel):
        return True
    red, green, blue = _rgb_channels(pixel)
    brightest = max(red, green, blue)
    darkest = min(red, green, blue)
    return 65 <= brightest <= 235 and brightest - darkest <= 70


def _iv_label_signal(panel: Image.Image, y_start: int, y_stop: int) -> float:
    width, _height = panel.size
    x_start = int(width * 0.05)
    x_stop = int(width * 0.50)
    y_midpoint = y_start + max(1, (y_stop - y_start) // 2)
    total = max(1, (x_stop - x_start) * (y_midpoint - y_start))
    matched = 0
    for y in range(y_start, y_midpoint):
        for x in range(x_start, x_stop):
            if _iv_fill_pixel(panel.getpixel((x, y))):
                matched += 1
    return matched / total


def _iv_best_track_run(
    panel: Image.Image, *, y_start: int, y_stop: int, x_start: int, x_stop: int
) -> tuple[int, tuple[int, int]]:
    best_score = 0
    best_y = y_start
    best_run = (x_start, x_start)
    for y in range(y_start, y_stop):
        track_positions = [
            x for x in range(x_start, x_stop) if _iv_track_pixel(panel.getpixel((x, y)))
        ]
        if not track_positions:
            continue
        span = track_positions[-1] - track_positions[0] + 1
        score = span + len(track_positions)
        if score > best_score:
            best_score = score
            best_y = y
            best_run = (track_positions[0], track_positions[-1] + 1)
    return best_y, best_run


def _iv_fill_positions(
    panel: Image.Image,
    *,
    best_run: tuple[int, int],
    best_y: int,
    y_start: int,
    y_stop: int,
) -> list[int]:
    y_neighborhood_start = max(y_start, best_y - 3)
    y_neighborhood_stop = min(y_stop, best_y + 4)
    return [
        x
        for x in range(best_run[0], best_run[1])
        if any(
            _iv_fill_pixel(panel.getpixel((x, y)))
            for y in range(y_neighborhood_start, y_neighborhood_stop)
        )
    ]


def _iv_track_clusters_at_y(
    panel: Image.Image, *, y: int, x_start: int, x_stop: int
) -> list[tuple[int, int]]:
    track_positions = [
        x for x in range(x_start, x_stop) if _iv_track_pixel(panel.getpixel((x, y)))
    ]
    return _iv_fill_clusters(track_positions) if track_positions else []


def _iv_fill_clusters(fill_positions: list[int]) -> list[tuple[int, int]]:
    clusters: list[tuple[int, int]] = []
    cluster_start = fill_positions[0]
    previous = fill_positions[0]
    for position in fill_positions[1:]:
        if position == previous + 1:
            previous = position
            continue
        clusters.append((cluster_start, previous))
        cluster_start = position
        previous = position
    clusters.append((cluster_start, previous))
    return clusters


def _merge_close_iv_track_clusters(
    clusters: list[tuple[int, int]], *, best_width: int
) -> list[tuple[int, int]]:
    if not clusters:
        return []

    merged: list[tuple[int, int]] = []
    max_internal_gap = max(1, int(best_width * 0.005))
    for start, stop in clusters:
        if not merged:
            merged.append((start, stop))
            continue

        previous_start, previous_stop = merged[-1]
        if start - previous_stop - 1 <= max_internal_gap:
            merged[-1] = (previous_start, stop)
            continue

        merged.append((start, stop))
    return merged


def _iv_filled_width_from_clusters(
    clusters: list[tuple[int, int]], best_run: tuple[int, int], best_width: int
) -> int:
    allowed_start_offset = max(20, int(best_width * 0.12))
    allowed_gap = max(14, int(best_width * 0.06))
    filled_stop = best_run[0]
    for start, stop in clusters:
        if filled_stop == best_run[0]:
            if start > best_run[0] + allowed_start_offset:
                continue
            filled_stop = stop
            continue
        if start - filled_stop > allowed_gap:
            break
        filled_stop = stop
    return max(0, filled_stop - best_run[0] + 1)


def _iv_bar_segment_clusters(
    panel: Image.Image,
    *,
    best_y: int,
    best_run: tuple[int, int],
    best_width: int,
    x_start: int,
    x_stop: int,
) -> list[tuple[int, int]]:
    track_clusters = _iv_track_clusters_at_y(
        panel, y=best_y, x_start=x_start, x_stop=x_stop
    )
    track_clusters = _merge_close_iv_track_clusters(
        track_clusters, best_width=best_width
    )
    min_segment_width = max(24, int(best_width * 0.08))
    eligible_clusters = [
        cluster
        for cluster in track_clusters
        if cluster[1] - cluster[0] + 1 >= min_segment_width
    ]
    selected: list[tuple[int, int]] = []
    allowed_start_offset = max(24, int(best_width * 0.12))
    allowed_segment_gap = max(14, int(best_width * 0.06))

    for cluster in eligible_clusters:
        if not selected:
            if cluster[0] > best_run[0] + allowed_start_offset:
                continue
        else:
            gap = cluster[0] - selected[-1][1] - 1
            if gap > allowed_segment_gap:
                break
        selected.append(cluster)
        if len(selected) == 3:
            return selected

    if len(eligible_clusters) == 1:
        start, stop = eligible_clusters[0]
        width = stop - start + 1
        if width >= best_width * 0.55:
            return [
                (
                    start + round(width * index / 3),
                    start + round(width * (index + 1) / 3) - 1,
                )
                for index in range(3)
            ]

    start = best_run[0]
    width = max(1, best_width)
    return [
        (start + round(width * index / 3), start + round(width * (index + 1) / 3) - 1)
        for index in range(3)
    ]


def _iv_segment_points(fill_ratio: float) -> int:
    if fill_ratio >= 0.90:
        return 5
    if fill_ratio <= 0.08:
        return 0
    return max(0, min(5, int(fill_ratio * 5 + 0.5)))


def _decode_iv_bar(
    panel: Image.Image, vertical_window: tuple[float, float]
) -> int | None:
    width, height = panel.size
    if width < 20 or height < 20:
        return None

    y_start = int(height * vertical_window[0])
    y_stop = max(y_start + 1, int(height * vertical_window[1]))
    x_start = int(width * 0.04)
    x_stop = int(width * 0.96)
    best_y, best_run = _iv_best_track_run(
        panel, y_start=y_start, y_stop=y_stop, x_start=x_start, x_stop=x_stop
    )

    best_width = best_run[1] - best_run[0]
    if best_width < width * 0.35:
        return None

    fill_positions = _iv_fill_positions(
        panel,
        best_run=best_run,
        best_y=best_y,
        y_start=y_start,
        y_stop=y_stop,
    )
    fill_ratio = len(fill_positions) / max(1, best_width)
    label_signal = _iv_label_signal(panel, y_start, y_stop)
    if fill_ratio < 0.015 and label_signal < 0.030:
        return None
    if not fill_positions:
        return 0

    fill_position_set = set(fill_positions)
    segment_values: list[int] = []
    for start, stop in _iv_bar_segment_clusters(
        panel,
        best_y=best_y,
        best_run=best_run,
        best_width=best_width,
        x_start=x_start,
        x_stop=x_stop,
    ):
        segment_width = max(1, stop - start + 1)
        filled_columns = sum(
            1 for x in range(start, stop + 1) if x in fill_position_set
        )
        segment_values.append(_iv_segment_points(filled_columns / segment_width))

    return max(0, min(15, sum(segment_values)))


def _upper_iv_bar_track_signal(panel: Image.Image) -> int:
    width, height = panel.size
    x_start = int(width * 0.04)
    x_stop = int(width * 0.96)
    y_start = int(height * 0.02)
    y_stop = max(y_start + 1, int(height * 0.20))
    best_count = 0
    for y in range(y_start, y_stop):
        count = sum(
            1 for x in range(x_start, x_stop) if _iv_track_pixel(panel.getpixel((x, y)))
        )
        best_count = max(best_count, count)
    return best_count


def _iv_bar_windows_for_panel(
    panel: Image.Image,
) -> dict[str, tuple[float, float]]:
    if _upper_iv_bar_track_signal(panel) >= int(panel.size[0] * 0.45):
        return IV_BAR_WINDOWS
    return LOWER_IV_BAR_WINDOWS


def _iv_fill_ratio(image: Image.Image) -> float:
    pixels = _image_pixels(_sample_image(image, max_width=80, max_height=80))
    total = 0
    matched = 0
    for pixel in pixels:
        total += 1
        if _iv_fill_pixel(pixel):
            matched += 1
    return matched / max(1, total)


def _iv_inactive_star_ratio(image: Image.Image) -> float:
    pixels = _image_pixels(
        _sample_image(image, max_width=80, max_height=80).convert("RGB")
    )
    total = 0
    matched = 0
    for pixel in pixels:
        total += 1
        red, green, blue = _rgb_channels(pixel)
        _hue, saturation, value = colorsys.rgb_to_hsv(
            red / 255, green / 255, blue / 255
        )
        if saturation < 0.13 and 0.45 <= value <= 0.90:
            matched += 1
    return matched / max(1, total)


def _iv_star_count(badge: Image.Image) -> tuple[int | None, bool]:
    visible = False
    star_signals: list[tuple[float, float, float, float]] = []
    for zone in IV_STAR_ZONES:
        region = _crop(badge, list(zone))
        amber_ratio = _orange_ratio(region)
        red_ratio = _red_ratio(region)
        inactive_ratio = _iv_inactive_star_ratio(region)
        edge_ratio = _edge_ratio(region)
        star_signals.append((amber_ratio, red_ratio, inactive_ratio, edge_ratio))
        star_visible = (
            amber_ratio >= 0.025
            or red_ratio >= 0.025
            or edge_ratio >= 0.045
            or (inactive_ratio >= 0.060 and edge_ratio >= 0.055)
        )
        visible = visible or star_visible

    red_stars = sum(
        red_ratio >= IV_RED_STAR_RATIO_MIN
        and inactive_ratio < IV_INACTIVE_STAR_GRAY_RATIO_MIN
        for _amber_ratio, red_ratio, inactive_ratio, _star_edge_ratio in star_signals
    )
    if red_stars >= 2:
        return 4, True

    amber_stars = 0
    for amber_ratio, red_ratio, inactive_ratio, _star_edge_ratio in star_signals:
        if (
            amber_ratio >= IV_AMBER_STAR_RATIO_MIN
            and red_ratio < IV_RED_STAR_RATIO_MIN
            and inactive_ratio < IV_INACTIVE_STAR_GRAY_RATIO_MIN
        ):
            amber_stars += 1
            continue
        break
    return (amber_stars, True) if visible else (None, False)


def _iv_star_agrees_with_sum(
    iv_sum: int | None, star_count: int | None, perfect: bool
) -> bool:
    if iv_sum is None or star_count is None:
        return False
    if star_count == 4:
        return iv_sum == 45
    if perfect:
        return iv_sum == 45
    if star_count == 0:
        return 0 <= iv_sum <= 22
    if star_count == 1:
        return 23 <= iv_sum <= 29
    if star_count == 2:
        return 30 <= iv_sum <= 36
    if star_count == 3:
        return 37 <= iv_sum <= 44
    return False


def _visual_iv_evidence(image: Image.Image) -> _IvEvidence:
    badge = _crop(image, REGIONS["appraisal_badge"])
    panel = _crop(image, REGIONS["iv_panel"])
    panel_light_ratio = round(_light_ratio(panel, threshold=245), 4)
    panel_visible = panel_light_ratio >= 0.30 and _edge_ratio(panel) >= 0.08
    raw_star_count, star_badge_visible = _iv_star_count(badge)
    seal_color_ratio = round(_orange_ratio(badge) + _red_ratio(badge), 4)
    seal_visible = panel_visible and star_badge_visible and seal_color_ratio >= 0.05
    star_count = raw_star_count if seal_visible else None
    bar_windows = _iv_bar_windows_for_panel(panel)
    bars = {name: _decode_iv_bar(panel, window) for name, window in bar_windows.items()}

    if not panel_visible or not seal_visible:
        bars = {name: None for name in IV_BAR_WINDOWS}

    bar_count = sum(value is not None for value in bars.values())
    attack = bars["attack"]
    defense = bars["defense"]
    stamina = bars["stamina"]
    iv_sum = None
    if attack is not None and defense is not None and stamina is not None:
        iv_sum = attack + defense + stamina
    perfect = star_count == 4
    star_agreement = _iv_star_agrees_with_sum(iv_sum, star_count, perfect)

    return _IvEvidence(
        attack=attack,
        defense=defense,
        stamina=stamina,
        iv_sum=iv_sum,
        star_count=star_count,
        badge_visible=seal_visible,
        perfect=perfect,
        star_agreement=star_agreement,
        panel_visible=panel_visible,
        seal_visible=seal_visible,
        bar_count=bar_count,
        panel_light_ratio=panel_light_ratio,
        seal_color_ratio=seal_color_ratio,
    )


def _iv_signal_payload(evidence: _IvEvidence) -> dict[str, SignalValue]:
    return {
        "iv_panel_light_ratio": evidence.panel_light_ratio,
        "iv_seal_color_ratio": evidence.seal_color_ratio,
        "iv_bar_count": evidence.bar_count,
        "iv_star_count": evidence.star_count if evidence.star_count is not None else -1,
        "iv_badge_visible": evidence.badge_visible,
        "iv_panel_visible": evidence.panel_visible,
        "iv_seal_visible": evidence.seal_visible,
        "iv_perfect_signal": evidence.perfect,
        "iv_star_agreement": evidence.star_agreement,
    }


def _dark_row_run_ratio(
    image: Image.Image, *, y: int, x_start: int, x_stop: int, threshold: int
) -> float:
    longest = 0
    current = 0
    width = max(1, x_stop - x_start)
    for x in range(x_start, x_stop):
        red, green, blue = _rgb_channels(image.getpixel((x, y)))
        if max(red, green, blue) <= threshold:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest / width


def _dark_row_ratio(
    image: Image.Image, *, y: int, x_start: int, x_stop: int, threshold: int
) -> float:
    width = max(1, x_stop - x_start)
    matched = 0
    for x in range(x_start, x_stop):
        red, green, blue = _rgb_channels(image.getpixel((x, y)))
        if max(red, green, blue) <= threshold:
            matched += 1
    return matched / width


def _detect_moves_tab_anchor(
    image: Image.Image, hp_bar_anchor_y: float | None = None
) -> tuple[float | None, float]:
    width, height = image.size
    if width < 20 or height < 20 or hp_bar_anchor_y is None:
        return None, 0.0

    x_start = int(width * 0.18)
    x_stop = int(width * 0.54)
    y_start = max(0, int(height * (hp_bar_anchor_y + 0.20)))
    y_stop = min(height, int(height * min(0.98, hp_bar_anchor_y + 0.78)))
    best_y = 0
    best_score = 0.0
    for y in range(y_start, y_stop):
        run_ratio = _dark_row_run_ratio(
            image, y=y, x_start=x_start, x_stop=x_stop, threshold=150
        )
        if run_ratio < 0.12:
            continue
        row_ratio = _dark_row_ratio(
            image, y=y, x_start=x_start, x_stop=x_stop, threshold=160
        )
        score = run_ratio + row_ratio * 0.35
        if score > best_score:
            best_score = score
            best_y = y
        if score >= 0.70:
            return y / height, score

    if best_score < 0.45:
        return None, best_score
    return best_y / height, best_score


def _move_visual_region_box(
    left: float, top: float, right: float, bottom: float
) -> list[float]:
    return _clamp_region_box(left, top, right, bottom)


def _move_visual_regions(tab_anchor_y: float | None) -> dict[str, list[float]]:
    if tab_anchor_y is None:
        return {
            "moves_tabs": list(REGIONS["moves_tabs"]),
            "moves_fast_row": list(REGIONS["moves_fast_row"]),
            "moves_charged_rows": list(REGIONS["moves_charged_rows"]),
            "moves_complete_rows": list(REGIONS["moves_complete_rows"]),
            "moves_completion_footer": list(REGIONS["moves_completion_footer"]),
            "moves_new_attack_button": list(REGIONS["moves_completion_footer"]),
            "moves_transition_guard": list(REGIONS["moves_transition_guard"]),
        }
    return {
        "moves_tabs": _move_visual_region_box(
            0.18, tab_anchor_y - 0.045, 0.82, tab_anchor_y + 0.020
        ),
        "moves_fast_row": _move_visual_region_box(
            0.05, tab_anchor_y + 0.012, 0.95, tab_anchor_y + 0.075
        ),
        "moves_charged_rows": _move_visual_region_box(
            0.05, tab_anchor_y + 0.070, 0.95, tab_anchor_y + 0.140
        ),
        "moves_complete_rows": _move_visual_region_box(
            0.05, tab_anchor_y + 0.070, 0.95, tab_anchor_y + 0.175
        ),
        "moves_completion_footer": _move_visual_region_box(
            0.05, tab_anchor_y + 0.105, 0.95, tab_anchor_y + 0.245
        ),
        "moves_new_attack_button": _move_visual_region_box(
            0.06, tab_anchor_y + 0.155, 0.44, tab_anchor_y + 0.235
        ),
        "moves_transition_guard": _move_visual_region_box(
            0.05, tab_anchor_y - 0.145, 0.45, tab_anchor_y + 0.030
        ),
    }


def _region_box_height(box: list[float]) -> float:
    return max(0.0, box[3] - box[1])


def _debug_crop_suffix(visible_crop: bool, suffix: str) -> str | None:
    return suffix if visible_crop else None


def _lightweight_iv_signal_payload(
    badge_region: Image.Image, panel_region: Image.Image
) -> dict[str, SignalValue]:
    panel_light_ratio = round(_light_ratio(panel_region, threshold=245), 4)
    panel_visible = panel_light_ratio >= 0.30 and _edge_ratio(panel_region) >= 0.08
    _, star_badge_visible = _iv_star_count(badge_region)
    seal_color_ratio = round(_orange_ratio(badge_region) + _red_ratio(badge_region), 4)
    seal_visible = panel_visible and star_badge_visible and seal_color_ratio >= 0.05
    return {
        "iv_panel_light_ratio": panel_light_ratio,
        "iv_seal_color_ratio": seal_color_ratio,
        "iv_bar_count": 0,
        "iv_star_count": -1,
        "iv_badge_visible": False,
        "iv_panel_visible": panel_visible,
        "iv_seal_visible": seal_visible,
        "iv_perfect_signal": False,
        "iv_star_agreement": False,
    }


def _visual_signals(
    image: Image.Image, *, visible_crop: bool = False
) -> dict[str, SignalValue]:
    list_region = _crop(
        image,
        REGIONS["list_rows"],
        _debug_crop_suffix(visible_crop, "visual_list_rows"),
    )
    detail_card = _crop(
        image,
        REGIONS["detail_card"],
        _debug_crop_suffix(visible_crop, "visual_detail_card"),
    )
    hp_region = _crop(
        image, REGIONS["hp"], _debug_crop_suffix(visible_crop, "visual_hp")
    )
    name_region = _crop(
        image, REGIONS["name"], _debug_crop_suffix(visible_crop, "visual_name")
    )
    moves_region = _crop(
        image, REGIONS["moves"], _debug_crop_suffix(visible_crop, "visual_moves")
    )
    story_region = _crop(
        image, REGIONS["story"], _debug_crop_suffix(visible_crop, "visual_story")
    )
    badge_region = _crop(
        image,
        REGIONS["appraisal_badge"],
        _debug_crop_suffix(visible_crop, "visual_appraisal_badge"),
    )
    iv_panel = _crop(
        image, REGIONS["iv_panel"], _debug_crop_suffix(visible_crop, "visual_iv_panel")
    )
    art_region = _crop(
        image,
        REGIONS["pokemon_art"],
        _debug_crop_suffix(visible_crop, "visual_pokemon_art"),
    )
    horizontal_swipe_card = _crop(
        image,
        REGIONS["horizontal_swipe_card"],
        _debug_crop_suffix(visible_crop, "visual_horizontal_swipe_card"),
    )
    list_row_count = _count_bright_row_bands(list_region)
    iv_signals = _lightweight_iv_signal_payload(badge_region, iv_panel)
    hp_area_card_gap_ratio, hp_area_card_gap_y = _hp_area_card_split_metrics(image)
    horizontal_card_gap_ratio = round(
        _low_bright_column_run_ratio(horizontal_swipe_card), 4
    )
    hp_area_card_gap_ratio = round(hp_area_card_gap_ratio, 4)
    hp_area_card_gap_y = round(hp_area_card_gap_y, 4)

    signals: dict[str, SignalValue] = {
        "list_bright_row_bands": list_row_count,
        "list_row_count": list_row_count,
        "list_text_dark_ratio": round(_dark_ratio(list_region, threshold=90), 4),
        "list_pokemon_art_signal": round(
            _hsv_ratio(
                list_region,
                hue_min=0,
                hue_max=360,
                saturation_min=0.18,
                value_min=0.55,
            ),
            4,
        ),
        "detail_card_brightness": round(_brightness(detail_card), 4),
        "hp_green_ratio": round(_green_ratio(hp_region), 4),
        "name_dark_ratio": round(_dark_ratio(name_region, threshold=185), 4),
        "moves_dark_ratio": round(_dark_ratio(moves_region, threshold=185), 4),
        "story_brightness": round(_brightness(story_region), 4),
        "story_dark_ratio": round(_dark_ratio(story_region, threshold=205), 4),
        "orange_badge_ratio": round(_orange_ratio(badge_region), 4),
        "pokemon_art_signal": round(
            _edge_ratio(art_region) + _saturated_ratio(art_region) * 0.25, 4
        ),
        "horizontal_card_gap_ratio": horizontal_card_gap_ratio,
        "hp_area_card_gap_ratio": hp_area_card_gap_ratio,
        "hp_area_card_gap_y": hp_area_card_gap_y,
        "hp_area_card_split_signal": (
            hp_area_card_gap_ratio >= HP_AREA_CARD_SPLIT_MIN_RATIO
            and horizontal_card_gap_ratio >= HORIZONTAL_CARD_GAP_MIN_RATIO
        ),
    }
    signals.update(iv_signals)
    signals["detail_card_visible"] = _detail_card_visible(signals)
    signals["single_list_screen_signal"] = _single_list_screen_signal(signals)
    signals["sparse_list_grid_signal"] = _sparse_list_grid_signal(signals)
    signals["list_grid_signal"] = (
        _list_grid_signal(signals)
        or bool(signals["sparse_list_grid_signal"])
        or bool(signals["single_list_screen_signal"])
    )
    signals["menu_overlay_signal"] = _menu_overlay_signal(signals)
    signals["stable_detail_signal"] = _stable_detail_signal(signals)
    signals["horizontal_swipe_signal"] = _horizontal_swipe_signal(signals)
    signals["sequence_transition_signal"] = False
    return signals


def _enrich_hp_tag_visual_signals(
    image: Image.Image,
    signals: dict[str, SignalValue],
    *,
    visible_crop: bool = False,
) -> None:
    hp_region = _crop(
        image, REGIONS["hp"], _debug_crop_suffix(visible_crop, "visual_hp")
    )
    hp_bar_region = _crop(
        image, REGIONS["hp_bar"], _debug_crop_suffix(visible_crop, "visual_hp_bar")
    )
    hp_text_region = _crop(
        image, REGIONS["hp_text"], _debug_crop_suffix(visible_crop, "visual_hp_text")
    )
    hp_bar_candidates = _hp_bar_anchor_candidates(image)
    hp_bar_anchor_y, hp_bar_anchor_score = _detect_hp_bar_anchor(image)
    tag_chip_region = _tag_chip_region(hp_bar_anchor_y)
    tag_region = _crop(
        image, tag_chip_region, _debug_crop_suffix(visible_crop, "visual_tag")
    )
    signals.update(
        {
            "hp_region_dark_ratio": round(_dark_ratio(hp_region, threshold=185), 4),
            "hp_bar_edge_ratio": round(_edge_ratio(hp_bar_region), 4),
            "hp_text_edge_ratio": round(_edge_ratio(hp_text_region), 4),
            "hp_bar_saturation_ratio": round(
                _hsv_ratio(
                    hp_bar_region,
                    hue_min=0,
                    hue_max=360,
                    saturation_min=0.08,
                    value_min=0.35,
                ),
                4,
            ),
            "hp_bar_orange_ratio": round(
                _hsv_ratio(
                    hp_bar_region,
                    hue_min=10,
                    hue_max=55,
                    saturation_min=0.20,
                    value_min=0.35,
                ),
                4,
            ),
            "hp_bar_anchor_y": round(hp_bar_anchor_y or 0.0, 4),
            "hp_bar_anchor_score": round(hp_bar_anchor_score, 4),
            "hp_bar_anchor_visible": hp_bar_anchor_y is not None,
            "hp_bar_anchor_candidate_count": len(hp_bar_candidates),
            "tag_edge_ratio": round(_edge_ratio(tag_region), 4),
            "tag_chip_region_anchored": hp_bar_anchor_y is not None,
            "tag_chip_region_left": round(tag_chip_region[0], 4),
            "tag_chip_region_top": round(tag_chip_region[1], 4),
            "tag_chip_region_right": round(tag_chip_region[2], 4),
            "tag_chip_region_bottom": round(tag_chip_region[3], 4),
        }
    )


def _enrich_detail_visual_signals(
    image: Image.Image,
    signals: dict[str, SignalValue],
    *,
    visible_crop: bool = False,
) -> None:
    _enrich_hp_tag_visual_signals(image, signals, visible_crop=visible_crop)
    hp_bar_anchor_y = (
        float(signals.get("hp_bar_anchor_y", 0.0))
        if bool(signals.get("hp_bar_anchor_visible"))
        else None
    )
    moves_tab_anchor_y, moves_tab_anchor_score = _detect_moves_tab_anchor(
        image, hp_bar_anchor_y
    )
    move_regions = _move_visual_regions(moves_tab_anchor_y)
    moves_tabs_region = _crop(
        image,
        move_regions["moves_tabs"],
        _debug_crop_suffix(visible_crop, "visual_moves_tabs"),
    )
    moves_fast_row_region = _crop(
        image,
        move_regions["moves_fast_row"],
        _debug_crop_suffix(visible_crop, "visual_moves_fast_row"),
    )
    moves_charged_rows_region = _crop(
        image,
        move_regions["moves_charged_rows"],
        _debug_crop_suffix(visible_crop, "visual_moves_charged_rows"),
    )
    moves_complete_rows_region = _crop(
        image,
        move_regions["moves_complete_rows"],
        _debug_crop_suffix(visible_crop, "visual_moves_complete_rows"),
    )
    moves_completion_footer_region = _crop(
        image,
        move_regions["moves_completion_footer"],
        _debug_crop_suffix(visible_crop, "visual_moves_completion_footer"),
    )
    moves_new_attack_button_region = _crop(
        image,
        move_regions["moves_new_attack_button"],
        _debug_crop_suffix(visible_crop, "visual_moves_new_attack_button"),
    )
    moves_transition_guard_region = _crop(
        image,
        move_regions["moves_transition_guard"],
        _debug_crop_suffix(visible_crop, "visual_moves_transition_guard"),
    )
    signals.update(
        {
            "moves_visual_region_anchored": (
                hp_bar_anchor_y is not None and moves_tab_anchor_y is not None
            ),
            "moves_tab_dark_ratio": round(
                _dark_ratio(moves_tabs_region, threshold=185), 4
            ),
            "moves_tab_edge_ratio": round(_edge_ratio(moves_tabs_region), 4),
            "moves_fast_row_dark_ratio": round(
                _dark_ratio(moves_fast_row_region, threshold=185), 4
            ),
            "moves_charged_rows_dark_ratio": round(
                _dark_ratio(moves_charged_rows_region, threshold=185), 4
            ),
            "moves_complete_rows_dark_ratio": round(
                _dark_ratio(moves_complete_rows_region, threshold=185), 4
            ),
            "moves_completion_footer_dark_ratio": round(
                _dark_ratio(moves_completion_footer_region, threshold=185), 4
            ),
            "moves_new_attack_button_green_ratio": round(
                _green_ratio(moves_new_attack_button_region), 4
            ),
            "moves_transition_guard_dark_ratio": round(
                _dark_ratio(moves_transition_guard_region, threshold=90), 4
            ),
            "moves_tab_anchor_y": round(moves_tab_anchor_y or 0.0, 4),
            "moves_tab_anchor_score": round(moves_tab_anchor_score, 4),
            "moves_tab_anchor_visible": moves_tab_anchor_y is not None,
            "moves_completion_footer_height": round(
                _region_box_height(move_regions["moves_completion_footer"]), 4
            ),
            "moves_new_attack_button_height": round(
                _region_box_height(move_regions["moves_new_attack_button"]), 4
            ),
        }
    )
    for region_name, region_box in move_regions.items():
        _set_region_signals(signals, region_name, region_box)


def _empty_iv_evidence_from_signals(signals: dict[str, SignalValue]) -> _IvEvidence:
    return _IvEvidence(
        attack=None,
        defense=None,
        stamina=None,
        iv_sum=None,
        star_count=None,
        badge_visible=False,
        perfect=False,
        star_agreement=False,
        panel_visible=bool(signals.get("iv_panel_visible")),
        seal_visible=bool(signals.get("iv_seal_visible")),
        bar_count=0,
        panel_light_ratio=float(signals.get("iv_panel_light_ratio", 0.0)),
        seal_color_ratio=float(signals.get("iv_seal_color_ratio", 0.0)),
    )


def _enrich_appraisal_visual_signals(
    image: Image.Image,
    signals: dict[str, SignalValue],
    *,
    visible_crop: bool = False,
) -> _IvEvidence:
    _enrich_hp_tag_visual_signals(image, signals, visible_crop=visible_crop)
    evidence = _visual_iv_evidence(image)
    signals.update(_iv_signal_payload(evidence))
    return evidence


def _visual_sequence_signals(image: Image.Image) -> dict[str, SignalValue]:
    return _visual_signals(image)


def _detail_card_visible(signals: dict[str, SignalValue]) -> bool:
    return float(signals["detail_card_brightness"]) >= 0.70


def _list_grid_signal(signals: dict[str, SignalValue]) -> bool:
    return (
        float(signals["detail_card_brightness"]) >= 0.92
        and float(signals["story_brightness"]) >= 0.90
        and float(signals["hp_green_ratio"]) < 0.01
        and float(signals["moves_dark_ratio"]) < 0.08
        and 0.035 <= float(signals["name_dark_ratio"]) <= 0.16
    )


def _sparse_list_grid_signal(signals: dict[str, SignalValue]) -> bool:
    return (
        float(signals["detail_card_brightness"]) >= 0.92
        and float(signals["story_brightness"]) >= 0.90
        and float(signals["moves_dark_ratio"]) < 0.08
        and 0.030 <= float(signals["hp_green_ratio"]) <= 0.060
        and 0.035 <= float(signals["name_dark_ratio"]) <= 0.16
        and 0.015 <= float(signals["list_pokemon_art_signal"]) <= 0.08
        and 0.10 <= float(signals["pokemon_art_signal"]) <= 0.19
        and not bool(signals.get("iv_panel_visible"))
        and not bool(signals.get("iv_seal_visible"))
    )


def _single_list_screen_signal(signals: dict[str, SignalValue]) -> bool:
    return (
        float(signals["detail_card_brightness"]) >= 0.93
        and float(signals["story_brightness"]) >= 0.90
        and float(signals["hp_green_ratio"]) <= 0.04
        and float(signals["moves_dark_ratio"]) <= 0.11
        and 0.010 <= float(signals["list_pokemon_art_signal"]) <= 0.080
        and 0.10 <= float(signals["pokemon_art_signal"]) <= 0.19
        and not bool(signals.get("iv_seal_visible"))
    )


def _menu_overlay_signal(signals: dict[str, SignalValue]) -> bool:
    detail_card_brightness = float(signals["detail_card_brightness"])
    hp_green_ratio = float(signals["hp_green_ratio"])
    name_dark_ratio = float(signals["name_dark_ratio"])
    moves_dark_ratio = float(signals["moves_dark_ratio"])
    return detail_card_brightness < 0.70 and (
        name_dark_ratio >= 0.40
        or moves_dark_ratio >= 0.45
        or (hp_green_ratio >= 0.20 and moves_dark_ratio >= 0.20)
    )


def _stable_detail_signal(signals: dict[str, SignalValue]) -> bool:
    return bool(signals["detail_card_visible"]) and (
        float(signals["hp_green_ratio"]) >= 0.03
        or float(signals["moves_dark_ratio"]) >= 0.10
        or float(signals["name_dark_ratio"]) >= 0.025
        or float(signals["pokemon_art_signal"]) >= 0.08
    )


def _horizontal_swipe_signal(signals: dict[str, SignalValue]) -> bool:
    return float(signals["horizontal_card_gap_ratio"]) >= HORIZONTAL_CARD_GAP_MIN_RATIO


def _contains_word(text: str | None, *words: str) -> bool:
    if not text:
        return False
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return any(word.casefold() in normalized for word in words)


def _has_letters(text: str | None, *, minimum: int = 3) -> bool:
    if not text:
        return False
    return sum(character.isalpha() for character in text) >= minimum


def _visual_display_name_evidence(signals: dict[str, SignalValue]) -> bool:
    name_dark_ratio = float(signals["name_dark_ratio"])
    detail_card_brightness = float(signals["detail_card_brightness"])
    pokemon_art_signal = float(signals["pokemon_art_signal"])
    low_contrast_name_run = (
        0.042 <= name_dark_ratio <= 0.045
        and 0.915 <= detail_card_brightness <= 0.918
        and 0.155 <= pokemon_art_signal <= 0.170
    )
    adjacent_low_contrast_name = (
        0.040 <= name_dark_ratio <= 0.048
        and 0.917 <= detail_card_brightness <= 0.920
        and 0.155 <= pokemon_art_signal <= 0.215
    )
    return low_contrast_name_run or adjacent_low_contrast_name


def _visual_hp_evidence(signals: dict[str, SignalValue]) -> bool:
    return (
        float(signals.get("hp_region_dark_ratio", 0.0)) >= 0.036
        and float(signals.get("hp_bar_edge_ratio", 0.0)) >= 0.090
        and float(signals.get("hp_text_edge_ratio", 0.0)) >= 0.110
    )


def _visual_all_moves_evidence(signals: dict[str, SignalValue]) -> bool:
    if (
        bool(signals.get("horizontal_swipe_signal"))
        or bool(signals.get("hp_area_card_split_signal"))
        or bool(signals.get("sequence_transition_signal"))
    ):
        return False
    if not bool(signals.get("hp_bar_anchor_visible")):
        return False
    active_moves_section = bool(signals.get("moves_tab_anchor_visible"))
    anchored_move_regions = bool(signals.get("moves_visual_region_anchored"))
    fast_move_row = float(signals.get("moves_fast_row_dark_ratio", 0.0))
    charged_move_row = float(signals.get("moves_charged_rows_dark_ratio", 0.0))
    complete_move_rows = float(signals.get("moves_complete_rows_dark_ratio", 0.0))
    completion_footer = float(signals.get("moves_completion_footer_dark_ratio", 0.0))
    completion_footer_height = float(signals.get("moves_completion_footer_height", 0.0))
    new_attack_button_green = float(
        signals.get("moves_new_attack_button_green_ratio", 0.0)
    )
    new_attack_button_height = float(signals.get("moves_new_attack_button_height", 0.0))
    transition_guard = float(signals.get("moves_transition_guard_dark_ratio", 1.0))
    if not active_moves_section or not anchored_move_regions:
        return False
    visible_section_end = completion_footer_height >= 0.115 or (
        new_attack_button_height >= 0.060 and new_attack_button_green >= 0.35
    )
    strict_complete_section = (
        fast_move_row >= 0.045
        and charged_move_row >= 0.070
        and complete_move_rows >= 0.070
        and completion_footer >= 0.085
        and visible_section_end
        and transition_guard <= 0.012
    )
    clear_two_move_section = (
        fast_move_row >= 0.040
        and charged_move_row >= 0.055
        and complete_move_rows >= 0.080
        and completion_footer >= 0.200
        and visible_section_end
        and transition_guard <= 0.012
    )
    return strict_complete_section or clear_two_move_section


def _moves_tab_text_confirmed(text: str | None) -> bool:
    if not text:
        return False
    compact = re.sub(r"[^a-z0-9]+", "", text.casefold())
    return "gymsraids" in compact or "gymraids" in compact


# Removed: appraisal story move fallback is no longer supported
def _appraisal_story_move_text(
    _raw_classification: str, _story_text: str | None, _current_move_text: str | None
) -> str:
    return ""


def _visual_cp_evidence(
    raw_classification: str, signals: dict[str, SignalValue]
) -> bool:
    name_dark_ratio = float(signals["name_dark_ratio"])
    detail_card_brightness = float(signals["detail_card_brightness"])
    pokemon_art_signal = float(signals["pokemon_art_signal"])
    hp_green_ratio = float(signals["hp_green_ratio"])
    orange_badge_ratio = float(signals["orange_badge_ratio"])

    early_detail = (
        0.055 <= name_dark_ratio <= 0.075
        and 0.75 <= detail_card_brightness <= 0.83
        and 0.285 <= pokemon_art_signal <= 0.34
    )
    appraisal_with_hp = (
        raw_classification == "appraisal"
        and hp_green_ratio >= 0.03
        and orange_badge_ratio >= 0.015
        and 0.805 <= detail_card_brightness <= 0.812
        and 0.20 <= pokemon_art_signal <= 0.22
        and 0.085 <= name_dark_ratio <= 0.12
    )
    late_detail = (
        raw_classification == "detail"
        and hp_green_ratio >= 0.03
        and 0.90 <= detail_card_brightness <= 0.91
        and 0.19 <= pokemon_art_signal <= 0.205
        and 0.085 <= name_dark_ratio <= 0.095
    )
    return early_detail or appraisal_with_hp or late_detail


def _read_region(
    image: Image.Image,
    engine: TesseractOcrEngine,
    box: list[float],
    *,
    psm: int = 6,
    config: str = "",
    min_width: int = 480,
    max_width: int = 1100,
    resample: Image.Resampling = Image.Resampling.BILINEAR,
    save_as: str | Path | None = None,
) -> OcrResult:
    if not engine.is_available():
        return OcrResult("", 0.0)
    crop = _crop(image, box, save_as)
    crop = ImageOps.grayscale(crop)
    crop = ImageOps.autocontrast(crop)
    width, height = crop.size
    scale = 1.0
    if width < min_width:
        scale = min_width / max(1, width)
    if width * scale > max_width:
        scale = max_width / max(1, width)
    if scale != 1.0:
        crop = crop.resize(
            (max(1, int(width * scale)), max(1, int(height * scale))),
            resample,
        )
    try:
        return engine.read_text(crop, psm=psm, config=config)
    except RuntimeError:
        return OcrResult("", 0.0)


def _detail_layout(
    *,
    raw_classification: str,
    signals: dict[str, SignalValue],
    iv_evidence: _IvEvidence,
    story_text: str,
) -> _DetailLayout:
    hp_bar_y = (
        float(signals.get("hp_bar_anchor_y", 0.0))
        if bool(signals.get("hp_bar_anchor_visible"))
        else None
    )
    hp_bar_score = float(signals.get("hp_bar_anchor_score", 0.0))
    initial_appraisal = (
        raw_classification == "appraisal"
        and iv_evidence.star_count is not None
        and iv_evidence.panel_visible
        and story_text_is_complete(story_text)
    )
    if initial_appraisal:
        return _DetailLayout("initial_appraisal_overlay", hp_bar_y, hp_bar_score)
    return _DetailLayout("scrollable_detail", hp_bar_y, hp_bar_score)


def _clamp_region_box(
    left: float, top: float, right: float, bottom: float
) -> list[float]:
    clamped_top = max(0.0, min(0.98, top))
    clamped_bottom = max(clamped_top + 0.01, min(1.0, bottom))
    return [max(0.0, left), clamped_top, min(1.0, right), clamped_bottom]


def _tag_chip_region(hp_bar_anchor_y: float | None) -> list[float]:
    if hp_bar_anchor_y is None:
        return list(REGIONS["tag"])
    return _clamp_region_box(
        0.06,
        hp_bar_anchor_y + 0.035,
        0.94,
        hp_bar_anchor_y + 0.145,
    )


def _unique_region_boxes(boxes: Iterable[list[float]]) -> list[list[float]]:
    unique: list[list[float]] = []
    seen: set[tuple[float, ...]] = set()
    for box in boxes:
        key = tuple(round(value, 4) for value in box)
        if key in seen:
            continue
        seen.add(key)
        unique.append(box)
    return unique


def _cp_fallback_regions(layout: _DetailLayout) -> list[list[float]]:
    boxes: list[list[float]] = []
    if layout.mode == "initial_appraisal_overlay":
        boxes.extend([list(box) for box in INITIAL_APPRAISAL_CP_REGIONS])
    if layout.hp_bar_y is not None:
        boxes.append(
            _clamp_region_box(
                0.30,
                layout.hp_bar_y - 0.360,
                0.64,
                layout.hp_bar_y - 0.305,
            )
        )
        boxes.append(
            _clamp_region_box(
                0.28,
                layout.hp_bar_y - 0.360,
                0.66,
                layout.hp_bar_y - 0.300,
            )
        )
    boxes.extend([list(box) for box in INITIAL_APPRAISAL_CP_REGIONS])
    return _unique_region_boxes(boxes)


def _hp_bar_text_regions(anchor_y: float) -> list[list[float]]:
    return [
        _clamp_region_box(
            0.35,
            anchor_y + 0.025,
            0.70,
            anchor_y + 0.070,
        ),
        _clamp_region_box(
            0.30,
            anchor_y + 0.020,
            0.74,
            anchor_y + 0.075,
        ),
        _clamp_region_box(
            0.28,
            anchor_y - 0.012,
            0.76,
            anchor_y + 0.038,
        ),
    ]


def _hp_fallback_regions(
    layout: _DetailLayout, extra_anchors: Iterable[float] = ()
) -> list[list[float]]:
    boxes: list[list[float]] = []
    if layout.mode == "initial_appraisal_overlay":
        boxes.extend([list(box) for box in INITIAL_APPRAISAL_HP_REGIONS])
    if layout.hp_bar_y is not None:
        boxes.extend(_hp_bar_text_regions(layout.hp_bar_y))
    for anchor_y in extra_anchors:
        boxes.extend(_hp_bar_text_regions(anchor_y))
    boxes.extend([list(box) for box in INITIAL_APPRAISAL_HP_REGIONS])
    return _unique_region_boxes(boxes)


def _weight_fallback_regions(layout: _DetailLayout | None = None) -> list[list[float]]:
    boxes: list[list[float]] = []
    if layout is not None and layout.hp_bar_y is not None:
        boxes.extend(
            [
                _clamp_region_box(
                    0.05,
                    layout.hp_bar_y + 0.125,
                    0.38,
                    layout.hp_bar_y + 0.205,
                ),
                _clamp_region_box(
                    0.03,
                    layout.hp_bar_y + 0.120,
                    0.42,
                    layout.hp_bar_y + 0.215,
                ),
            ]
        )
    boxes.extend(
        [
            [0.04, 0.36, 0.38, 0.42],
            [0.04, 0.38, 0.38, 0.44],
            [0.02, 0.36, 0.45, 0.42],
        ]
    )
    return _unique_region_boxes(boxes)


def _moves_ocr_region(signals: dict[str, SignalValue]) -> list[float]:
    anchor_y = (
        float(signals.get("moves_tab_anchor_y", 0.0))
        if bool(signals.get("moves_tab_anchor_visible"))
        else None
    )
    crop_top = anchor_y + 0.012 if anchor_y else REGIONS["moves_complete_rows"][1]
    return _clamp_region_box(0.05, min(0.93, crop_top), 0.95, 0.99)


def _cp_text_has_prefix(text: str | None) -> bool:
    if not text:
        return False
    normalized = text.translate(NUM_TRANSLATION)
    compact = re.sub(r"[^a-z0-9]+", "", normalized.casefold())
    return re.search(r"(?:c+p+e?|c+e+p+|e+p+|c+e+)\d{2,5}", compact) is not None


def _select_cp_ocr_result(candidates: Iterable[OcrResult]) -> OcrResult:
    best_score: tuple[bool, int, float, int] | None = None
    best_result = OcrResult("", 0.0)
    for index, candidate in enumerate(candidates):
        if parse_cp_candidate(candidate.text) is None:
            continue
        score = (
            _cp_text_has_prefix(candidate.text),
            sum(character.isdigit() for character in candidate.text),
            float(candidate.confidence),
            -index,
        )
        if best_score is None or score > best_score:
            best_score = score
            best_result = candidate
    return best_result


def _select_hp_ocr_result(candidates: Iterable[OcrResult]) -> OcrResult:
    selected = _select_hp_ocr_result_with_box(
        (candidate, None) for candidate in candidates
    )
    return selected[0]


def _select_hp_ocr_result_with_box(
    candidates: Iterable[tuple[OcrResult, list[float] | None]],
) -> tuple[OcrResult, list[float] | None]:
    best_score: tuple[bool, float, int] | None = None
    best_result = OcrResult("", 0.0)
    best_box: list[float] | None = None
    for index, candidate in enumerate(candidates):
        result, box = candidate
        if parse_hp_candidate(result.text) is None:
            continue
        score = (
            "hp" in result.text.casefold(),
            float(result.confidence),
            -index,
        )
        if best_score is None or score > best_score:
            best_score = score
            best_result = result
            best_box = box
    return best_result, best_box


def _select_weight_ocr_result_with_box(
    candidates: Iterable[tuple[OcrResult, list[float] | None]],
    *,
    prefer_fallback: bool = False,
) -> tuple[OcrResult, list[float] | None]:
    best_score: tuple[bool, float, int] | None = None
    best_result = OcrResult("", 0.0)
    best_box: list[float] | None = None
    for index, candidate in enumerate(candidates):
        result, box = candidate
        if parse_weight_candidate(result.text) is None:
            continue
        score = (prefer_fallback and box is not None, float(result.confidence), -index)
        if best_score is None or score > best_score:
            best_score = score
            best_result = result
            best_box = box
    return best_result, best_box


def _select_height_ocr_result_with_box(
    candidates: Iterable[tuple[OcrResult, list[float] | None]],
    *,
    prefer_fallback: bool = False,
) -> tuple[OcrResult, list[float] | None]:
    best_score: tuple[bool, float, int] | None = None
    best_result = OcrResult("", 0.0)
    best_box: list[float] | None = None
    for index, candidate in enumerate(candidates):
        result, box = candidate
        if parse_height_candidate(result.text) is None:
            continue
        score = (prefer_fallback and box is not None, float(result.confidence), -index)
        if best_score is None or score > best_score:
            best_score = score
            best_result = result
            best_box = box
    return best_result, best_box


def _recover_cp_ocr(
    image: Image.Image,
    engine: TesseractOcrEngine,
    layout: _DetailLayout,
    current: OcrResult,
    *,
    visible_crop: bool = False,
) -> OcrResult:
    candidates = [current]
    configs = ("", "-c tessedit_char_whitelist=CPcp0123456789")
    for box_index, box in enumerate(_cp_fallback_regions(layout), start=1):
        for config_index, config in enumerate(configs, start=1):
            candidates.append(
                _read_region(
                    image,
                    engine,
                    box,
                    psm=7,
                    config=config,
                    min_width=900,
                    max_width=1400,
                    resample=Image.Resampling.BICUBIC,
                    save_as=_debug_crop_suffix(
                        visible_crop, f"cp_fallback_{box_index:02d}_{config_index:02d}"
                    ),
                )
            )
    selected = _select_cp_ocr_result(candidates)
    return selected if selected.text else current


def _recover_hp_ocr(
    image: Image.Image,
    engine: TesseractOcrEngine,
    layout: _DetailLayout,
    current: OcrResult,
    *,
    visible_crop: bool = False,
) -> tuple[OcrResult, list[float] | None]:
    candidates: list[tuple[OcrResult, list[float] | None]] = [(current, None)]
    configs = ("", "-c tessedit_char_whitelist=HP0123456789/ ")
    extra_anchors = (candidate.y for candidate in _hp_bar_anchor_candidates(image))
    for box_index, box in enumerate(
        _hp_fallback_regions(layout, extra_anchors), start=1
    ):
        for config_index, config in enumerate(configs, start=1):
            candidates.append(
                (
                    _read_region(
                        image,
                        engine,
                        box,
                        psm=7,
                        config=config,
                        min_width=900,
                        max_width=1400,
                        resample=Image.Resampling.BICUBIC,
                        save_as=_debug_crop_suffix(
                            visible_crop,
                            f"hp_fallback_{box_index:02d}_{config_index:02d}",
                        ),
                    ),
                    box,
                )
            )
    selected, selected_box = _select_hp_ocr_result_with_box(candidates)
    return (selected, selected_box) if selected.text else (current, None)


def _recover_weight_ocr(
    image: Image.Image,
    engine: TesseractOcrEngine,
    current: OcrResult,
    *,
    layout: _DetailLayout | None = None,
    prefer_fallback: bool = False,
    visible_crop: bool = False,
) -> tuple[OcrResult, list[float] | None]:
    candidates: list[tuple[OcrResult, list[float] | None]] = [(current, None)]
    configs = ("", "-c tessedit_char_whitelist=0123456789.,kgKG ")
    for box_index, box in enumerate(_weight_fallback_regions(layout), start=1):
        for psm in (6, 7, 11):
            for config_index, config in enumerate(configs, start=1):
                candidates.append(
                    (
                        _read_region(
                            image,
                            engine,
                            box,
                            psm=psm,
                            config=config,
                            min_width=900,
                            max_width=1400,
                            resample=Image.Resampling.BICUBIC,
                            save_as=_debug_crop_suffix(
                                visible_crop,
                                (
                                    f"weight_fallback_{box_index:02d}_psm{psm}_"
                                    f"{config_index:02d}"
                                ),
                            ),
                        ),
                        box,
                    )
                )
    selected, selected_box = _select_weight_ocr_result_with_box(
        candidates,
        prefer_fallback=prefer_fallback,
    )
    return (selected, selected_box) if selected.text else (current, None)


def _height_fallback_regions(layout: _DetailLayout | None = None) -> list[list[float]]:
    boxes: list[list[float]] = []
    if layout is not None and layout.hp_bar_y is not None:
        boxes.extend(
            [
                _clamp_region_box(
                    0.66,
                    layout.hp_bar_y + 0.125,
                    0.98,
                    layout.hp_bar_y + 0.205,
                ),
                _clamp_region_box(
                    0.62,
                    layout.hp_bar_y + 0.120,
                    1.00,
                    layout.hp_bar_y + 0.215,
                ),
            ]
        )
    boxes.extend(
        [
            [0.66, 0.36, 0.98, 0.43],
            [0.62, 0.36, 1.00, 0.44],
        ]
    )
    return _unique_region_boxes(boxes)


def _recover_height_ocr(
    image: Image.Image,
    engine: TesseractOcrEngine,
    current: OcrResult,
    *,
    layout: _DetailLayout | None = None,
    prefer_fallback: bool = False,
    visible_crop: bool = False,
) -> tuple[OcrResult, list[float] | None]:
    candidates: list[tuple[OcrResult, list[float] | None]] = [(current, None)]
    configs = ("", "-c tessedit_char_whitelist=0123456789.,mM ")
    for box_index, box in enumerate(_height_fallback_regions(layout), start=1):
        for psm in (6, 7, 11):
            for config_index, config in enumerate(configs, start=1):
                candidates.append(
                    (
                        _read_region(
                            image,
                            engine,
                            box,
                            psm=psm,
                            config=config,
                            min_width=900,
                            max_width=1400,
                            resample=Image.Resampling.BICUBIC,
                            save_as=_debug_crop_suffix(
                                visible_crop,
                                (
                                    f"height_fallback_{box_index:02d}_psm{psm}_"
                                    f"{config_index:02d}"
                                ),
                            ),
                        ),
                        box,
                    )
                )
    selected, selected_box = _select_height_ocr_result_with_box(
        candidates,
        prefer_fallback=prefer_fallback,
    )
    return (selected, selected_box) if selected.text else (current, None)


def _ocr_payload(result: OcrResult) -> dict[str, object]:
    return {"text": result.text, "confidence": round(float(result.confidence), 4)}


def _empty_ocr_results() -> dict[str, OcrResult]:
    return {
        "cp": OcrResult("", 0.0),
        "display_name": OcrResult("", 0.0),
        "hp": OcrResult("", 0.0),
        "weight": OcrResult("", 0.0),
        "height": OcrResult("", 0.0),
        "moves": OcrResult("", 0.0),
        "tag": OcrResult("", 0.0),
        "story": OcrResult("", 0.0),
        "special_sections": OcrResult("", 0.0),
    }


def _ocr_regions_for(
    raw_classification: str,
    ocr_mode: str,
    *,
    moves_box: list[float] | None = None,
) -> dict[str, tuple[list[float], int]]:
    all_regions = {
        "cp": (REGIONS["cp"], 7),
        "display_name": (REGIONS["name"], 7),
        "hp": (REGIONS["hp"], 7),
        "weight": (REGIONS["weight"], 7),
        "height": (REGIONS["height"], 6),
        "moves": (moves_box or REGIONS["moves"], 6),
        "tag": (REGIONS["tag"], 11),
        "story": (REGIONS["story"], 6),
        "special_sections": (REGIONS["special_sections"], 6),
    }
    if ocr_mode == "off" or raw_classification in {NON_EXTRACTABLE_CLASS, "list"}:
        return {}
    if raw_classification == "appraisal":
        return {
            key: all_regions[key]
            for key in (
                "cp",
                "display_name",
                "hp",
                "weight",
                "story",
            )
        }
    if raw_classification == "detail":
        return {
            key: all_regions[key]
            for key in (
                "cp",
                "display_name",
                "hp",
                "weight",
                "height",
                "moves",
                "special_sections",
            )
        }
    return {}


def _verified_appraisal_evidence(evidence: _IvEvidence) -> bool:
    return evidence.has_iv


def _raw_classification(
    signals: dict[str, SignalValue], *, allow_appraisal: bool = True
) -> str:
    list_bands = int(signals["list_bright_row_bands"])
    detail_card_brightness = float(signals["detail_card_brightness"])
    hp_green_ratio = float(signals["hp_green_ratio"])
    name_dark_ratio = float(signals["name_dark_ratio"])
    moves_dark_ratio = float(signals["moves_dark_ratio"])
    story_brightness = float(signals["story_brightness"])
    story_dark_ratio = float(signals["story_dark_ratio"])
    orange_badge_ratio = float(signals["orange_badge_ratio"])
    appraisal_badge_color_ratio = max(
        orange_badge_ratio, float(signals.get("iv_seal_color_ratio", 0.0))
    )
    appraisal_candidate_signal = (
        allow_appraisal
        and not bool(signals.get("hp_area_card_split_signal"))
        and not (
            bool(signals.get("horizontal_swipe_signal"))
            and detail_card_brightness >= 0.89
        )
        and bool(signals.get("iv_panel_visible"))
        and bool(signals.get("iv_seal_visible"))
    )

    if bool(signals["list_grid_signal"]):
        return "list"
    if _sparse_list_grid_signal(signals):
        return "list"
    if not bool(signals["detail_card_visible"]) and list_bands >= 3:
        return "list"
    if bool(signals["menu_overlay_signal"]):
        return NON_EXTRACTABLE_CLASS
    if not bool(signals["detail_card_visible"]):
        return NON_EXTRACTABLE_CLASS
    if (
        detail_card_brightness >= 0.90
        and story_brightness >= 0.90
        and moves_dark_ratio >= 0.08
        and not bool(signals.get("horizontal_swipe_signal"))
    ):
        return "detail"
    if (
        story_brightness >= 0.68
        and story_dark_ratio >= 0.08
        and appraisal_badge_color_ratio >= 0.015
        and appraisal_candidate_signal
    ):
        return "appraisal"
    if list_bands >= 3 and hp_green_ratio < 0.02 and 0.18 <= moves_dark_ratio < 0.30:
        return NON_EXTRACTABLE_CLASS
    if (
        detail_card_brightness >= 0.70
        and hp_green_ratio >= 0.03
        and name_dark_ratio >= 0.025
    ):
        return "detail"
    if detail_card_brightness >= 0.70 and moves_dark_ratio >= 0.10:
        return "detail"
    detail_visual_votes = sum(
        (
            detail_card_brightness >= 0.70,
            hp_green_ratio >= 0.03,
            name_dark_ratio >= 0.025,
            moves_dark_ratio >= 0.10,
        )
    )
    if detail_visual_votes >= 3:
        return "detail"
    if list_bands >= 3:
        return "list"
    return NON_EXTRACTABLE_CLASS


def _detail_feature_candidate(
    raw_classification: str, signals: dict[str, SignalValue]
) -> bool:
    if raw_classification in {"detail", "appraisal"}:
        return True
    if raw_classification == "list":
        return False
    return bool(signals["detail_card_visible"]) and not bool(
        signals["menu_overlay_signal"]
    )


def _apply_list_features(
    features: dict[str, bool],
    raw_classification: str,
    signals: dict[str, SignalValue],
) -> None:
    if raw_classification != "list":
        return

    features["has_list_grid"] = True
    features["has_list_cp"] = float(signals["list_text_dark_ratio"]) >= 0.012
    features["has_list_display_name"] = float(signals["list_text_dark_ratio"]) >= 0.012
    features["has_list_pokemon_art"] = (
        float(signals["list_pokemon_art_signal"]) >= 0.015
    )


def _clear_detail_extraction_features(features: dict[str, bool]) -> None:
    for key in DETAIL_FEATURE_KEYS:
        if key != "has_transition":
            features[key] = False


def _classification_from_features(
    raw_classification: str,
    features: dict[str, bool],
    signals: dict[str, SignalValue],
) -> str:
    if features["has_transition"]:
        return NON_EXTRACTABLE_CLASS
    if raw_classification == "list":
        return "list"
    if raw_classification in {"detail", "appraisal"}:
        return raw_classification
    if bool(signals["menu_overlay_signal"]) or not bool(signals["detail_card_visible"]):
        return NON_EXTRACTABLE_CLASS
    feature_votes = sum(
        int(features[key])
        for key in (
            "has_CP",
            "has_hp",
            "has_weight",
            "has_moves",
            "has_iv",
            "has_story",
        )
    )
    return "detail" if feature_votes >= 2 else NON_EXTRACTABLE_CLASS


def _scan_notes(features: dict[str, bool], *, tesseract_available: bool) -> list[str]:
    notes: list[str] = []
    if features["has_display_name"]:
        notes.append("Display-name evidence is weak identity evidence.")
    if features["has_iv"] and not features["has_iv_complete"]:
        notes.append(IV_INCOMPLETE_NOTE)
    if not tesseract_available:
        notes.append("Tesseract was unavailable; OCR-backed evidence was skipped.")
    return notes


def _scan_values(
    parsed: _ParsedOcrValues,
    iv_evidence: _IvEvidence,
) -> dict[str, object | None]:
    return {
        "cp": parsed.cp,
        "hp": parsed.hp,
        "weight_kg": parsed.weight,
        "height_m": parsed.height,
        "story_text": parsed.story_text or None,
        "story_sentence_complete": story_text_is_complete(parsed.story_text),
        "iv_attack": iv_evidence.attack,
        "iv_defense": iv_evidence.defense,
        "iv_stamina": iv_evidence.stamina,
        "iv_sum": iv_evidence.iv_sum,
        "appraisal_star_count": iv_evidence.star_count,
        "appraisal_badge_visible": iv_evidence.badge_visible,
        "appraisal_perfect": iv_evidence.perfect,
        "iv_star_agreement": iv_evidence.star_agreement,
    }


def _load_frame_image(frame: FrameCandidate) -> tuple[Image.Image, float]:
    started = time.perf_counter()
    with Image.open(frame.frame_path) as opened:
        image = opened.convert("RGB")
    image.info[IMAGE_FILENAME_INFO_KEY] = str(frame.frame_path)
    if frame.debug_frame_path is not None:
        image.info[IMAGE_DEBUG_FILENAME_INFO_KEY] = str(frame.debug_frame_path)
    return image, time.perf_counter() - started


def _sequence_motion_sample_from_image(image: Image.Image) -> Image.Image:
    return ImageOps.grayscale(_crop(image, REGIONS["sequence_motion"])).resize(
        (120, 40),
        Image.Resampling.BILINEAR,
    )


def _set_region_signals(
    signals: dict[str, SignalValue], prefix: str, box: list[float]
) -> None:
    signals[f"{prefix}_left"] = round(box[0], 4)
    signals[f"{prefix}_top"] = round(box[1], 4)
    signals[f"{prefix}_right"] = round(box[2], 4)
    signals[f"{prefix}_bottom"] = round(box[3], 4)


def _visual_scan_analysis(
    image: Image.Image, *, visible_crop: bool = False
) -> _VisualScanAnalysis:
    started = time.perf_counter()
    signals = _visual_signals(image, visible_crop=visible_crop)
    raw_classification = _raw_classification(signals)
    iv_evidence = _empty_iv_evidence_from_signals(signals)
    if raw_classification == "appraisal":
        iv_evidence = _enrich_appraisal_visual_signals(
            image, signals, visible_crop=visible_crop
        )
        if not _verified_appraisal_evidence(iv_evidence):
            raw_classification = _raw_classification(signals, allow_appraisal=False)
            iv_evidence = _empty_iv_evidence_from_signals(signals)
    if raw_classification != "appraisal" and (
        raw_classification == "detail"
        or _detail_feature_candidate(raw_classification, signals)
    ):
        _enrich_detail_visual_signals(image, signals, visible_crop=visible_crop)

    moves_ocr_box = (
        _moves_ocr_region(signals)
        if raw_classification == "detail"
        else list(REGIONS["moves"])
    )
    _set_region_signals(signals, "moves_ocr", moves_ocr_box)
    return _VisualScanAnalysis(
        signals=signals,
        iv_evidence=iv_evidence,
        raw_classification=raw_classification,
        moves_ocr_box=moves_ocr_box,
        duration_s=time.perf_counter() - started,
    )


def _visual_sequence_analysis(image: Image.Image) -> _VisualSequenceAnalysis:
    started = time.perf_counter()
    signals = _visual_sequence_signals(image)
    raw_classification = _raw_classification(signals)
    if raw_classification == "appraisal":
        iv_evidence = _visual_iv_evidence(image)
        signals.update(_iv_signal_payload(iv_evidence))
        if not _verified_appraisal_evidence(iv_evidence):
            raw_classification = _raw_classification(signals, allow_appraisal=False)
    return _VisualSequenceAnalysis(
        signals=signals,
        raw_classification=raw_classification,
        duration_s=time.perf_counter() - started,
    )


def scan_frame_visual_candidate(frame: FrameCandidate) -> FrameVisualRecord:
    started = time.perf_counter()
    image, image_load_s = _load_frame_image(frame)
    analysis = _visual_sequence_analysis(image)
    return FrameVisualRecord(
        frame=frame,
        source_file=_source_file_name(frame.source_asset),
        source_type=frame.source_asset.source_type,
        frame_path=str(frame.frame_path),
        frame_index=frame.frame_index,
        timestamp_s=frame.timestamp_s,
        raw_classification=analysis.raw_classification,
        signals=analysis.signals,
        motion_sample=_sequence_motion_sample_from_image(image),
        timing={
            "image_load_s": round(image_load_s, 6),
            "visual_analysis_s": round(analysis.duration_s, 6),
            "total_s": round(time.perf_counter() - started, 6),
        },
    )


def _read_frame_ocr(
    image: Image.Image,
    engine: TesseractOcrEngine,
    settings: ScanSettings,
    analysis: _VisualScanAnalysis,
    *,
    requested_ocr_fields: Iterable[str] | None = None,
) -> tuple[dict[str, OcrResult], float]:
    started = time.perf_counter()
    ocr = _empty_ocr_results()
    requested = (
        frozenset(requested_ocr_fields) if requested_ocr_fields is not None else None
    )
    for field_name, (box, psm) in _ocr_regions_for(
        analysis.raw_classification,
        settings.ocr_mode,
        moves_box=analysis.moves_ocr_box,
    ).items():
        if requested is not None and field_name not in requested:
            continue
        ocr[field_name] = _read_region(
            image,
            engine,
            box,
            psm=psm,
            save_as=_debug_crop_suffix(settings.visible_crop, f"ocr_{field_name}"),
        )
    if analysis.raw_classification == "detail" and not bool(
        analysis.signals.get("moves_tab_anchor_visible")
    ):
        ocr["moves"] = OcrResult("", 0.0)
    return ocr, time.perf_counter() - started


def _parsed_ocr_values(
    ocr: dict[str, OcrResult], *, raw_classification: str = ""
) -> _ParsedOcrValues:
    _ = raw_classification
    move_text = ocr["moves"].text
    special_text = ocr["special_sections"].text
    story_text = ocr["story"].text
    if move_text and not _moves_tab_text_confirmed(special_text):
        ocr["moves"] = OcrResult("", 0.0)
        move_text = ""
    weight = parse_weight_candidate(ocr["weight"].text) or parse_weight_candidate(
        special_text
    )
    height = parse_height_candidate(ocr["height"].text) or parse_height_candidate(
        special_text
    )
    return _ParsedOcrValues(
        cp=parse_cp_candidate(ocr["cp"].text),
        hp=parse_hp_candidate(ocr["hp"].text),
        weight=weight,
        height=height,
        story_text=story_text,
        move_text=move_text,
        special_text=special_text,
    )


def _classified_scan_features(
    raw_classification: str,
    signals: dict[str, SignalValue],
    iv_evidence: _IvEvidence,
    ocr: dict[str, OcrResult],
    parsed: _ParsedOcrValues,
) -> tuple[dict[str, bool], str]:
    features = _features()
    _apply_list_features(features, raw_classification, signals)

    if _detail_feature_candidate(raw_classification, signals):
        power_text = f"{parsed.special_text} {parsed.move_text}"
        features["has_CP"] = parsed.cp is not None or _visual_cp_evidence(
            raw_classification, signals
        )
        features["has_display_name"] = _has_letters(ocr["display_name"].text) or (
            float(signals["name_dark_ratio"]) >= 0.05
            or _visual_display_name_evidence(signals)
        )
        features["has_hp"] = (
            parsed.hp is not None or float(signals["hp_green_ratio"]) >= 0.03
        )
        features["has_weight"] = parsed.weight is not None
        if raw_classification == "detail":
            features["has_moves"] = (
                _has_letters(parsed.move_text)
                or float(signals["moves_dark_ratio"]) >= 0.10
            )
            features["is_shadow"] = _contains_word(
                f"{parsed.move_text} {parsed.special_text}",
                "shadow bonus",
                "frustration",
            )
            features["has_dynamax"] = _contains_word(power_text, *DYNAMAX_KEYWORDS)
            features["has_gigantamax"] = _contains_word(
                power_text, *GIGANTAMAX_KEYWORDS
            )
            if features["has_gigantamax"]:
                features["has_dynamax"] = False
        else:
            features["has_moves"] = False
            features["is_shadow"] = False
            features["has_dynamax"] = False
            features["has_gigantamax"] = False

        features["has_iv"] = raw_classification == "appraisal" and iv_evidence.has_iv
        features["has_iv_complete"] = (
            raw_classification == "appraisal" and iv_evidence.has_iv_complete
        )
        features["has_story"] = (
            raw_classification == "appraisal"
            and story_text_is_complete(parsed.story_text)
        )
        features["has_tag_chips"] = float(signals["tag_edge_ratio"]) >= 0.07
        features["has_height"] = (
            raw_classification == "detail" and parsed.height is not None
        )
        features["has_pokemon_art"] = float(signals["pokemon_art_signal"]) >= 0.08

    classification = _classification_from_features(
        raw_classification, features, signals
    )
    if classification not in {"detail", "appraisal"}:
        _clear_detail_extraction_features(features)
    else:
        features["has_hp"] = (
            parsed.hp is not None
            or float(signals["hp_green_ratio"]) >= 0.03
            or _visual_hp_evidence(signals)
        )
        features["has_moves"] = (
            raw_classification == "detail" and _visual_all_moves_evidence(signals)
        )
    return features, classification


def scan_frame_candidate_with_ocr_fields(
    frame: FrameCandidate,
    settings: ScanSettings,
    requested_ocr_fields: Iterable[str],
) -> FrameScanRecord:
    return _scan_frame_candidate(
        frame,
        settings,
        requested_ocr_fields=frozenset(requested_ocr_fields),
    )


def scan_frame_candidate_for_production_export_fields(
    frame: FrameCandidate,
    settings: ScanSettings,
    requested_export_fields: Iterable[str],
) -> FrameScanRecord:
    return _scan_frame_candidate(
        frame,
        settings,
        requested_ocr_fields=None,
        requested_export_fields=frozenset(requested_export_fields),
    )


def scan_frame_candidate(
    frame: FrameCandidate, settings: ScanSettings
) -> FrameScanRecord:
    return _scan_frame_candidate(
        frame,
        settings,
        requested_ocr_fields=None,
        requested_export_fields=None,
    )


# pylint: disable-next=too-many-statements
def _scan_frame_candidate(
    frame: FrameCandidate,
    settings: ScanSettings,
    *,
    requested_ocr_fields: frozenset[str] | None,
    requested_export_fields: frozenset[str] | None = None,
) -> FrameScanRecord:
    started = time.perf_counter()
    image, image_load_s = _load_frame_image(frame)
    analysis = _visual_scan_analysis(image, visible_crop=settings.visible_crop)
    signals = analysis.signals
    raw_classification = analysis.raw_classification
    iv_evidence = analysis.iv_evidence
    if requested_export_fields is not None:
        requested_ocr_fields = frozenset(
            _production_ocr_fields_for_export_fields(
                _production_probeable_export_fields(
                    requested_export_fields,
                    raw_classification,
                    signals,
                )
            )
        )
    engine = TesseractOcrEngine(lang=settings.ocr_lang)
    ocr, ocr_s = _read_frame_ocr(
        image,
        engine,
        settings,
        analysis,
        requested_ocr_fields=requested_ocr_fields,
    )

    story_text = ocr["story"].text
    layout = _detail_layout(
        raw_classification=raw_classification,
        signals=signals,
        iv_evidence=iv_evidence,
        story_text=story_text,
    )
    signals["initial_appraisal_layout"] = layout.mode == "initial_appraisal_overlay"
    signals["scrollable_detail_layout"] = layout.mode == "scrollable_detail"
    signals["hp_ocr_fallback_used"] = False
    signals["weight_ocr_fallback_used"] = False
    signals["height_ocr_fallback_used"] = False
    _set_region_signals(signals, "hp_ocr_fallback", [0.0, 0.0, 0.0, 0.0])
    _set_region_signals(signals, "weight_ocr_fallback", [0.0, 0.0, 0.0, 0.0])
    _set_region_signals(signals, "height_ocr_fallback", [0.0, 0.0, 0.0, 0.0])
    if settings.ocr_mode != "off" and raw_classification in {"detail", "appraisal"}:
        cp_requested = requested_ocr_fields is None or "cp" in requested_ocr_fields
        cp_needs_recovery = (
            parse_cp_candidate(ocr["cp"].text) is None
            or not _cp_text_has_prefix(ocr["cp"].text)
            or raw_classification == "appraisal"
        )
        if cp_requested and cp_needs_recovery:
            ocr["cp"] = _recover_cp_ocr(
                image,
                engine,
                layout,
                ocr["cp"],
                visible_crop=settings.visible_crop,
            )
        if parse_hp_candidate(ocr["hp"].text) is None:
            if requested_ocr_fields is None or "hp" in requested_ocr_fields:
                ocr["hp"], selected_hp_box = _recover_hp_ocr(
                    image,
                    engine,
                    layout,
                    ocr["hp"],
                    visible_crop=settings.visible_crop,
                )
                if selected_hp_box is not None:
                    signals["hp_ocr_fallback_used"] = True
                    _set_region_signals(signals, "hp_ocr_fallback", selected_hp_box)
        if requested_ocr_fields is None or "weight" in requested_ocr_fields:
            scrolled_moves_layout = bool(signals.get("moves_tab_anchor_visible"))
            if (
                scrolled_moves_layout
                or parse_weight_candidate(ocr["weight"].text) is None
            ):
                ocr["weight"], selected_weight_box = _recover_weight_ocr(
                    image,
                    engine,
                    ocr["weight"],
                    layout=layout,
                    prefer_fallback=scrolled_moves_layout,
                    visible_crop=settings.visible_crop,
                )
                if selected_weight_box is not None:
                    signals["weight_ocr_fallback_used"] = True
                    _set_region_signals(
                        signals, "weight_ocr_fallback", selected_weight_box
                    )
        if (
            requested_ocr_fields is None or "height" in requested_ocr_fields
        ) and raw_classification == "detail":
            scrolled_moves_layout = bool(signals.get("moves_tab_anchor_visible"))
            if (
                scrolled_moves_layout
                or parse_height_candidate(ocr["height"].text) is None
            ):
                ocr["height"], selected_height_box = _recover_height_ocr(
                    image,
                    engine,
                    ocr["height"],
                    layout=layout,
                    prefer_fallback=scrolled_moves_layout,
                    visible_crop=settings.visible_crop,
                )
                if selected_height_box is not None:
                    signals["height_ocr_fallback_used"] = True
                    _set_region_signals(
                        signals, "height_ocr_fallback", selected_height_box
                    )

    parsed = _parsed_ocr_values(ocr, raw_classification=raw_classification)
    signals["appraisal_story_moves_fallback_used"] = False
    features, classification = _classified_scan_features(
        raw_classification, signals, iv_evidence, ocr, parsed
    )
    values = _scan_values(parsed, iv_evidence)
    _apply_source_payload_values(frame, features, values, signals)
    notes = _scan_notes(features, tesseract_available=engine.is_available())

    return FrameScanRecord(
        source_file=_source_file_name(frame.source_asset),
        source_type=frame.source_asset.source_type,
        frame_path=str(frame.frame_path),
        frame_index=frame.frame_index,
        timestamp_s=frame.timestamp_s,
        classification=classification,
        raw_classification=raw_classification,
        features=features,
        values=values,
        signals=signals,
        ocr={key: _ocr_payload(result) for key, result in ocr.items()},
        timing={
            "image_load_s": round(image_load_s, 6),
            "visual_analysis_s": round(analysis.duration_s, 6),
            "ocr_s": round(ocr_s, 6),
            "total_s": round(time.perf_counter() - started, 6),
        },
        notes=notes,
    )


def _apply_source_payload_values(
    frame: FrameCandidate,
    features: dict[str, bool],
    values: dict[str, object | None],
    signals: dict[str, SignalValue],
) -> None:
    payload = frame.source_payload
    if not isinstance(payload, dict):
        return
    payload_values = payload.get("values")
    payload_features = payload.get("features")
    if not isinstance(payload_values, dict) or not isinstance(payload_features, dict):
        return
    if payload.get("classification") not in {"detail", "appraisal"}:
        return
    if payload_features.get("has_CP") is not True:
        return
    source_cp = payload_values.get("cp")
    if not isinstance(source_cp, int) or isinstance(source_cp, bool):
        return
    source_cp = _valid_cp_value(source_cp)
    if source_cp is None:
        return
    if values.get("cp") != source_cp:
        signals["source_payload_cp_used"] = True
    values["cp"] = source_cp
    features["has_CP"] = True


def _failed_record(
    frame: FrameCandidate, *, error: str, attempts: int
) -> FrameScanRecord:
    return FrameScanRecord(
        source_file=_source_file_name(frame.source_asset),
        source_type=frame.source_asset.source_type,
        frame_path=str(frame.frame_path),
        frame_index=frame.frame_index,
        timestamp_s=frame.timestamp_s,
        classification=NON_EXTRACTABLE_CLASS,
        raw_classification="error",
        features=_features(),
        attempts=attempts,
        error=error,
        notes=["Frame analysis failed after retries."],
    )


def _record_with_attempts(record: FrameScanRecord, attempts: int) -> FrameScanRecord:
    record.attempts = attempts
    return record


def _sequence_motion_sample(record: FrameScanRecord) -> Image.Image | None:
    try:
        with Image.open(record.frame_path) as opened:
            image = opened.convert("RGB")
    except (FileNotFoundError, OSError):
        return None
    return _sequence_motion_sample_from_image(image)


def _mean_absolute_delta(left: Image.Image | None, right: Image.Image | None) -> float:
    if left is None or right is None:
        return 0.0
    diff = ImageChops.difference(left, right)
    histogram = diff.histogram()
    total = sum(histogram)
    return (
        sum(value * count for value, count in enumerate(histogram))
        / max(1, total)
        / 255
    )


def _is_sequence_transition_candidate(record: FrameScanRecord) -> bool:
    if record.classification not in {"detail", "appraisal"}:
        return False
    if record.raw_classification == "error":
        return False
    return bool(record.signals.get("stable_detail_signal"))


def _mark_sequence_transition(record: FrameScanRecord) -> None:
    preserve_has_moves = (
        record.raw_classification == "detail"
        and _safe_visual_all_moves_evidence(record.signals)
    )
    record.classification = NON_EXTRACTABLE_CLASS
    record.signals["sequence_transition_signal"] = True
    _clear_detail_extraction_features(record.features)
    record.features["has_transition"] = True
    if preserve_has_moves:
        record.features["has_moves"] = True
    note = "Source-local transition evidence marked this frame as a transition."
    if note not in record.notes:
        record.notes.append(note)


def _record_has_transition(record: FrameScanRecord) -> bool:
    return bool(record.features.get("has_transition")) or bool(
        record.signals.get("sequence_transition_signal")
    )


def _is_iv_appraisal_segment_record(record: FrameScanRecord) -> bool:
    return (
        record.classification == "appraisal"
        and record.raw_classification == "appraisal"
        and not _record_has_transition(record)
    )


def _is_iv_complete_candidate(record: FrameScanRecord) -> bool:
    return (
        _is_iv_appraisal_segment_record(record)
        and bool(record.features.get("has_iv"))
        and record.signals.get("iv_bar_count") == 3
        and record.values.get("iv_star_agreement") is True
    )


def _refresh_iv_note(record: FrameScanRecord) -> None:
    record.notes = [note for note in record.notes if note != IV_INCOMPLETE_NOTE]
    if record.features.get("has_iv") and not record.features.get("has_iv_complete"):
        record.notes.append(IV_INCOMPLETE_NOTE)


def _select_sequence_iv_complete(records: list[FrameScanRecord]) -> None:
    for record in records:
        record.features["has_iv_complete"] = False

    candidates: list[FrameScanRecord] = []

    def select_latest_candidate() -> None:
        if candidates:
            candidates[-1].features["has_iv_complete"] = True
            candidates.clear()

    for record in records:
        if not _is_iv_appraisal_segment_record(record):
            select_latest_candidate()
            continue
        if _is_iv_complete_candidate(record):
            candidates.append(record)
    select_latest_candidate()

    for record in records:
        _refresh_iv_note(record)


def _is_power_feature_segment_record(record: FrameScanRecord) -> bool:
    return (
        record.classification == "detail"
        and record.raw_classification == "detail"
        and not _record_has_transition(record)
    )


def _record_ocr_text(record: FrameScanRecord, *fields: str) -> str:
    fragments: list[str] = []
    for field_name in fields:
        payload = record.ocr.get(field_name)
        if payload is None:
            continue
        text = payload.get("text")
        if isinstance(text, str):
            fragments.append(text)
    return " ".join(fragments)


def _has_power_section_context(record: FrameScanRecord) -> bool:
    if record.features.get("has_dynamax") or record.features.get("has_gigantamax"):
        return True
    if record.features.get("has_moves"):
        return True
    text = _record_ocr_text(record, "special_sections", "moves")
    return _contains_word(text, *POWER_SECTION_CONTEXT_KEYWORDS)


def _postprocess_power_feature_sequences(records: list[FrameScanRecord]) -> None:
    segment: list[FrameScanRecord] = []

    def flush_segment() -> None:
        if not segment:
            return

        has_gigantamax = any(
            record.features.get("has_gigantamax") for record in segment
        )
        has_dynamax = any(record.features.get("has_dynamax") for record in segment)

        if has_gigantamax:
            for record in segment:
                if not _has_power_section_context(record):
                    continue
                if not record.features.get("has_gigantamax"):
                    record.signals["sequence_gigantamax_signal"] = True
                if record.features.get("has_dynamax"):
                    record.signals["sequence_dynamax_suppressed_by_gigantamax"] = True
                record.features["has_gigantamax"] = True
                record.features["has_dynamax"] = False
        elif has_dynamax:
            for record in segment:
                if not _has_power_section_context(record):
                    continue
                if not record.features.get("has_dynamax"):
                    record.signals["sequence_dynamax_signal"] = True
                record.features["has_dynamax"] = True

        segment.clear()

    for record in records:
        record.signals.pop("sequence_gigantamax_signal", None)
        record.signals.pop("sequence_dynamax_signal", None)
        record.signals.pop("sequence_dynamax_suppressed_by_gigantamax", None)

        if _is_power_feature_segment_record(record):
            segment.append(record)
            continue

        flush_segment()

    flush_segment()


def _record_cp_value(record: FrameScanRecord) -> int | None:
    value = record.values.get("cp")
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def select_cp_consensus_value(
    values: Iterable[object],
) -> tuple[int | None, set[int], set[int]]:
    value_list = [
        value
        for value in values
        if isinstance(value, int)
        and not isinstance(value, bool)
        and _valid_cp_value(value) is not None
    ]
    if not value_list:
        return None, set(), set()

    counted = Counter(value_list)
    if len(counted) == 1:
        return value_list[0], set(), set()

    high_counted = Counter(value for value in value_list if value >= 100)
    if high_counted:
        suffix_selected = _select_cp_without_suffix_pollution(high_counted)
        if suffix_selected is not None:
            return suffix_selected, set(counted) - {suffix_selected}, set()

        if len(high_counted) == 1:
            selected = next(iter(high_counted))
            return selected, set(counted) - {selected}, set()

        ranked_high = high_counted.most_common()
        selected, selected_count = ranked_high[0]
        runner_up_count = ranked_high[1][1]
        if (
            selected_count >= CP_CONSENSUS_MIN_COUNT
            and selected_count >= runner_up_count * CP_CONSENSUS_MIN_RATIO
        ):
            return selected, set(counted) - {selected}, set()

        return None, set(), set(counted)

    ranked = counted.most_common()
    selected, selected_count = ranked[0]
    runner_up_count = ranked[1][1]
    if (
        selected_count >= CP_CONSENSUS_MIN_COUNT
        and selected_count >= runner_up_count * CP_CONSENSUS_MIN_RATIO
    ):
        return selected, set(counted) - {selected}, set()
    return None, set(), set(counted)


def _select_cp_without_suffix_pollution(counted: Counter[int]) -> int | None:
    for value, count in sorted(counted.items(), key=lambda item: (-item[1], item[0])):
        text = str(value)
        if len(text) < 3:
            continue
        suffix_variants = {
            other
            for other in counted
            if other != value
            and len(str(other)) == len(text) + 1
            and str(other).startswith(text)
        }
        if suffix_variants and count >= CP_CONSENSUS_MIN_COUNT:
            return value
        if len(suffix_variants) == 1:
            suffix_value = next(iter(suffix_variants))
            if counted[suffix_value] >= CP_CONSENSUS_MIN_RATIO:
                return value

    prefix_groups: dict[int, Counter[int]] = {}
    for value, count in counted.items():
        text = str(value)
        if len(text) < 4:
            continue
        prefix = int(text[:-1])
        if _valid_cp_value(prefix) is None:
            continue
        prefix_groups.setdefault(prefix, Counter())[value] = count
    ranked_prefixes = sorted(
        prefix_groups.items(),
        key=lambda item: (-sum(item[1].values()), item[0]),
    )
    for prefix, variants in ranked_prefixes:
        if len(variants) >= 4 and sum(variants.values()) >= CP_CONSENSUS_MIN_COUNT:
            return prefix
    return None


def _record_hp_value(record: FrameScanRecord) -> str | None:
    value = record.values.get("hp")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _record_weight_value(record: FrameScanRecord) -> str | None:
    value = record.values.get("weight_kg")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_weight_propagation_segment_record(record: FrameScanRecord) -> bool:
    return (
        record.classification in {"detail", "appraisal"}
        and record.raw_classification in {"detail", "appraisal"}
        and not _record_has_transition(record)
        and _record_hp_value(record) is not None
    )


def _clear_sequence_weight_propagation(record: FrameScanRecord) -> None:
    if record.signals.get("sequence_weight_propagated") is True:
        if record.values.get("weight_kg") == record.signals.get(
            "sequence_weight_value"
        ):
            record.values["weight_kg"] = None
            record.features["has_weight"] = False
    if record.signals.get("sequence_weight_corrected") is True:
        if record.values.get("weight_kg") == record.signals.get(
            "sequence_weight_value"
        ):
            record.values["weight_kg"] = record.signals.get(
                "sequence_weight_original_value"
            )
            record.features["has_weight"] = _record_weight_value(record) is not None
    for key in (
        "sequence_weight_propagated",
        "sequence_weight_corrected",
        "sequence_weight_value",
        "sequence_weight_original_value",
        "sequence_weight_group_size",
    ):
        record.signals.pop(key, None)
    record.notes = [
        note
        for note in record.notes
        if note not in {WEIGHT_PROPAGATED_NOTE, WEIGHT_CORRECTED_NOTE}
    ]


def _dominant_sequence_weight(weights: Iterable[str]) -> str | None:
    counted = Counter(weights)
    if not counted:
        return None
    if len(counted) == 1:
        return next(iter(counted))

    ranked = counted.most_common(2)
    selected, selected_count = ranked[0]
    runner_up_count = ranked[1][1]
    total = sum(counted.values())
    if selected_count < 3:
        return None
    if selected_count < runner_up_count * 3:
        return None
    if selected_count / total < 0.75:
        return None
    return selected


def _apply_weight_propagation_segment(segment: list[FrameScanRecord]) -> None:
    weight_values = [
        value
        for record in segment
        if (value := _record_weight_value(record)) is not None
    ]
    weight = _dominant_sequence_weight(weight_values)
    if weight is None:
        return

    for record in segment:
        current_weight = _record_weight_value(record)
        if current_weight == weight:
            continue
        if current_weight is not None:
            record.values["weight_kg"] = weight
            record.features["has_weight"] = True
            record.signals["sequence_weight_corrected"] = True
            record.signals["sequence_weight_value"] = cast(SignalValue, weight)
            record.signals["sequence_weight_original_value"] = cast(
                SignalValue, current_weight
            )
            record.signals["sequence_weight_group_size"] = len(segment)
            record.notes.append(WEIGHT_CORRECTED_NOTE)
            continue
        record.values["weight_kg"] = weight
        record.features["has_weight"] = True
        record.signals["sequence_weight_propagated"] = True
        record.signals["sequence_weight_value"] = cast(SignalValue, weight)
        record.signals["sequence_weight_group_size"] = len(segment)
        record.notes.append(WEIGHT_PROPAGATED_NOTE)


def _postprocess_weight_sequences(records: list[FrameScanRecord]) -> None:
    segment: list[FrameScanRecord] = []
    segment_hp: str | None = None

    def flush_segment() -> None:
        nonlocal segment_hp
        _apply_weight_propagation_segment(segment)
        segment.clear()
        segment_hp = None

    for record in records:
        _clear_sequence_weight_propagation(record)

        if record.classification == "list" or record.raw_classification == "list":
            flush_segment()
            continue

        hp_value = _record_hp_value(record)
        if segment and hp_value is not None and hp_value != segment_hp:
            flush_segment()

        if not _is_weight_propagation_segment_record(record):
            continue

        if not segment:
            segment_hp = hp_value
        segment.append(record)

    flush_segment()


def _is_cp_consensus_segment_record(record: FrameScanRecord) -> bool:
    return (
        record.classification in {"detail", "appraisal"}
        and record.raw_classification in {"detail", "appraisal"}
        and not _record_has_transition(record)
        and _record_hp_value(record) is not None
    )


def _apply_cp_consensus_segment(segment: list[FrameScanRecord]) -> None:
    cp_values = [
        value for record in segment if (value := _record_cp_value(record)) is not None
    ]
    consensus_value, ignored_values, _unresolved_values = select_cp_consensus_value(
        cp_values
    )
    if consensus_value is None or not ignored_values:
        return

    for record in segment:
        original_value = _record_cp_value(record)
        if original_value is None or original_value == consensus_value:
            continue
        record.values["cp"] = consensus_value
        record.signals["cp_consensus_corrected"] = True
        record.signals["cp_original_value"] = original_value
        record.signals["cp_consensus_value"] = consensus_value
        record.signals["cp_consensus_group_size"] = len(segment)


def _postprocess_cp_consensus_sequences(records: list[FrameScanRecord]) -> None:
    segment: list[FrameScanRecord] = []
    segment_hp: str | None = None

    def flush_segment() -> None:
        nonlocal segment_hp
        _apply_cp_consensus_segment(segment)
        segment.clear()
        segment_hp = None

    for record in records:
        for key in (
            "cp_consensus_corrected",
            "cp_original_value",
            "cp_consensus_value",
            "cp_consensus_group_size",
        ):
            record.signals.pop(key, None)

        hp_value = (
            _record_hp_value(record)
            if _is_cp_consensus_segment_record(record)
            else None
        )
        if hp_value is None:
            flush_segment()
            continue
        if segment and hp_value != segment_hp:
            flush_segment()
        segment.append(record)
        segment_hp = hp_value

    flush_segment()


def _is_appraisal_transition_motion(
    previous_delta: float, future_delta: float, previous_marked_transition: bool
) -> bool:
    if previous_delta >= 0.04 and future_delta >= 0.04:
        return True
    if previous_marked_transition and previous_delta <= 0.005 and future_delta >= 0.04:
        return True
    return (
        previous_marked_transition and previous_delta >= 0.032 and future_delta >= 0.035
    )


def _is_detail_transition_motion(
    previous_delta: float,
    future_delta: float,
    previous_marked_transition: bool,
    horizontal_swipe_signal: bool,
) -> bool:
    if horizontal_swipe_signal and previous_delta >= 0.06 and future_delta >= 0.04:
        return True
    if not previous_marked_transition:
        return False
    if previous_delta <= 0.005 and future_delta >= 0.04:
        return True
    if previous_delta >= 0.03 and future_delta >= 0.035:
        return True
    return previous_delta >= 0.06 and future_delta >= 0.03


def _is_transition_motion(
    record: FrameScanRecord,
    previous_delta: float,
    future_delta: float,
    previous_marked_transition: bool,
) -> bool:
    if bool(record.signals.get("hp_area_card_split_signal")):
        return True
    if record.raw_classification == "appraisal":
        return _is_appraisal_transition_motion(
            previous_delta, future_delta, previous_marked_transition
        )
    if record.raw_classification == "detail":
        return _is_detail_transition_motion(
            previous_delta,
            future_delta,
            previous_marked_transition,
            bool(record.signals.get("horizontal_swipe_signal")),
        )
    return False


def _postprocess_source_sequence(records: list[FrameScanRecord]) -> None:
    if len(records) <= 1:
        _postprocess_cp_consensus_sequences(records)
        _postprocess_weight_sequences(records)
        _postprocess_power_feature_sequences(records)
        _select_sequence_iv_complete(records)
        return

    samples = [_sequence_motion_sample(record) for record in records]
    previous_delta_by_index: list[float] = []
    future_delta_by_index: list[float] = []
    for index, record in enumerate(records):
        previous_delta = (
            _mean_absolute_delta(samples[index - 1], samples[index])
            if index > 0
            else 0.0
        )
        future_index = index + 3
        future_delta = (
            _mean_absolute_delta(samples[index], samples[future_index])
            if future_index < len(samples)
            else 0.0
        )
        previous_delta_by_index.append(previous_delta)
        future_delta_by_index.append(future_delta)
        record.signals["previous_frame_delta"] = round(previous_delta, 4)
        record.signals["future_frame_delta_3"] = round(future_delta, 4)

    previous_marked_transition = False
    for index, record in enumerate(records):
        if not _is_sequence_transition_candidate(record):
            previous_marked_transition = False
            continue

        previous_delta = previous_delta_by_index[index]
        future_delta = future_delta_by_index[index]
        is_transition = _is_transition_motion(
            record, previous_delta, future_delta, previous_marked_transition
        )

        if is_transition:
            _mark_sequence_transition(record)
        previous_marked_transition = is_transition

    _postprocess_cp_consensus_sequences(records)
    _postprocess_weight_sequences(records)
    _postprocess_power_feature_sequences(records)
    _select_sequence_iv_complete(records)


def _postprocess_frame_sequences(records: list[FrameScanRecord]) -> None:
    source_records: dict[str, list[FrameScanRecord]] = {}
    for record in records:
        source_records.setdefault(record.source_file, []).append(record)
    for sequence in source_records.values():
        sequence.sort(key=lambda record: record.frame_index)
        _postprocess_source_sequence(sequence)


def _is_production_sequence_candidate(record: FrameVisualRecord) -> bool:
    return record.raw_classification in {"detail", "appraisal"} and bool(
        record.signals.get("stable_detail_signal")
    )


def _is_visual_transition_motion(
    record: FrameVisualRecord,
    previous_delta: float,
    future_delta: float,
    previous_marked_transition: bool,
) -> bool:
    if bool(record.signals.get("hp_area_card_split_signal")):
        return True
    if record.raw_classification == "appraisal":
        return _is_appraisal_transition_motion(
            previous_delta, future_delta, previous_marked_transition
        )
    if record.raw_classification == "detail":
        return _is_detail_transition_motion(
            previous_delta,
            future_delta,
            previous_marked_transition,
            bool(record.signals.get("horizontal_swipe_signal")),
        )
    return False


def _production_sequence_transition_flags(
    records: list[FrameVisualRecord],
) -> list[bool]:
    previous_delta_by_index: list[float] = []
    future_delta_by_index: list[float] = []
    for index, record in enumerate(records):
        previous_delta = (
            _mean_absolute_delta(records[index - 1].motion_sample, record.motion_sample)
            if index > 0
            else 0.0
        )
        future_index = index + 3
        future_delta = (
            _mean_absolute_delta(
                record.motion_sample, records[future_index].motion_sample
            )
            if future_index < len(records)
            else 0.0
        )
        previous_delta_by_index.append(previous_delta)
        future_delta_by_index.append(future_delta)
        record.signals["previous_frame_delta"] = round(previous_delta, 4)
        record.signals["future_frame_delta_3"] = round(future_delta, 4)

    flags: list[bool] = []
    previous_marked_transition = False
    for index, record in enumerate(records):
        if not _is_production_sequence_candidate(record):
            flags.append(False)
            previous_marked_transition = False
            continue

        is_transition = _is_visual_transition_motion(
            record,
            previous_delta_by_index[index],
            future_delta_by_index[index],
            previous_marked_transition,
        )
        flags.append(is_transition)
        previous_marked_transition = is_transition
    return flags


def group_production_sequences(
    visual_records: Iterable[FrameVisualRecord],
) -> list[list[FrameVisualRecord]]:
    source_records: dict[str, list[FrameVisualRecord]] = {}
    for record in visual_records:
        source_records.setdefault(record.source_file, []).append(record)

    sequences: list[list[FrameVisualRecord]] = []
    for records in source_records.values():
        ordered = sorted(records, key=lambda record: record.frame_index)
        transition_flags = _production_sequence_transition_flags(ordered)
        current: list[FrameVisualRecord] = []
        for record, is_transition in zip(ordered, transition_flags, strict=True):
            same_logical_run = (
                current and record.raw_classification == current[-1].raw_classification
            )
            if (
                not _is_production_sequence_candidate(record)
                or is_transition
                or (current and not same_logical_run)
            ):
                if current:
                    sequences.append(current)
                    current = []
                if not _is_production_sequence_candidate(record) or is_transition:
                    continue
            current.append(record)
        if current:
            sequences.append(current)
    return sequences


def _safe_visual_all_moves_evidence(signals: dict[str, SignalValue]) -> bool:
    try:
        return _visual_all_moves_evidence(signals)
    except KeyError:
        return False


def _safe_visual_power_section_evidence(signals: dict[str, SignalValue]) -> bool:
    try:
        return _visual_power_section_evidence(signals)
    except KeyError:
        return False


def _production_desired_export_fields(
    sequence: Iterable[FrameVisualRecord],
) -> set[str]:
    records = list(sequence)
    if not records:
        return set()

    raw_classification = records[0].raw_classification
    if raw_classification == "appraisal":
        return set(PRODUCTION_APPRAISAL_USEFUL_FIELD_NAMES)

    return set(PRODUCTION_DETAIL_BASE_USEFUL_FIELD_NAMES) | set(
        PRODUCTION_DETAIL_VISUAL_USEFUL_FIELD_NAMES
    )


def _visual_power_section_evidence(signals: dict[str, SignalValue]) -> bool:
    return (
        bool(signals.get("moves_tab_anchor_visible"))
        or float(signals["pokemon_art_signal"]) >= 0.08
        or float(signals.get("tag_edge_ratio", 0.0)) >= 0.07
    )


# pylint: disable-next=too-many-branches
def _production_probeable_export_fields(
    export_fields: Iterable[str],
    raw_classification: str,
    signals: dict[str, SignalValue],
) -> set[str]:
    probeable: set[str] = set()
    detail_like = raw_classification in {"detail", "appraisal"}
    for field_name in export_fields:
        if field_name in {
            "iv",
            "iv_star_agreement",
            "appraisal_perfect",
            "appraisal_star_count",
            "iv_sum",
        }:
            if raw_classification == "appraisal":
                probeable.add(field_name)
            continue
        if field_name == "tag":
            if detail_like:
                probeable.add(field_name)
            continue
        if field_name in {"is_shadow", "has_dynamax", "has_gigantamax"}:
            if raw_classification == "detail":
                probeable.add(field_name)
            continue
        if field_name == "moves":
            if raw_classification == "detail" and _visual_all_moves_evidence(signals):
                probeable.add(field_name)
            continue
        if field_name == "power":
            if raw_classification == "detail" and _visual_power_section_evidence(
                signals
            ):
                probeable.add(field_name)
            continue
        if field_name == "cp":
            if raw_classification == "appraisal" or (
                detail_like and _visual_cp_evidence(raw_classification, signals)
            ):
                probeable.add(field_name)
            continue
        if field_name == "display_name":
            if detail_like and (
                float(signals["name_dark_ratio"]) >= 0.05
                or _visual_display_name_evidence(signals)
            ):
                probeable.add(field_name)
            continue
        if field_name in {"hp", "weight"}:
            if detail_like:
                probeable.add(field_name)
            continue
        if field_name == "height":
            if raw_classification == "detail":
                probeable.add(field_name)
            continue
        if field_name == "story":
            if raw_classification == "appraisal":
                probeable.add(field_name)
            continue
        probeable.add(field_name)
    return probeable


def _production_ocr_fields_for_export_fields(
    export_fields: Iterable[str],
) -> tuple[str, ...]:
    ocr_fields: set[str] = set()
    for field_name in export_fields:
        ocr_fields.update(PRODUCTION_OCR_FIELDS_BY_EXPORT_FIELD.get(field_name, ()))
    return tuple(sorted(ocr_fields))


def _record_ocr_string(record: FrameScanRecord, field_name: str) -> str:
    payload = record.ocr.get(field_name, {})
    text = payload.get("text")
    return text.strip() if isinstance(text, str) else ""


def _record_has_iv_triplet(record: FrameScanRecord) -> bool:
    return all(
        isinstance(record.values.get(field_name), int)
        for field_name in ("iv_attack", "iv_defense", "iv_stamina")
    )


# pylint: disable-next=too-many-branches
def _production_export_field_values(record: FrameScanRecord) -> dict[str, object]:
    fields: dict[str, object] = {}
    cp = _record_cp_value(record)
    if cp is not None:
        fields["cp"] = cp
    hp = _record_hp_value(record)
    if hp is not None:
        fields["hp"] = hp
    weight = record.values.get("weight_kg")
    if isinstance(weight, str) and weight.strip():
        fields["weight"] = weight.strip()
    height = record.values.get("height_m")
    if isinstance(height, str) and height.strip():
        fields["height"] = height.strip()

    display_name = _record_ocr_string(record, "display_name")
    if display_name and record.features.get("has_display_name"):
        fields["display_name"] = display_name

    moves = _record_ocr_string(record, "moves")
    if moves and record.features.get("has_moves"):
        fields["moves"] = moves
    power = _record_ocr_string(record, "special_sections")
    if power:
        fields["power"] = power

    if record.raw_classification == "appraisal":
        story_text = record.values.get("story_text")
        if isinstance(story_text, str) and story_text_is_complete(story_text):
            fields["story"] = story_text.strip()

        if record.features.get("has_iv_complete") and _record_has_iv_triplet(record):
            fields["iv"] = (
                record.values["iv_attack"],
                record.values["iv_defense"],
                record.values["iv_stamina"],
            )
        iv_star_agreement = record.values.get("iv_star_agreement")
        if iv_star_agreement is True:
            fields["iv_star_agreement"] = True
        for field_name in ("iv_sum", "appraisal_star_count"):
            value = record.values.get(field_name)
            if isinstance(value, int) and not isinstance(value, bool):
                fields[field_name] = value
        appraisal_perfect = record.values.get("appraisal_perfect")
        if appraisal_perfect is True:
            fields["appraisal_perfect"] = True
    if record.features.get("has_tag_chips"):
        fields["tag"] = True
    for feature_name in ("is_shadow", "has_dynamax", "has_gigantamax"):
        if record.features.get(feature_name):
            fields[feature_name] = True
    return fields


@dataclass(slots=True)
class _ProductionRecordAccumulator:
    desired_fields: set[str]
    sequence_type: str
    accepted_fields: dict[str, object] = field(default_factory=dict)
    conflicts: dict[str, set[object]] = field(default_factory=dict)
    confirmed_fields: set[str] = field(default_factory=set)
    cp_candidates: list[int] = field(default_factory=list)
    probe_attempts: dict[str, int] = field(default_factory=dict)
    attempted_probe_fields_by_frame: dict[int, set[str]] = field(default_factory=dict)

    def fields_to_probe(self) -> set[str]:
        missing = self.desired_fields - self.accepted_fields.keys()
        if "cp" in self.desired_fields:
            if self._probe_budget_available("cp"):
                missing.add("cp")
            else:
                missing.discard("cp")
        return {
            field_name
            for field_name in missing | self.conflicts.keys()
            if self._probe_budget_available(field_name)
        }

    def exhausted_probe_fields(self) -> set[str]:
        missing = self.desired_fields - self.accepted_fields.keys()
        unresolved = missing | self.conflicts.keys()
        if "cp" in self.desired_fields:
            unresolved.add("cp")
        return {
            field_name
            for field_name in unresolved
            if not self._probe_budget_available(field_name)
        }

    def unattempted_fields_for_frame(
        self, frame_index: int, export_fields: Iterable[str]
    ) -> set[str]:
        attempted = self.attempted_probe_fields_by_frame.get(frame_index, set())
        return set(export_fields) - attempted

    def record_probe_attempts(
        self, frame_index: int, export_fields: Iterable[str]
    ) -> None:
        attempted = self.attempted_probe_fields_by_frame.setdefault(frame_index, set())
        for field_name in export_fields:
            if field_name in attempted:
                continue
            attempted.add(field_name)
            self.probe_attempts[field_name] = self.probe_attempts.get(field_name, 0) + 1

    def accept_record(self, record: FrameScanRecord) -> None:
        for field_name, value in _production_export_field_values(record).items():
            if field_name == "cp":
                self._accept_cp_value(value)
                continue
            current = self.accepted_fields.get(field_name)
            if current is None:
                self.accepted_fields[field_name] = value
                continue
            if field_name in PRODUCTION_NON_CONFLICTING_FIELD_NAMES:
                continue
            if current != value:
                self.conflicts.setdefault(field_name, {current}).add(value)
            else:
                self.confirmed_fields.add(field_name)

    def _accept_cp_value(self, value: object) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            return
        self.cp_candidates.append(value)
        selected, _ignored, unresolved = select_cp_consensus_value(self.cp_candidates)
        if selected is None:
            self.accepted_fields.pop("cp", None)
            self.confirmed_fields.discard("cp")
            if unresolved:
                self.conflicts.pop("cp", None)
            return

        current = self.accepted_fields.get("cp")
        self.accepted_fields["cp"] = selected
        self.conflicts.pop("cp", None)
        if (
            current == selected
            or self.cp_candidates.count(selected) >= CP_CONSENSUS_MIN_COUNT
        ):
            self.confirmed_fields.add("cp")

    def _probe_budget_available(self, field_name: str) -> bool:
        if field_name == "cp":
            return (
                self.probe_attempts.get(field_name, 0)
                < PRODUCTION_CP_PROBE_FRAME_BUDGET
            )
        if field_name in {"height", "weight"}:
            return (
                self.probe_attempts.get(field_name, 0)
                < PRODUCTION_PHYSICAL_PROBE_FRAME_BUDGET
            )
        return True

    def has_core_identity(self) -> bool:
        return {"hp", "weight"}.issubset(self.accepted_fields.keys())

    def has_appraisal_anchor(self) -> bool:
        return (
            self.has_core_identity()
            and "story" in self.accepted_fields
            and "iv" in self.accepted_fields
        )

    def has_detail_anchor(self) -> bool:
        return self.has_core_identity() and "moves" in self.accepted_fields

    def has_anchor(self) -> bool:
        if self.sequence_type == "detail/raw=appraisal":
            return self.has_appraisal_anchor()
        return self.has_detail_anchor()

    def completion_blockers(self) -> set[str]:
        blockers = set(self.desired_fields - self.accepted_fields.keys())
        if not self.has_anchor():
            blockers.add("anchor")
        blockers.update(self.conflicts)
        return blockers

    def is_complete(self) -> bool:
        return self.has_anchor() and not self.conflicts


def _production_probeable_fields_for_visual_record(
    export_fields: Iterable[str],
    visual_record: FrameVisualRecord,
) -> set[str]:
    return _production_probeable_export_fields(
        export_fields,
        visual_record.raw_classification,
        visual_record.signals,
    )


def _production_sequence_warning(
    sequence: list[FrameVisualRecord], message: str
) -> str:
    source_file = sequence[0].source_file if sequence else "unknown"
    if sequence:
        first_index = sequence[0].frame_index
        last_index = sequence[-1].frame_index
        return f"{source_file} frames {first_index}-{last_index}: {message}"
    return message


def _production_post_completion_probe_fields(
    fields_to_probe: set[str],
    sequence_type: str,
    *,
    power_diagnostic_pending: bool,
) -> set[str]:
    post_completion_fields: set[str] = set()
    if sequence_type == "detail/raw=appraisal":
        if "cp" in fields_to_probe:
            post_completion_fields.add("cp")
        return post_completion_fields
    if "cp" in fields_to_probe:
        post_completion_fields.add("cp")
    if "height" in fields_to_probe:
        post_completion_fields.add("height")
    if power_diagnostic_pending:
        post_completion_fields.add("power")
    return post_completion_fields


def _production_sequence_type(sequence: list[FrameVisualRecord]) -> str:
    raw_classification = sequence[0].raw_classification if sequence else "unknown"
    return f"detail/raw={raw_classification}"


def _postprocess_production_sequence_records(records: list[FrameScanRecord]) -> None:
    ordered = sorted(records, key=lambda record: record.frame_index)
    _postprocess_cp_consensus_sequences(ordered)
    _postprocess_weight_sequences(ordered)
    _postprocess_power_feature_sequences(ordered)
    if any(record.raw_classification == "appraisal" for record in ordered):
        _select_sequence_iv_complete(ordered)


def _production_accumulator_from_records(
    records: Iterable[FrameScanRecord],
    desired_fields: set[str],
    sequence_type: str,
) -> _ProductionRecordAccumulator:
    accumulator = _ProductionRecordAccumulator(desired_fields, sequence_type)
    for record in sorted(records, key=lambda item: item.frame_index, reverse=True):
        accumulator.accept_record(record)
    return accumulator


def refresh_production_sequence_result(result: ProductionSequenceScanResult) -> None:
    accumulator = _production_accumulator_from_records(
        result.records,
        result.desired_fields,
        result.sequence_type,
    )
    result.accepted_fields = dict(accumulator.accepted_fields)
    result.completed = accumulator.is_complete()
    result.completion_reason = (
        "anchor complete"
        if result.completed
        else "missing useful fields or unresolved conflicts"
    )


# pylint: disable-next=too-many-branches,too-many-statements
def scan_production_sequence(
    sequence: list[FrameVisualRecord],
    settings: ScanSettings,
    *,
    scanner: Callable[
        [FrameCandidate, ScanSettings, Iterable[str]], FrameScanRecord
    ] = scan_frame_candidate_with_ocr_fields,
    progress_callback: Callable[[FrameCandidate, tuple[str, ...]], None] | None = None,
) -> ProductionSequenceScanResult:
    desired_fields = _production_desired_export_fields(sequence)
    sequence_type = _production_sequence_type(sequence)
    accumulator = _ProductionRecordAccumulator(desired_fields, sequence_type)
    records: list[FrameScanRecord] = []
    warnings: list[str] = []
    requested_by_frame: dict[int, tuple[str, ...]] = {}
    power_diagnostic_resolved = False

    for visual_record in sorted(
        sequence, key=lambda record: record.frame_index, reverse=True
    ):
        fields_to_probe = accumulator.fields_to_probe()
        if accumulator.is_complete():
            fields_to_probe = _production_post_completion_probe_fields(
                fields_to_probe,
                sequence_type,
                power_diagnostic_pending=(
                    accumulator.accepted_fields.get("has_dynamax") is True
                    and not power_diagnostic_resolved
                ),
            )
        if not fields_to_probe:
            if progress_callback is not None:
                exhausted = accumulator.exhausted_probe_fields()
                if exhausted:
                    progress_callback(
                        visual_record.frame,
                        ("stop:probe_budget_exhausted:" + ",".join(sorted(exhausted)),),
                    )
                else:
                    progress_callback(visual_record.frame, ("stop:no_fields_to_probe",))
            break

        visual_probe_fields = _production_probeable_fields_for_visual_record(
            fields_to_probe,
            visual_record,
        )
        visual_probe_fields = accumulator.unattempted_fields_for_frame(
            visual_record.frame_index,
            visual_probe_fields,
        )
        if not visual_probe_fields:
            if progress_callback is not None:
                already_attempted = accumulator.attempted_probe_fields_by_frame.get(
                    visual_record.frame_index,
                    set(),
                )
                reason = (
                    "skip:probe_already_attempted"
                    if already_attempted
                    else "skip:no_probeable_fields"
                )
                progress_callback(visual_record.frame, (reason,))
            continue

        probe_fields = tuple(sorted(visual_probe_fields))
        accumulator.record_probe_attempts(
            visual_record.frame_index,
            visual_probe_fields,
        )

        if scanner is scan_frame_candidate_with_ocr_fields:
            if progress_callback is not None:
                progress_callback(visual_record.frame, probe_fields)

            record = scan_frame_candidate_for_production_export_fields(
                visual_record.frame,
                settings,
                visual_probe_fields,
            )
            requested_ocr_fields = _production_ocr_fields_for_export_fields(
                _production_probeable_export_fields(
                    visual_probe_fields,
                    record.raw_classification,
                    record.signals,
                )
            )
        else:
            requested_ocr_fields = _production_ocr_fields_for_export_fields(
                visual_probe_fields
            )
            if progress_callback is not None:
                progress_callback(visual_record.frame, probe_fields)

            record = scanner(visual_record.frame, settings, requested_ocr_fields)
        requested_by_frame[visual_record.frame_index] = requested_ocr_fields
        records.append(record)
        accumulator.accept_record(record)
        if record.features.get("has_gigantamax"):
            power_diagnostic_resolved = True

    _postprocess_production_sequence_records(records)
    accumulator = _production_accumulator_from_records(
        records,
        desired_fields,
        sequence_type,
    )

    if accumulator.conflicts:
        conflicts = ", ".join(sorted(accumulator.conflicts))
        warnings.append(
            _production_sequence_warning(
                sequence,
                (
                    f"{sequence_type} conflicting production evidence remained "
                    f"for: {conflicts}."
                ),
            )
        )
    completion_reason = (
        "anchor complete"
        if accumulator.is_complete()
        else "missing useful fields or unresolved conflicts"
    )
    if sequence and not accumulator.is_complete():
        missing = ", ".join(sorted(accumulator.completion_blockers()))
        warnings.append(
            _production_sequence_warning(
                sequence,
                f"{sequence_type} production scan ended before a complete anchor; "
                f"missing or unresolved fields: {missing or 'none'}.",
            )
        )
    elif sequence:
        missing = ", ".join(sorted(desired_fields - accumulator.accepted_fields.keys()))
        if missing:
            completion_reason = (
                f"anchor complete; missing optional useful fields: {missing}"
            )

    return ProductionSequenceScanResult(
        records=records,
        accepted_fields=dict(accumulator.accepted_fields),
        desired_fields=desired_fields,
        requested_ocr_fields_by_frame=requested_by_frame,
        warnings=warnings,
        completed=accumulator.is_complete(),
        sequence_type=sequence_type,
        completion_reason=completion_reason,
    )


def scan_production_sequence_repair(
    sequence: list[FrameVisualRecord],
    settings: ScanSettings,
    *,
    progress_callback: Callable[[FrameCandidate, tuple[str, ...]], None] | None = None,
) -> ProductionSequenceScanResult:
    desired_fields = _production_desired_export_fields(sequence)
    repair_fields = (
        set(PRODUCTION_APPRAISAL_USEFUL_FIELD_NAMES)
        | set(PRODUCTION_DETAIL_BASE_USEFUL_FIELD_NAMES)
        | set(PRODUCTION_DETAIL_VISUAL_USEFUL_FIELD_NAMES)
    )
    sequence_type = _production_sequence_type(sequence)
    records: list[FrameScanRecord] = []
    requested_by_frame: dict[int, tuple[str, ...]] = {}

    for visual_record in _production_repair_records(sequence):
        record = scan_frame_candidate_for_production_export_fields(
            visual_record.frame,
            settings,
            repair_fields,
        )
        requested_ocr_fields = _production_ocr_fields_for_export_fields(
            _production_probeable_export_fields(
                repair_fields,
                record.raw_classification,
                record.signals,
            )
        )
        if progress_callback is not None:
            progress_callback(visual_record.frame, requested_ocr_fields)
        requested_by_frame[visual_record.frame_index] = requested_ocr_fields
        records.append(record)

    _postprocess_production_sequence_records(records)
    accumulator = _production_accumulator_from_records(
        records,
        desired_fields,
        sequence_type,
    )

    warnings: list[str] = []
    if accumulator.conflicts:
        conflicts = ", ".join(sorted(accumulator.conflicts))
        warnings.append(
            _production_sequence_warning(
                sequence,
                (
                    f"{sequence_type} repair scan still has conflicting "
                    f"production evidence for: {conflicts}."
                ),
            )
        )
    if sequence and not accumulator.is_complete():
        missing = ", ".join(sorted(accumulator.completion_blockers()))
        warnings.append(
            _production_sequence_warning(
                sequence,
                f"{sequence_type} repair scan ended before a complete anchor; "
                f"missing or unresolved fields: {missing or 'none'}.",
            )
        )

    return ProductionSequenceScanResult(
        records=records,
        accepted_fields=dict(accumulator.accepted_fields),
        desired_fields=desired_fields,
        requested_ocr_fields_by_frame=requested_by_frame,
        warnings=warnings,
        completed=accumulator.is_complete(),
        sequence_type=sequence_type,
        completion_reason=(
            "repair anchor complete"
            if accumulator.is_complete()
            else "repair missing useful fields or unresolved conflicts"
        ),
    )


def _production_repair_records(
    sequence: list[FrameVisualRecord],
) -> list[FrameVisualRecord]:
    ordered = sorted(sequence, key=lambda record: record.frame_index)
    if len(ordered) <= PRODUCTION_REPAIR_MAX_FRAMES:
        return sorted(ordered, key=lambda record: record.frame_index, reverse=True)

    edge_count = PRODUCTION_REPAIR_MAX_FRAMES // 3
    middle_count = PRODUCTION_REPAIR_MAX_FRAMES - (edge_count * 2)
    middle = ordered[edge_count:-edge_count]
    selected = ordered[:edge_count] + ordered[-edge_count:]
    if middle:
        if middle_count == 1:
            selected.append(middle[len(middle) // 2])
        else:
            last_index = len(middle) - 1
            for step in range(middle_count):
                selected.append(
                    middle[round(step * last_index / max(1, middle_count - 1))]
                )
    return sorted(
        {record.frame_index: record for record in selected}.values(),
        key=lambda record: record.frame_index,
        reverse=True,
    )


def _process_frames_with_retry(
    frames: list[FrameCandidate],
    settings: ScanSettings,
    *,
    processor: Callable[
        [FrameCandidate, ScanSettings], FrameScanRecord
    ] = scan_frame_candidate,
    executor_factory=ThreadPoolExecutor,
    completed_iterator=as_completed,
) -> _FrameProcessingResult:
    records: list[FrameScanRecord] = []
    warnings: list[str] = []
    retry_summary = execute_with_adaptive_retries(
        frames,
        requested_workers=settings.workers,
        max_attempts=settings.max_frame_attempts,
        process_item=lambda frame: processor(frame, settings),
        on_success=lambda frame, record, attempt: records.append(
            _record_with_attempts(record, attempt)
        ),
        on_final_failure=lambda frame, exc, attempt: records.append(
            _failed_record(frame, error=str(exc), attempts=attempt)
        ),
        warnings=warnings,
        build_retry_warning=lambda pending_count, worker_count: (
            f"{pending_count} frame task(s) failed with {worker_count} workers; "
            "requeued with reduced concurrency."
        ),
        executor_factory=executor_factory,
        completed_iterator=completed_iterator,
    )

    records.sort(key=lambda record: (record.source_file, record.frame_index))
    _postprocess_frame_sequences(records)
    return _FrameProcessingResult(
        records,
        warnings,
        retry_summary.worker_count,
        retry_summary.retry_count,
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")


def _write_jsonl(path: Path, records: Iterable[FrameScanRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.to_json_dict(), ensure_ascii=True))
            handle.write("\n")


def _audit_image_src(path: str, audit_path: Path) -> str:
    frame_path = Path(path)
    if not frame_path.is_absolute():
        frame_path = frame_path.resolve()
    try:
        relative_path = os.path.relpath(frame_path, audit_path.parent.resolve())
    except ValueError:
        return frame_path.as_uri()
    return Path(relative_path).as_posix()


def _audit_ocr_text(record: FrameScanRecord, field_name: str) -> str:
    payload = record.ocr.get(field_name) or {}
    text = payload.get("text")
    return " ".join(text.split()) if isinstance(text, str) else ""


def _audit_value_is_present(value: object | None) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _audit_value_items(record: FrameScanRecord) -> list[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    emitted: set[str] = set()

    def add_value(key: str, value: object | None) -> None:
        if _audit_value_is_present(value):
            items.append((key, value if not isinstance(value, str) else value.strip()))
            emitted.add(key)

    def add_value_or_ocr(key: str, ocr_field: str) -> None:
        value = record.values.get(key)
        if not _audit_value_is_present(value):
            value = _audit_ocr_text(record, ocr_field)
        add_value(key, value)

    add_value_or_ocr("cp", "cp")
    if record.signals.get("cp_consensus_corrected") is True:
        add_value("cp_original_value", record.signals.get("cp_original_value"))
        add_value("cp_consensus_value", record.signals.get("cp_consensus_value"))
    add_value_or_ocr("hp", "hp")
    add_value("hp_bar_anchor_y", record.signals.get("hp_bar_anchor_y"))
    add_value("hp_bar_anchor_score", record.signals.get("hp_bar_anchor_score"))
    for key in (
        "tag_chip_region_anchored",
        "tag_chip_region_left",
        "tag_chip_region_top",
        "tag_chip_region_right",
        "tag_chip_region_bottom",
    ):
        add_value(key, record.signals.get(key))
    if record.signals.get("hp_ocr_fallback_used") is True:
        for key in (
            "hp_ocr_fallback_left",
            "hp_ocr_fallback_top",
            "hp_ocr_fallback_right",
            "hp_ocr_fallback_bottom",
        ):
            add_value(key, record.signals.get(key))
    if record.signals.get("height_ocr_fallback_used") is True:
        for key in (
            "height_ocr_fallback_left",
            "height_ocr_fallback_top",
            "height_ocr_fallback_right",
            "height_ocr_fallback_bottom",
        ):
            add_value(key, record.signals.get(key))
    for key in (
        "moves_visual_region_anchored",
        "moves_tab_anchor_y",
        "moves_tab_anchor_score",
        "moves_fast_row_dark_ratio",
        "moves_charged_rows_dark_ratio",
        "moves_complete_rows_dark_ratio",
        "moves_completion_footer_dark_ratio",
        "moves_completion_footer_height",
        "moves_new_attack_button_green_ratio",
        "moves_new_attack_button_height",
        "moves_new_attack_button_left",
        "moves_new_attack_button_top",
        "moves_new_attack_button_right",
        "moves_new_attack_button_bottom",
    ):
        add_value(key, record.signals.get(key))
    moves_text = _audit_ocr_text(record, "moves")
    if moves_text:
        for key in (
            "moves_ocr_left",
            "moves_ocr_top",
            "moves_ocr_right",
            "moves_ocr_bottom",
        ):
            add_value(key, record.signals.get(key))
    add_value("moves_text", moves_text)
    add_value_or_ocr("story_text", "story")

    for key in (
        "story_sentence_complete",
        *IV_NUMERIC_FIELD_NAMES,
        "appraisal_perfect",
        "iv_star_agreement",
    ):
        add_value(key, record.values.get(key))

    for key, value in record.values.items():
        if key not in emitted:
            add_value(key, value)

    return items


def _audit_values_are_ignored_candidates(record: FrameScanRecord) -> bool:
    return record.classification == NON_EXTRACTABLE_CLASS or bool(
        record.features.get("has_transition")
    )


def _write_audit_html(
    path: Path, records: list[FrameScanRecord], _artifacts_dir: Path
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cards: list[str] = []
    for record in records:
        enabled = [key for key, value in record.features.items() if value]
        feature_html = "\n".join(
            f'    <span class="chip">{html.escape(feature)}</span>'
            for feature in enabled
        )
        values = ", ".join(
            f"{key}={value}" for key, value in _audit_value_items(record)
        )
        if values and _audit_values_are_ignored_candidates(record):
            values = f"ignored candidate values: {values}"
        image_src = _audit_image_src(record.frame_path, path)
        card_lines = [
            '  <article class="card">',
            f'    <img src="{html.escape(image_src)}" loading="lazy" alt="frame">',
            f"    <h2>{html.escape(record.classification)}</h2>",
            f"    <p>{html.escape(record.source_file)} #{record.frame_index} "
            f"@ {record.timestamp_s:.3f}s</p>",
            f"    <p>raw: {html.escape(record.raw_classification)}</p>",
            '    <div class="chips">',
        ]
        if feature_html:
            card_lines.append(feature_html)
        card_lines.extend(
            [
                "    </div>",
                f'    <p class="values">{html.escape(values)}</p>',
            ]
        )
        if record.error:
            card_lines.append(f'    <p class="error">{html.escape(record.error)}</p>')
        card_lines.append("  </article>")
        cards.append("\n".join(card_lines))

    grid_html = "\n".join(
        [
            '<section class="grid">',
            *cards,
            "</section>",
        ]
    )

    path.write_text(
        (
            "<!doctype html>\n"
            '<html lang="en">\n'
            "<head>\n"
            '<meta charset="utf-8">\n'
            "<title>Frame Scan Audit</title>\n"
            "<style>\n"
            "body{font-family:Arial,sans-serif;margin:24px;background:#f5f6f8;"
            "color:#1f2933;}\n"
            ".grid{display:grid;grid-template-columns:repeat(auto-fill,"
            "minmax(220px,1fr));gap:14px;}\n"
            ".card{background:#fff;border:1px solid #d9dee7;border-radius:6px;"
            "padding:10px;}\n"
            ".card img{width:100%;aspect-ratio:9/16;object-fit:contain;"
            "background:#111;border-radius:4px;}\n"
            "h1{font-size:24px;margin:0 0 14px;}h2{font-size:18px;margin:8px 0 4px;}\n"
            "p{font-size:12px;margin:4px 0;color:#44515f;}.chips{display:flex;"
            "flex-wrap:wrap;gap:4px;margin-top:8px;}\n"
            ".chip{font-size:11px;padding:2px 6px;border-radius:999px;"
            "background:#e8eef7;color:#1d4f8f;}\n"
            ".values{min-height:28px;}.error{color:#a43b3b;}\n"
            "</style>\n"
            "</head>\n"
            "<body>\n"
            f"<h1>Frame Scan Audit ({len(records)} frames)</h1>\n"
            f"{grid_html}\n"
            "</body>\n"
            "</html>\n"
        ),
        encoding="utf-8",
    )


def _timing_profile(
    *,
    run_total_s: float,
    phase_totals_s: dict[str, float],
    records: list[FrameScanRecord],
    worker_count: int,
    retry_count: int,
) -> dict[str, object]:
    counts = Counter(record.classification for record in records)
    slowest_rows: list[dict[str, str | int | float]] = [
        {
            "source_file": record.source_file,
            "frame_index": record.frame_index,
            "classification": record.classification,
            "total_s": float(record.timing.get("total_s", 0.0)),
            "ocr_s": float(record.timing.get("ocr_s", 0.0)),
        }
        for record in records
    ]
    slowest = sorted(
        slowest_rows,
        key=lambda item: (
            -float(item["total_s"]),
            str(item["source_file"]),
            int(item["frame_index"]),
        ),
    )[:10]
    return {
        "timing_summary": {
            "run_total_s": round(run_total_s, 6),
            "frame_count": len(records),
            "worker_count": worker_count,
            "retry_count": retry_count,
            "classification_counts": dict(counts),
        },
        "run_phase_totals_s": {
            key: round(value, 6)
            for key, value in sorted(phase_totals_s.items(), key=lambda item: item[0])
        },
        "slowest_frames": slowest,
    }


def _write_scan_artifacts(
    *,
    settings: ScanSettings,
    artifacts_dir: Path,
    report: ScanReport,
    run_total_s: float,
    phase_totals_s: dict[str, float],
    retry_count: int,
    source_payloads: dict[str, object],
) -> None:
    timing_profile = _timing_profile(
        run_total_s=run_total_s,
        phase_totals_s=phase_totals_s,
        records=report.records,
        worker_count=report.worker_count,
        retry_count=retry_count,
    )
    report.timing_summary = timing_profile["timing_summary"]  # type: ignore[assignment]
    fragments = extract_fragments(report.records)
    catalog = load_default_metadata_catalog()
    enrich_fragments_with_species(fragments, catalog)
    enrich_fragments_with_moves(fragments, catalog)
    report.fragments = fragments
    fragment_counts = Counter(fragment.fragment_type for fragment in fragments)
    _write_jsonl(artifacts_dir / "frames.jsonl", report.records)
    write_fragments_jsonl(artifacts_dir / "fragments.jsonl", fragments)
    _write_audit_html(artifacts_dir / "audit.html", report.records, artifacts_dir)
    _write_json(artifacts_dir / "timing_profile.json", timing_profile)
    _write_json(
        artifacts_dir / "scan_manifest.json",
        {
            **build_input_manifest_payload(
                settings,
                processed_files=report.processed_files,
                failed_files=report.failed_files,
            ),
            "warnings": report.warnings,
            "frame_count": len(report.records),
            "classification_counts": dict(
                Counter(record.classification for record in report.records)
            ),
            "fragment_count": len(fragments),
            "fragment_counts": dict(fragment_counts),
            "feature_keys": list(FEATURE_KEYS),
            "detail_feature_keys": list(DETAIL_FEATURE_KEYS),
            "list_feature_keys": list(LIST_FEATURE_KEYS),
            "timing_summary": report.timing_summary,
            "sources": source_payloads,
            "artifacts": {
                "frames_jsonl": str(artifacts_dir / "frames.jsonl"),
                "fragments_jsonl": str(artifacts_dir / "fragments.jsonl"),
                "audit_html": str(artifacts_dir / "audit.html"),
                "timing_profile": str(artifacts_dir / "timing_profile.json"),
                "scan_manifest": str(artifacts_dir / "scan_manifest.json"),
            },
        },
    )


def run_frame_scan(settings: ScanSettings) -> ScanReport:
    started = time.perf_counter()
    phase_timer = PhaseTimer()

    artifacts_dir = _artifact_dir(settings)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    report = ScanReport()
    source_payloads: dict[str, object] = {}
    all_frames: list[FrameCandidate] = []

    assets = phase_timer.run(
        "input_discovery", lambda: discover_inputs(settings.input_path)
    )
    if not TesseractOcrEngine(lang=settings.ocr_lang).is_available():
        report.warnings.append(
            "Tesseract is unavailable; OCR-backed evidence will be blank."
        )

    for asset in assets:
        asset_name = _source_file_name(asset)
        source_frames_dir = artifacts_dir / _source_artifact_stem(asset_name) / "frames"
        try:
            if asset.source_type == "frames_jsonl":

                def load_asset_jsonl(
                    asset: SourceAsset = asset,
                ) -> JsonlFrameLoadResult:
                    return load_jsonl_frame_candidates(asset, artifacts_dir)

                jsonl_load = phase_timer.run(
                    "jsonl_frame_loading",
                    load_asset_jsonl,
                )
                report.warnings.extend(jsonl_load.warnings)
                source_payloads.update(jsonl_load.source_payloads)
                source_payloads[asset.path.name] = jsonl_load.input_payload
                all_frames.extend(jsonl_load.frames)
            elif asset.source_type == "video":

                def extract_asset_frames(
                    asset: SourceAsset = asset,
                    source_frames_dir: Path = source_frames_dir,
                ) -> VideoExtractionResult:
                    return extract_video_frames(asset, source_frames_dir)

                extraction = phase_timer.run(
                    "frame_extraction",
                    extract_asset_frames,
                )
                report.warnings.extend(extraction.warnings)
                source_payloads[asset_name] = build_video_source_payload(
                    asset.source_type, extraction
                )
                all_frames.extend(extraction.frames)
            else:

                def copy_asset_frame(
                    asset: SourceAsset = asset,
                    source_frames_dir: Path = source_frames_dir,
                ) -> list[FrameCandidate]:
                    return copy_image_frame(asset, source_frames_dir)

                frames = phase_timer.run(
                    "image_copy",
                    copy_asset_frame,
                )
                source_payloads[asset_name] = build_image_source_payload(
                    asset.source_type, frames
                )
                all_frames.extend(frames)
            report.processed_files.append(asset.path)
        except Exception as exc:  # noqa: BLE001
            report.failed_files.append(asset.path)
            report.warnings.append(f"{asset.path.name}: {exc}")

    processing = phase_timer.run(
        "frame_analysis", lambda: _process_frames_with_retry(all_frames, settings)
    )
    report.records = processing.records
    report.warnings.extend(processing.warnings)
    report.worker_count = processing.worker_count

    for source_file, payload in source_payloads.items():
        if not isinstance(payload, dict):
            continue
        source_records = [
            record for record in report.records if record.source_file == source_file
        ]
        if source_records:
            payload["classification_counts"] = dict(
                Counter(record.classification for record in source_records)
            )

    phase_timer.run(
        "artifact_writing",
        lambda: _write_scan_artifacts(
            settings=settings,
            artifacts_dir=artifacts_dir,
            report=report,
            run_total_s=time.perf_counter() - started,
            phase_totals_s=phase_timer.totals_s,
            retry_count=processing.retry_count,
            source_payloads=source_payloads,
        ),
    )
    return report
