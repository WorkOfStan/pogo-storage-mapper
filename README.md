# pogo-storage-mapper

Offline Pokemon GO screenshot and screen-recording scanner.

`pogo-storage-mapper` is currently in an evidence-first restart phase. The active
CLI has an exhaustive `scan-frames` audit path, an optimized `export` path, and
an offline `classify` path for exported inventory rows.
It extracts frames, classifies stable inventory grids as `list`, clearly visible
detail/appraisal screens as `detail`, and unclear menu, swipe, or partial-detail
frames as `non_extractable`. It records feature evidence, extracts traceable per-frame
fragments, and exports source-local Pokemon rows only when the evidence is
unambiguous.

## Project Status

`pogo-storage-mapper` is currently a working proof of concept.

The script can process screenshots and videos recorded from Pokémon GO storage and extract a list of Pokémon together with selected properties, especially IVs and moves. Move extraction requires the user to scroll far enough below the last move so that the script can clearly see that there is no additional move and that the next UI element, such as the **New Attack** button or another section, has been reached.

The recognition works reasonably well, but the extraction is still slow. For example, processing a 30-second video containing around 20 Pokémon currently takes approximately 5–10 minutes per Pokémon when extracting the full set of details. It is also possible to run a lighter extraction mode that extracts only IVs without moves.

The output is generated as both Excel and CSV files.

The resulting Excel file can then be further classified by adding columns such as whether a Pokémon is one of the best attacker candidates, whether it has good IVs for Great League, Ultra League or Little League, and whether it may have a Legacy or Elite TM move that could make it worth keeping.

However, the current output is not yet as clear and practical as it should be for fast decision-making. In addition, Pokémon can have extra value because they are shiny, were caught in specific locations, have costumes, are lucky, shadow, or otherwise special. These factors make the decision of which Pokémon to keep more complicated.

As a proof of concept, the system already works, but it still needs refinement before it can be used comfortably for quick everyday inventory cleanup.

Originally, the plan was to identify Pokémon primarily by name, but this turned out to be unreliable because Pokémon can have arbitrary nicknames and OCR does not recognize names consistently enough. Another idea was to identify Pokémon by CP, but that is also not reliable, because CP is often not clearly readable for shiny, shadow, lucky, or otherwise visually modified Pokémon.

The most reliable identification combination so far appears to be **HP + weight**.

At the moment, recognition is likely tied to the image and video dimensions produced by a Google Pixel 9 screen recording, i.e. PNG screenshots: `864 x 1939 x 24 BPP` and mp4 videos: `1080 x 2424 pixels, kodek: H264 - MPEG-4 AVC (part 10) (avc 1)`. Support for other screen sizes or resolutions may require additional calibration.

## Current Status

- Runtime mode: offline local processing
- Runnable milestones: `scan-frames`, `export`, `validate`, `classify`
- OCR backend: Tesseract, used as evidence only
- Supported inputs: `.mp4`, `.png`, `.jpg`, `.jpeg`
- Supported output: audit artifacts, `pokemon.csv`, `pokemon.xlsx`, and
  classified inventory CSV/XLSX workbooks
- Metadata: packaged local species/move catalog, refreshed only by developer sync

Current pipeline behavior is documented in [docs/pipeline.md](docs/pipeline.md).
Planned work lives in [PLAN.md](PLAN.md).

## Requirements

System tools:

- `ffmpeg`
- `ffprobe`
- Tesseract OCR with English data, optional but recommended for OCR-backed evidence

On Windows, Tesseract is detected from PATH and from:

- `C:\Program Files\Tesseract-OCR\tesseract.exe`
- `C:\Program Files (x86)\Tesseract-OCR\tesseract.exe`

## Setup

Create the virtual environment with the interpreter configured in
[.vscode/settings.json](.vscode/settings.json):

```powershell
& "c:\Pro\Python314\python.exe" -m venv .venv
.venv\Scripts\python.exe -m pip install -e .[dev]
```

Check local tools:

```powershell
.venv\Scripts\python.exe -m pogo_storage_mapper doctor
```

Refresh the packaged metadata catalog when upstream Pokemon GO data changes:

```powershell
.venv\Scripts\python.exe -m pogo_storage_mapper sync-metadata
```

`sync-metadata` is a developer-only network command. Normal `scan-frames`
runtime reads the packaged catalog and does not fetch metadata.

## Scan Frames

Run the scanner on the example video:

```powershell
.venv\Scripts\python.exe -m pogo_storage_mapper scan-frames `
  --input example\screen-20260426-182559-1777220738734_iaast.mp4 `
  --output output\iaast-scan
```

Run the scanner on a folder of screenshots:

