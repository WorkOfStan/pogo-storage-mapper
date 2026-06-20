# AGENTS.md

Repository instructions for Codex and other coding agents working in this project.

## Purpose

This repository is a Python prototype that exports Pokemon GO inventory data from screenshots and screen recordings into CSV/XLSX. The implementation is intentionally multi-stage and multi-source: detail screens, appraisal screens, and list screens each contribute different fragments of the final record.

## Changes

If CHANGELOG.md is present, describe there what you changed - in English.
If needed also update AGENTS.md - in English.

## Working Agreement

- Prefer substantial restructuring over minimal edits when it clearly improves long-term clarity or maintainability.
- Preserve information that is still genuinely useful. Remove stale, duplicated, or contradictory material instead of layering new text on top of it.
- Never remove comments unless you are resolving the todo they describe or translating the comment to English.
- Keep normal `run` behavior offline-only. Do not introduce network access into the runtime pipeline.
- Do not add sensitive data or private media to the repository.
- Do not manipulate files outside the workspace without explicit approval.

## Validation Standard

Use these as the repository's contract commands:

- `python -m venv .venv`
- `pip install -e .[dev]`
- `ruff format .`
- `ruff check .`
- `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.tmp/pytest`
- `python -m build`

Prefer the interpreter configured in `.vscode/settings.json` when recreating `.venv`.

## Python / Tests

This repository uses its local virtual environment for Python tooling.

On Windows, run tests through the virtual environment interpreter:

- `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp=.tmp/pytest`

Do not run tests with bare `python -m pytest`, because the global Python may not have pytest installed.

If only a targeted test is needed, use:

- `.venv\Scripts\python.exe -m pytest path\to\test_file.py -q -p no:cacheprovider --basetemp=.tmp/pytest`

Expect media-heavy tests to be slow on Windows. Recent local timing:

- `tests\test_scan_frames.py` takes about 6-7 minutes.
- `tests\test_export.py::test_bounded_iaast_export_matches_unlimited_toxtricity_iv`
  can exceed 10 minutes because it exports the same video through both
  unlimited and bounded paths with OCR.
- Combined classifier/scan/export targeted runs can exceed 15 minutes; split
  them by file when debugging failures or use a long timeout.

If pytest temp cleanup fails on Windows, remove only `.tmp/pytest` and rerun tests serially.

`Done` means:

- Tests are green.
- Ruff is clean.
- Documentation is updated when behavior or workflow changes.
- The CLI still works on committed fixtures when the change affects the processing pipeline.
- Output and artifact behavior remains coherent: CSV/XLSX generation, manifest writing, file routing, and error logging still make sense.

## Documentation Authority

Keep one source of truth for each concern:

- `README.md`: human-facing overview, constraints, setup, usage, current status, and concise architecture summary
- `docs/pipeline.md`: technical source of truth for the processing pipeline, frame selection, extraction, consolidation, reconciliation, and export semantics
- `PLAN.md`: current actionable roadmap only
- `DECISIONS.md`: accepted architectural, product, and process decisions only
- `CHANGELOG.md`: historical changelog only

## Documentation Maintenance Rules

Update documentation as part of the same change, not as follow-up cleanup.

- If user-facing behavior changes, update `README.md`.
- If matching, extraction, consolidation, thresholds, or merge behavior changes, update `docs/pipeline.md`.
- If the active roadmap changes, update `PLAN.md`.
- If an architectural or process choice becomes settled, record it in `DECISIONS.md`.
- If repository behavior changes, add an English entry to `CHANGELOG.md`.

When cleaning up docs:

- Remove duplication instead of synchronizing the same explanation across multiple files.
- Distinguish clearly between current behavior and future intent.
- Keep historical context only in `CHANGELOG.md` or in accepted decision records where it is relevant to the decision.

## Code And Test Expectations

- Prefer fixture-based tests for media workflows.
- When changing OCR or reconciliation behavior, keep the Totodile/Fidough/Litwick examples and the list fallback fixture in mind.
- Preserve the separation between visible OCR fields and canonical species metadata.
- Keep the benchmark harness developer-only; production behavior should not depend on benchmark-only code paths.

## Handoff Quality

Before finishing, make sure a future contributor or future Codex session can answer these questions from the docs without reading the whole codebase:

- What each screen type contributes.
- How frames are selected from video.
- How duplicate or partial records are handled.
- How detail and appraisal fragments are matched into one Pokemon record.
- Which document is authoritative for each kind of information.
