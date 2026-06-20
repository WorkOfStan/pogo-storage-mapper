from __future__ import annotations

from collections import Counter
from typing import Literal

from pogo_storage_mapper import runtime_support


def _completed_futures(futures):
    return list(futures)


class _ImmediateFuture:
    def __init__(self, fn, *args) -> None:
        try:
            self.value = fn(*args)
            self.error = None
        except Exception as exc:  # noqa: BLE001
            self.value = None
            self.error = exc

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value


class _InlineSubmitExecutor:
    def __init__(self, *, max_workers: int) -> None:
        self.max_workers = max_workers

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> Literal[False]:
        del exc_type, exc, tb
        return False

    def submit(self, fn, *args):
        return _ImmediateFuture(fn, *args)


def test_adaptive_retry_diagnostics_keep_auto_resolution_per_call(
    monkeypatch,
) -> None:
    monkeypatch.setattr(runtime_support.os, "cpu_count", lambda: 8)
    attempts: Counter[int] = Counter()
    successes: list[int] = []
    warnings: list[str] = []
    events: list[dict[str, object]] = []

    def process_item(item: int) -> int:
        attempts[item] += 1
        if item == 0 and attempts[item] == 1:
            msg = "temporary worker failure"
            raise RuntimeError(msg)
        return item

    summary = runtime_support.execute_with_adaptive_retries(
        [0, 1, 2],
        requested_workers=None,
        max_attempts=2,
        process_item=process_item,
        on_success=lambda item, result, attempt: successes.append(result),
        on_final_failure=lambda item, exc, attempt: None,
        warnings=warnings,
        build_retry_warning=lambda pending_count, worker_count: (
            f"{pending_count} failed with {worker_count} workers"
        ),
        executor_factory=_InlineSubmitExecutor,
        completed_iterator=_completed_futures,
        on_diagnostic=events.append,
    )

    assert summary.worker_count == 3
    assert summary.retry_count == 1
    assert sorted(successes) == [0, 1, 2]
    assert warnings == ["1 failed with 3 workers"]
    assert events[0]["event"] == "batch_start"
    assert events[0]["requested_workers"] == "auto"
    assert events[0]["resolved_worker_count"] == 3
    assert events[0]["active_worker_count"] == 3
    assert events[0]["queued_item_count"] == 3
    assert any(
        event["event"] == "retry_reduced" and event["next_worker_count"] == 1
        for event in events
    )
    assert events[-2]["event"] == "batch_start"
    assert events[-2]["active_worker_count"] == 1

    second_events: list[dict[str, object]] = []
    second_summary = runtime_support.execute_with_adaptive_retries(
        [10, 11, 12],
        requested_workers=None,
        max_attempts=2,
        process_item=lambda item: item,
        on_success=lambda item, result, attempt: None,
        on_final_failure=lambda item, exc, attempt: None,
        warnings=[],
        build_retry_warning=lambda pending_count, worker_count: "",
        executor_factory=_InlineSubmitExecutor,
        completed_iterator=_completed_futures,
        on_diagnostic=second_events.append,
    )

    assert second_summary.worker_count == 3
    assert second_events[0]["active_worker_count"] == 3