```powershell
.venv\Scripts\python.exe -m pogo_storage_mapper scan-frames `
  --input example `
  --output output\example-scan
```

Re-scan the exact frames from an earlier run:

```powershell
.venv\Scripts\python.exe -m pogo_storage_mapper scan-frames `
  --input output\iaast-scan\artifacts\frames.jsonl `
  --output output\iaast-rescan
```

JSONL re-scans preserve each prior `source_file`, `source_type`, `frame_index`,
`timestamp_s`, and referenced original image path, then recompute
classification, features, OCR, signals, and new scan timing. The referenced
images are not copied into the new output folder, so repeated re-scans stay
small. Use this when two assessments should compare the same extracted frames
directly instead of freshly extracting from video again.

Useful options:

- `--workers auto`: use all logical CPU cores for frame analysis
- `--workers 1`: run frame analysis serially
- `--max-frame-attempts 3`: retry failed frame tasks before recording them as failed
- `--artifacts-dir <path>`: write artifacts somewhere other than `<output>\artifacts`
- `--ocr-lang eng`: Tesseract language pack
- `--ocr-mode balanced`: read feature-critical OCR regions; use `full` for every snippet
- `--visible-crop`: write red-rectangle overlays for the visual and OCR crop
  regions actually evaluated during staged analysis; JSONL re-scan overlays are
  written into the new artifact folder, not beside the referenced originals

## Export Pokemon Rows

Run production export on the same supported inputs:

```powershell
.venv\Scripts\python.exe -m pogo_storage_mapper export `
  --input example `
  --output output\example-export
```

`export` first runs visual-only frame analysis in parallel to classify frames as
`list`, `detail`, `appraisal`, or `non_extractable` and split stable source-local detail screens into
logical `raw=detail` and `raw=appraisal` runs. Field gates such as `has_hp`,
`has_moves`, and `has_iv` are computed later by the sequence workers, which scan
each run backward from its latest frame and OCR only the fields that the current
frame's gates make useful. Clean evidence is collected generically from every
processed frame; the screen type controls only which fields are useful for
stopping that run, not which fields may be exported. It keeps `scan-frames`
separate as the exhaustive audit path. After the normal production scan,
`export` automatically reruns a bounded repair scan for near-miss
detail/appraisal runs where a likely anchor was incomplete or support evidence
conflicted with it; repair uses the same sequence eligibility, runs repairable
sequences in parallel, and scans frames sequentially within each repaired
sequence. After repair, adjacent same-source same-HP production runs can share a
clearly dominant weight to correct isolated sequence-level OCR outliers before
row fragments are built. There is no separate repair flag.

Useful export options mirror `scan-frames` where relevant: `--workers` controls
visual frame analysis, production sequence scanning, and production repair;
`--max-frame-attempts` retries failed frame, sequence, and repair tasks;
`--max-export-frame-files N` bounds temporary MP4 export frame images when
`N > 0`; and `--artifacts-dir`, `--ocr-lang`, `--ocr-mode`, and
`--visible-crop` control artifact and OCR diagnostics.

`--max-export-frame-files` is export-only and defaults to `0`, which keeps the
current unlimited extraction behavior. Positive values process MP4 exports in
newest-to-oldest temporary frame windows while using the same visual screening,
sequence scanning, repair, stabilization, and row assembly path as unlimited
export. The limit applies only to temporary frame images under the export
artifact frame directory, not to final `pokemon.csv`, `pokemon.xlsx`,
`export.log`, manifest, timing, performance, or warning artifacts. The limit is
soft: completed sequences are released after structured data is extracted, but
an open detail/appraisal sequence at a chunk boundary may temporarily keep extra
frames so the exporter can confirm sequence pairing and preserve provenance. In
the current long-run observations, temporary peaks can retain a noticeable
fraction of total frames, roughly 10-20%, depending on video structure, sequence
grouping, and unresolved detail/appraisal frames. Leave that disk headroom when
choosing the cap. On a 16-logical-core i5-13450HX, start around
`--max-export-frame-files 800` to `1000` when disk space allows; lowering the cap
mainly saves disk, while raising it may reduce extraction chunk churn but does
not reduce OCR quality or directly increase the configured worker count.
For bounded MP4 input, extraction remains frame-index based, but the exporter
may build an internal FFprobe frame timestamp map and use timestamp-guided
FFmpeg seeks for each chunk. If the timeline is incomplete or a seeked chunk
does not validate to the requested frame names/count, that chunk falls back to
the exact global frame-select extraction path. `export.log` and
`artifacts/performance_summary.json` report the timeline status, per-chunk
extraction method, seek window, and fallback reason.
`scan-frames` behavior is unchanged.

