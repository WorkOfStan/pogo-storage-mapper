from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TypeVar

ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")
WarningT = TypeVar("WarningT")


class ScanSettingsLike(Protocol):
    input_path: Path
    output_dir: Path
    artifacts_dir: Path | None
    ocr_lang: str
    ocr_mode: str
    workers: int | None
    max_frame_attempts: int


class VideoExtractionLike(Protocol):
    @property
    def frames(self) -> Sequence[object]: ...

    @property
    def warnings(self) -> Sequence[str]: ...

    @property
    def used_hwaccel(self) -> str: ...


@dataclass(slots=True)
class AdaptiveRetrySummary:
    worker_count: int
    retry_count: int


class PhaseTimer:
    def __init__(self) -> None:
        self.totals_s: dict[str, float] = {}

    def run(self, name: str, callback: Callable[[], ResultT]) -> ResultT:
        started = time.perf_counter()
        result = callback()
        self.totals_s[name] = self.totals_s.get(name, 0.0) + (
            time.perf_counter() - started
        )
        return result


def resolve_worker_count(requested_workers: int | None, item_count: int) -> int:
    if item_count <= 1:
        return 1
    if requested_workers is not None:
        return max(1, min(item_count, requested_workers))
    return max(1, min(item_count, os.cpu_count() or 1))


def build_input_manifest_payload(
    settings: ScanSettingsLike,
    *,
    processed_files: list[Path],
    failed_files: list[Path],
) -> dict[str, object]:
    return {
        "settings": {
            "input_path": str(settings.input_path),
            "output_dir": str(settings.output_dir),
            "artifacts_dir": (
                str(settings.artifacts_dir) if settings.artifacts_dir else None
            ),
            "ocr_lang": settings.ocr_lang,
            "ocr_mode": settings.ocr_mode,
            "workers": "auto" if settings.workers is None else settings.workers,
            "max_frame_attempts": settings.max_frame_attempts,
        },
        "processed_files": [str(path) for path in processed_files],
        "failed_files": [str(path) for path in failed_files],
    }


def build_source_payload(
    source_type: str,
    *,
    frame_count: int,
    warnings: list[str],
    used_hwaccel: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "source_type": source_type,
        "frame_count": frame_count,
        "warnings": warnings,
    }
    if used_hwaccel is not None:
        payload["video_extraction"] = {
            "requested_hwaccel": "auto",
            "used_hwaccel": used_hwaccel,
        }
    return payload


def build_video_source_payload(
    source_type: str, extraction: VideoExtractionLike
) -> dict[str, object]:
    return build_source_payload(
        source_type,
        frame_count=len(extraction.frames),
        warnings=list(extraction.warnings),
        used_hwaccel=extraction.used_hwaccel,
    )


def build_image_source_payload(
    source_type: str, frames: Sequence[object]
) -> dict[str, object]:
    return build_source_payload(source_type, frame_count=len(frames), warnings=[])


def execute_with_adaptive_retries(
    items: list[ItemT],
    *,
    requested_workers: int | None,
    max_attempts: int,
    process_item: Callable[[ItemT], ResultT],
    on_success: Callable[[ItemT, ResultT, int], None],
    on_final_failure: Callable[[ItemT, Exception, int], None],
    warnings: list[WarningT],
    build_retry_warning: Callable[[int, int], WarningT],
    executor_factory: Callable[..., Any] = ThreadPoolExecutor,
    completed_iterator: Callable[
        [dict[Future[ResultT], tuple[ItemT, int]]],
        Iterable[Future[ResultT]],
    ] = as_completed,
    on_diagnostic: Callable[[dict[str, object]], None] | None = None,
) -> AdaptiveRetrySummary:
    resolved_workers = resolve_worker_count(requested_workers, len(items))
    current_workers = resolved_workers
    pending: list[tuple[ItemT, int]] = [(item, 1) for item in items]
    retry_count = 0

    def emit(event: str, **payload: object) -> None:
        if on_diagnostic is None:
            return
        on_diagnostic(
            {
                "event": event,
                "requested_workers": (
                    "auto" if requested_workers is None else requested_workers
                ),
                "resolved_worker_count": resolved_workers,
                **payload,
            }
        )

    while pending:
        batch_attempt = min(attempt for _item, attempt in pending)
        batch_started = time.perf_counter()
        if current_workers <= 1:
            emit(
                "batch_start",
                active_worker_count=1,
                queued_item_count=len(pending),
                attempt=batch_attempt,
            )
            next_pending: list[tuple[ItemT, int]] = []
            for item, attempt in pending:
                try:
                    on_success(item, process_item(item), attempt)
                except Exception as exc:  # noqa: BLE001
                    retry_count += _handle_retry_failure(
                        item=item,
                        attempt=attempt,
                        exc=exc,
                        max_attempts=max_attempts,
                        next_pending=next_pending,
                        on_final_failure=on_final_failure,
                    )
            emit(
                "batch_complete",
                active_worker_count=1,
                queued_item_count=len(pending),
                failed_item_count=len(next_pending),
                attempt=batch_attempt,
                duration_s=round(time.perf_counter() - batch_started, 6),
            )
            pending = next_pending
            continue

        next_pending = []
        emit(
            "batch_start",
            active_worker_count=current_workers,
            queued_item_count=len(pending),
            attempt=batch_attempt,
        )
        with executor_factory(max_workers=current_workers) as executor:
            future_to_item = {
                executor.submit(process_item, item): (item, attempt)
                for item, attempt in pending
            }
            for future in completed_iterator(future_to_item):
                item, attempt = future_to_item[future]
                try:
                    on_success(item, future.result(), attempt)
                except Exception as exc:  # noqa: BLE001
                    retry_count += _handle_retry_failure(
                        item=item,
                        attempt=attempt,
                        exc=exc,
                        max_attempts=max_attempts,
                        next_pending=next_pending,
                        on_final_failure=on_final_failure,
                    )
        emit(
            "batch_complete",
            active_worker_count=current_workers,
            queued_item_count=len(pending),
            failed_item_count=len(next_pending),
            attempt=batch_attempt,
            duration_s=round(time.perf_counter() - batch_started, 6),
        )
        if next_pending:
            warnings.append(build_retry_warning(len(next_pending), current_workers))
            next_worker_count = max(1, current_workers // 2)
            emit(
                "retry_reduced",
                active_worker_count=current_workers,
                next_worker_count=next_worker_count,
                queued_item_count=len(next_pending),
                attempt=batch_attempt + 1,
            )
            current_workers = next_worker_count
        pending = next_pending

    return AdaptiveRetrySummary(
        worker_count=resolved_workers,
        retry_count=retry_count,
    )


def _handle_retry_failure(
    *,
    item: ItemT,
    attempt: int,
    exc: Exception,
    max_attempts: int,
    next_pending: list[tuple[ItemT, int]],
    on_final_failure: Callable[[ItemT, Exception, int], None],
) -> int:
    if attempt < max_attempts:
        next_pending.append((item, attempt + 1))
        return 1
    on_final_failure(item, exc, attempt)
    return 0