`--workers auto` resolves to `os.cpu_count()` logical CPUs capped by the queued
work in each visual, sequence, or repair batch. Workers are created per batch
and reused within that batch. If a batch has fewer ready sequences than logical
cores, or if one long sequence is being scanned frame-by-frame, CPU use can be
below the configured worker count. Temporary task failures are retried according
to `--max-frame-attempts`; retry batches may use reduced concurrency and are
reported in `export.log` and `artifacts/performance_summary.json`.

Each exported row is built from a source-local evidence bucket keyed primarily
by `hp + weight`. A raw-appraisal anchor requires `hp + weight`, resolved
species, catch date/location, and a complete IV triplet. If IV/star agreement is
missing, the row may still be anchored with `iv_complete=false` and a warning. A
raw-detail anchor requires `hp + weight` plus resolved moves. Frames with only
`hp + weight` are support evidence: they may contribute clean fields, but they
do not create a row by themselves and cannot veto an accepted detail/appraisal
anchor. CP is optional; unresolved CP conflicts are left blank instead of
exporting a dubious value. Detail physical-stat height wins over fallback
height when both are present. Scrolled move frames use HP-anchor-aware
physical-stat recovery so visible `hp + weight` evidence can still drive normal
identity pairing.

## Classify Exported Inventory

Validate an exported CSV/XLSX before classification:

```powershell
.venv\Scripts\python.exe -m pogo_storage_mapper validate `
  --input output\example-export\pokemon.xlsx
```

Classify duplicate candidates into `KEEP`, `REVIEW`, and `LET-GO`:

```powershell
.venv\Scripts\python.exe -m pogo_storage_mapper classify `
  --input output\example-export\pokemon.xlsx `
  --output output\example-classified
```

`classify` writes `<input-stem>_classified.csv`,
`<input-stem>_classified.xlsx`, and
`artifacts\classify_manifest.json` inside the output directory. The workbook
contains an `All` sheet plus filtered recommendation, PvP, attacker, legacy, and
summary sheets.

This classifier intentionally does not use current meta rankings. `attacker_top3`
means the Pokemon is one of the top three owned IV candidates for the selected
final evolution family by the local score `10 * attack IV + defense IV +
stamina IV`; it does not mean the species is globally useful. PvP flags mean the
best owned IV candidate in that evolution family for Little, Great, or Ultra
League by stat product; they do not mean the species is meta-relevant. `LET-GO`
is conservative but still intended for manual review before transferring.

Runtime classification is offline. The developer-only `sync-metadata` command
refreshes the packaged catalog data used for species, moves, base stats,
evolutions, and CP multipliers. Possible legacy/Elite move warnings come from a
curated local CSV and are intentionally separate from the Game Master metadata.

## Scan Artifacts

Each `scan-frames` run writes:

- `artifacts/<source-stem>/frames/`: extracted video frames, copied image
  inputs, or generated visible-crop overlays for JSONL re-scan input
- `artifacts/frames.jsonl`: one JSON record per evaluated frame
- `artifacts/fragments.jsonl`: traceable structured values extracted from gated
  `list`, stable `detail`, and stable `appraisal` records; complete story
  species names are resolved through the packaged metadata catalog when
  possible, and move OCR is normalized into move slots when the local catalog
  has unambiguous matches
- `artifacts/audit.html`: browser contact sheet for manual review, with CP, HP,
  weight, height, CP consensus diagnostics, HP anchor diagnostics, move OCR crop
  diagnostics, moves, story, and IV candidate values shown when available;
  values on `non_extractable` transition frames are labeled as ignored
  candidates
- `artifacts/timing_profile.json`: scan timing summary and slowest frames
- `artifacts/scan_manifest.json`: settings, source summaries, warnings, and artifact paths

Every frame record contains a consistent set of boolean feature keys, diagnostic
signals, OCR snippets, and candidate values. Detail/appraisal gates such as
`has_CP`, `has_iv`, `has_iv_complete`, and `has_story` are separate from
list-layout evidence such as `has_list_grid`, `has_list_cp`,
`has_list_display_name`, and `has_list_pokemon_art`. Frames where a detail
screen is obscured, horizontally moving, or only partially visible are kept as
`non_extractable` so extraction does not consume unstable evidence. Fragment rows keep
their source frame and field evidence so later matching can merge them into
unique Pokemon records. See [docs/pipeline.md](docs/pipeline.md) for the
maintainer-level feature semantics.

When story OCR yields a complete catch sentence, the fragment keeps the visible
`canonical_name_text` and adds `species_key`, `species_name`, and `pokedex_id`
when the name resolves unambiguously through the local metadata catalog. If no
story name is present, exact display-name OCR can provide the same species
metadata. A one-edit fuzzy species fallback repairs unique names with at least
five characters; unresolved names remain as OCR text and do not fail the scan.

`--visible-crop` is a manual diagnostic aid. It writes frame copies with red
rectangles around the visual/OCR regions being inspected; for JSONL re-scans,
those generated overlays live in the new artifact folder while `audit.html`
continues to show the referenced original frames. OCR text and confidence still
live in `frames.jsonl`, and the overlay images are not an OCR cache.

Within a stable source-local detail run, parsed minority CP outliers can be
corrected to a clearly dominant CP value for the same visible HP, while missing
CP remains missing and raw CP OCR text stays in the artifacts. Parsed CP values
must be in the Pokemon GO range `10..9366`, so OCR-joined over-max candidates
are rejected. A dominant weight seen in the same source-local same-HP detail run
can fill later detail/appraisal frames where the physical-stats row is covered
or correct an isolated weight outlier on a move frame; propagated rows carry a
`sequence_weight_propagated` signal, and corrected rows carry
`sequence_weight_corrected`; production export can also correct isolated
outlier weights across adjacent same-source same-HP sequence results before
fragment matching. Move OCR is detail-only and is accepted when the
HP-bar-anchored move section confirms the active `GYMS & RAIDS` tab and the
visible attack block is complete, including one-charged-attack layouts where the
section clearly ends after the first charged attack. A clipped lower strip of
the `NEW ATTACK` button is not enough evidence on its own. The crop starts below
the tab underline to keep power-up and resource text out of `moves_text`, and is
normalized into fast, charged, second charged, and specific Max Move fields when
the packaged move catalog resolves the visible names unambiguously.
Unresolved move text remains as raw `moves_text`, and generic `Max Moves` header
text is not treated as a specific Max Move. Appraisal IV bars are decoded as
three five-point segments, with an adaptive vertical layout for appraisal cards
that appear higher or lower inside the fixed IV-panel crop.

Visible height is read from the right physical-stat column on stable detail
frames when the row is unobscured. It remains audit/export evidence only and is
not propagated across later IV-covered frames. Production export can recover
height from the HP-bar-anchored upper physical-stat row on scrolled move frames,
and may opportunistically probe earlier stable detail frames for this visible
value. Height remains optional and is not used for identity matching.

If Tesseract is unavailable, or if a region is skipped by `--ocr-mode balanced`,
the scan still completes and writes visual/audit artifacts. OCR-backed snippets and
feature evidence remain blank or false for those regions.

## Export Artifacts

Each `export` run writes:

- `pokemon.csv`: final source-local Pokemon rows
- `pokemon.xlsx`: the same rows in an Excel workbook
- `export.log`: live tab-separated progress log with timestamp, worker ID,
  processing phase, and frame name for `tail -f` monitoring
- `artifacts/export_manifest.json`: settings, source summaries, counts,
  warnings count, and artifact paths
- `artifacts/timing_profile.json`: production timing summary
- `artifacts/performance_summary.json`: aggregated phase, worker, bounded frame
  retention/extraction, probe, cleanup, and artifact-writing diagnostics
- `artifacts/warnings.jsonl`: rejected sequence, conflict, input, and
  environment warnings
- `artifacts/frame_lifecycle.jsonl`: bounded export frame processing, deletion,
  retention, and skip decisions with reasons
- `artifacts/row_diagnostics.jsonl`: accepted, merged-support, and rejected row
  candidates with present fields and skip reasons

Export columns include source span, visible and canonical species fields, CP, HP,
weight, visible height when production OCR can collect it, IV values, catch
story fields, resolved move slots, specific Max Move, and power-state/tag flags.
List fragments
remain available in `scan-frames` artifacts for audit and future fallback work,
but they do not participate in production export matching.
If bounded export sees Pokemon-like detail/appraisal evidence that cannot form
a normal row, the row diagnostics artifact records the partial fields and skip
reason; complementary unresolved evidence with a unique `hp + weight` identity
and canonical corroboration can be exported as a partial row instead of being
silently dropped. When exact `hp + weight` matching fails only because detail
and appraisal weights disagree or one side lacks weight, export can recover a
single unambiguous same-source, same-HP appraisal/detail pair and logs the
physical-key mismatch; ambiguous same-HP candidates remain diagnostic-only.

## Contributor Document Map

- `README.md`: start here for project overview and usage
- `docs/pipeline.md`: current implemented pipeline behavior for maintainers
- `PLAN.md`: current actionable roadmap only
- `DECISIONS.md`: accepted architectural, product, and process decisions
- `AGENTS.md`: repository instructions for coding agents
- `CHANGELOG.md`: historical changes
