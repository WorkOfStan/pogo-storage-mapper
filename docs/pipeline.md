# Pipeline

This document is the maintainer source of truth for the current implemented
scanner pipeline. The user-facing overview lives in [../README.md](../README.md);
planned work lives in [../PLAN.md](../PLAN.md).

## Runtime Model

- `scan-frames` is the exhaustive audit path.
- `export` is the optimized production path for CSV/XLSX Pokemon rows.
- `classify` is a post-export inventory classifier for CSV/XLSX rows; it does
  not run frame scanning or OCR.
- The scanner is offline-only during normal runtime.
- Inputs can be MP4 videos, image files, image folders, or prior
  `artifacts/frames.jsonl` files.
- Video frame extraction tries NVIDIA/CUDA FFmpeg decode first and falls back to
  CPU extraction after clearing partial output.
- `scan-frames` and unlimited `export` still materialize all video frames for
  behavior compatibility. `export --max-export-frame-files N` with `N > 0`
  bounds MP4 temporary frame images by processing newest-to-oldest windows;
  `N <= 0` keeps unlimited export behavior.
- Bounded MP4 export may build a one-run FFprobe frame timestamp map and use it
  to seek FFmpeg near each requested frame-index window. Chunks that do not
  validate fall back to exact global frame selection.
- The export frame-image limit is soft and applies only to temporary frame
  images under the export artifact frame directory, not to final CSV/XLSX/log,
  manifest, timing, or warning artifacts. A single open detail/appraisal run
  may temporarily exceed the limit so row assembly remains correct.
- Frame analysis uses all logical CPU cores by default. Failed frame tasks are
  retried with reduced concurrency and finally retried serially before they are
  recorded as failed frames.
- `scan-frames` reads packaged metadata from
  `pogo_storage_mapper/data/metadata_catalog.json`; it does not fetch metadata.
- OCR is evidence only. OCR snippets and candidate values are written for audit,
  traceable fragment extraction, and conservative production row assembly.
- `sync-metadata` is the developer-only network command for refreshing the
  packaged species, move, and classifier metadata from upstream Game Master data.

## Scan Artifacts

Each run writes:

- `artifacts/<source-stem>/frames/`: extracted video frames, copied image
  inputs, or generated visible-crop overlays for JSONL re-scan input
- `artifacts/frames.jsonl`: one JSON record per evaluated frame
- `artifacts/fragments.jsonl`: one traceable structured fragment per useful
  frame with extracted field values, field-level evidence, and resolved species
  metadata or move metadata when OCR names match the local catalog
- `artifacts/audit.html`: browser contact sheet for manual review, with CP, HP,
  moves, story, and IV candidate values shown when available
- `artifacts/timing_profile.json`: timing summary and slowest frames
- `artifacts/scan_manifest.json`: settings, source summaries, warnings, and
  artifact paths

Passing a prior `artifacts/frames.jsonl` to `scan-frames --input` re-scans the
referenced frame images while preserving `source_file`, `source_type`,
`frame_index`, `timestamp_s`, and referenced original image paths. Referenced
JSONL images are not copied into the new output folder; `frames.jsonl` and
`audit.html` point back to the original images used for analysis.

## Export Artifacts

Each `export` run writes final outputs plus minimal production artifacts:

- `<output>/pokemon.csv`
- `<output>/pokemon.xlsx`
- `<output>/export.log`
- `artifacts/export_manifest.json`
- `artifacts/timing_profile.json`
- `artifacts/performance_summary.json`
- `artifacts/warnings.jsonl`
- `artifacts/frame_lifecycle.jsonl`
- `artifacts/row_diagnostics.jsonl`

The export manifest records settings, source summaries, processed/failed files,
frame counts, visual/scanned frame counts, sequence counts, exported row counts,
rejected sequence counts, warning counts, timing summary, artifact paths, and
bounded frame-materialization counters when the export limit is configured:
`max_export_frame_files`, `bounded_extraction_enabled`,
`peak_export_frame_files`, `deleted_list_or_non_extractable_frames`,
`deleted_sequence_frames`, `deleted_unsequenced_visual_frames`,
`retained_frame_count`, `frame_lifecycle_summary`,
`unresolved_pokemon_like_sequence_count`, and
`bounded_extraction_soft_limit_exceeded`.
The live `export.log` is reset at the start of each run and writes one
tab-separated line per frame as it starts visual screening or production
sequence OCR, including timestamp, worker ID, phase, and frame name. It also
logs worker batch diagnostics for configured/resolved workers, queued work,
active worker count, retry reductions, and bounded chunk extraction/accounting.
`performance_summary.json` aggregates phase totals, slowest operation groups,
configured and resolved worker batches, bounded chunk frame-file and extraction
method counts, lifecycle cleanup totals, probe field groups, and
artifact-writing timings.
`frame_lifecycle.jsonl` records bounded export frame cleanup decisions with a
per-frame action (`processed`, `skipped`, `deleted`, or `retained`) and reason.
`row_diagnostics.jsonl` records accepted, merged-support, and rejected row
candidates with source spans, detail/appraisal source frame indices, present and
missing fields, move-resolution status, and row assembly skip reasons so
unresolved Pokemon-like sequences can be audited without inspecting frames by
hand.

## Frame Classes

- `list`: stable Pokemon inventory grid or list screens.
- `detail`: stable Pokemon detail screens.
- `appraisal`: stable appraisal overlays.
- `non_extractable`: menus, horizontal swipes, unclear transitions, partial-detail frames,
  and any frame that should not feed later extraction.

After per-frame analysis, source-local sequence checks mark unstable horizontally
moving detail/appraisal frames as `non_extractable` while preserving stable detail runs.
`has_transition` is rejection evidence: the frame should be skipped because it is
probably between stable Pokemon detail/appraisal states.
Horizontal detail-card transitions are geometry-first: the scanner probes the
HP-bar band, treats white card pixels and green HP-bar pixels as continuous card
content, ignores left/right edge gaps, and records internal non-card gaps as
diagnostics. A gap becomes transition rejection evidence only when broader
horizontal-card split context is also visible, which avoids demoting stable
detail frames with normal HP-area glyphs while still catching repeated swipe
frames even when neighboring frame deltas are zero.

## Feature Gates

Every frame record contains the same boolean feature keys. Count-like and
diagnostic evidence is stored in `signals`; candidate values are stored in
`values`.

OCR snippets and `values` are candidate evidence. Frame analysis may read and
parse them before the final feature gates are settled, but downstream extracted
or audit-facing values should be interpreted only through the matching `has_*`
gate. The audit contact sheet labels values on `non_extractable` or
`has_transition` frames as ignored candidates.

Detail/appraisal gates are screen-type specific. Shared identity evidence can
appear on both stable `detail` and `appraisal` frames, but move, height, and
lower power/context OCR belong to detail frames, while catch story and IV
evidence belong to appraisal frames.

- `has_CP`: the centered CP label is visible in the form letters `CP` followed
  by an integer in the Pokemon GO range `10..9366`, with narrow visual
  fallbacks for stable detail/appraisal layouts when OCR drops CP. CP OCR uses
  tight fallback crops when broad OCR returns a bare or missing value, rejects
  over-max or OCR-joined suffix candidates, and suppresses bare-number fallback
  when a noisy CP-like prefix is present. Source-local postprocessing can
  correct parsed minority CP outliers to a clearly dominant CP value within the
  same stable contiguous HP run; it does not fill missing CP, and the raw CP OCR
  text remains unchanged for audit.
- `has_display_name`: the centered display name is visible above the HP bar,
  with the edit pen icon on its right side. This is weak identity evidence
  because visible display-name OCR can be noisy.
- `has_hp`: centered HP text is visible below the HP bar and validates as
  `numerator/denominator HP`, with HP-specific visual bar/text evidence as a
  fallback. HP OCR uses the HP bar as a layout anchor when the fixed text region
  misses visible HP text. Anchor diagnostics and the selected fallback crop are
  written for audit when fallback OCR recovers HP.
- `has_weight`: weight OCR validates as a number with up to two decimal places,
  `0 < weight <= 1000`, followed by letters `kg`; the `WEIGHT` label is below
  the number. Production export can also recover weight from detail lower-panel
  context OCR when the dedicated weight crop is blank, and from an upper
  physical-stats fallback crop when the detail page is scrolled far enough for
  moves to be visible. On scrolled move layouts, the upper physical-stats fallback is
  preferred when it parses, but a clean lower fixed weight crop is retained when
  the upper fallback is blank. Symbol-prefixed `kg` snippets such as OCR-misread
  leading digits are ignored. Source-local postprocessing can also propagate a
  dominant same-HP weight across stable detail/appraisal frames when later
  frames no longer expose the physical-stats row, and can correct an isolated
  non-dominant same-HP weight outlier when the dominant value is clear. Those
  records carry `sequence_weight_propagated` or `sequence_weight_corrected`
  diagnostics. Production export also has a later adjacent-sequence
  stabilization pass that can correct isolated weight outliers across
  same-source same-HP production sequence results before fragment matching.
- `has_moves`: the complete visible detail move section is present and tied to
  the current Pokemon by layout: an HP-bar anchor is visible, the active battle
  tab underline is found below that HP bar, attack rows are visible, and either
  the second charged/`NEW ATTACK` area or a clear end to the shown attack block
  is visible. This supports Pokemon that visibly expose only one charged attack
  without requiring species-specific move-pool metadata.
  The final end-of-section gate rejects frames that show only a clipped lower
  strip of the `NEW ATTACK` button, because another charged move may still be
  hidden below the display edge.
  During early classification, a provisional move vote can use OCR text or
  rough move-region visual density to help identify ambiguous detail frames. On
  saved stable `detail` records, final `has_moves` is overwritten from visual
  complete-move-section evidence. Appraisal frames never read or export moves.
  Normal move OCR is accepted only when detail lower-panel context OCR confirms
  the active `GYMS & RAIDS` tab text; the inactive
  grey `TRAINER BATTLES` tab is useful supporting evidence but is not required.
  The crop starts below the detected active tab underline so power-up, candy,
  and stardust text above the tabs does not pollute move evidence. Treat
  `moves_text` as extracted attack data only when final `has_moves` is true.
  Transition frames may keep visual `has_moves` evidence for audit review, but
  `non_extractable` or `has_transition` frames still do not produce fragments.
- `is_shadow`: literal `SHADOW BONUS` text is visible below the moves, or
  `Frustration` is visible as a move. Other move names containing `Shadow`,
  such as `Shadow Ball`, do not imply shadow status by themselves.
- `has_dynamax`: Dynamax or Max Move OCR is visible, or source-sequence
  postprocessing fills a stable Dynamax power/move section when nearby frames in
  the same detail run have direct Dynamax evidence. Gigantamax evidence
  suppresses this more generic flag within the same source-local run.
- `has_gigantamax`: Gigantamax or G-Max OCR is visible, or source-sequence
  postprocessing fills stable power/move frames in the same detail run when
  nearby frames have direct Gigantamax evidence.
- `has_iv`: a sufficiently opaque appraisal IV card, star/seal evidence, and at
  least one decoded IV bar are present. Each IV bar is decoded as three visual
  segments worth up to five points each; amber or red fill is counted from the
  left within each segment, grey tails remain unfilled, and fully red/pink
  segmented bars decode as `15`. The scanner keeps a fixed IV-panel crop but
  selects between high and lower vertical bar layouts from the visible bar-track
  signal inside that crop.
  `appraisal_star_count` counts the active appraisal tier: active amber stars
  from the left produce `0` through `3`, grey inactive stars are ignored, and a
  red perfect badge is represented as `4`. `appraisal_star_count` is `null`
  when no usable appraisal badge/seal and IV panel are visible together; use
  `appraisal_badge_visible` to distinguish no badge from a visible zero-star
  badge.
- `has_iv_complete`: source-sequence postprocessing marks the latest stable
  non-transition appraisal frame in each source-local run whose three decoded IV
  bars agree with the visible star tier. Direct `scan_frame_candidate(...)` does
  not finalize this flag; normal CLI scans do.
- `has_story`: appraisal story OCR contains a complete parse-ready catch sentence in the
  form `This canonical_name was caught on catch_date around location.` The catch
  sentence may follow leading appraisal flavor text, and location can be
  country-only, region-only, or multi-part. Detail frames never read story OCR.
- `has_tag_chips`: visual tag-chip evidence is present. When an HP-bar anchor
  is detected, the tag-chip crop is derived from that anchor; the fixed tag crop
  is used only when no HP anchor is available.
- `has_height`: height OCR validates as meters from the visible right physical
  stat column. The detail crop is aligned to the weight row and uses block OCR
  so the number and `HEIGHT` label can be read together; HP-bar-anchored fallback
  OCR recovers the upper physical-stat row on scrolled move layouts. Appraisal
  frames never read height because the value is obscured. Production export
  opportunistically probes earlier stable detail frames for height, but height
  remains optional audit/export evidence and is not an identity signal.
- `has_pokemon_art`: the Pokemon art region has enough edge/color evidence to be
  useful.
- `has_transition`: rejection evidence for frames classified as `non_extractable`.

Reserved feature keys currently remain false unless a detector is added:
`has_gender`, `has_shiny`, `has_lucky`, `has_purified`, `has_favorite`,
`has_costume_or_form_visual`, `has_mega_or_primal_section`, and
`has_scroll_position`.

List-layout gates describe list evidence only:

- `has_list_grid`: stable Pokemon inventory grid/list evidence is present.
- `has_list_cp`: list-layout CP text evidence is present.
- `has_list_display_name`: list-layout display-name text evidence is present.
- `has_list_pokemon_art`: list-layout Pokemon art evidence is present.

Frames classified as `non_extractable` do not keep detail or list extraction
gates, except for rejection evidence such as `has_transition`.

## Detail Layout

The Pokemon detail page can move vertically. CP, Pokemon art, display name, HP
bar/text, tag chips, Dynamax/Gigantamax sections, and moves should not be
treated as absolute vertical positions on every frame. The scanner records HP
bar anchor diagnostics in `signals` and uses the HP bar to derive tighter CP/HP
OCR recovery regions when fixed OCR boxes miss visible values. Tag-chip visual
evidence is HP-anchor primary and records the selected tag crop coordinates in
`signals`. Move detection is also HP-anchor primary: final `has_moves` is never
set without an HP-bar anchor, and the move-tab/attack-row evidence boxes are
derived from the active battle-tab underline found below that anchor. HP-bar
detection scans the plausible detail-card height, groups colored candidate rows
into bar-like bands, rejects broad art/decorative bands, and chooses the best
eligible HP-bar band so vertically scrolled detail pages can still recover HP
text when CP has moved out of view.

The visible detail-page sections appear in this order, though vertical scrolling
can place upper or lower sections outside the frame:

- CP, with either an outline five-point favorite star or a yellow favorite star
  on the same line.
- Pokemon art.
- Display name.
- HP bar, with a gender sign on the same line when gender is known.
- HP score.
- Tags, sometimes partially obscured.
- Weight, Pokemon type or types, and height.
- Dynamax/Gigantamax section when the Pokemon has that state.
- Max Moves, either near the Dynamax/Gigantamax section or below Moves.
- Mega evolve circle button when the Pokemon was already Mega evolved.
- Stardust, Candy, and Candy XL.
- Mega Energy when the Pokemon may Mega evolve. With a timer if mega evolved right now.
- Purify button for shadow Pokemon.
- Power Up button when the Pokemon can be powered up.
- Evolve buttons when the Pokemon can evolve, including multiple branches.
- Mega Evolve buttons when the Pokemon can Mega evolve, including multiple
  branches.
- Moves, meaning Pokemon GO attack rows. This section is always part of the
  detail page but may be below the visible frame.
- Caught-at information, usually below the visible frame.
- Swap buddies button, usually below the visible frame.

Appraisal overlays have one important special case: when the appraisal stars,
the IV rectangle, and a complete story sentence are present, the background
Pokemon detail page is treated as being in its initial top position. In that
mode, CP, Pokemon art, display name, HP bar/text, and at least partial tag chips
are expected to be visible, so CP/HP OCR uses tight initial-position crops.

## Fragment Extraction

After frame postprocessing, the scanner extracts `PokemonFragment` rows from
stable gated records:

- `detail` fragments may include CP, display-name text, HP current/max, weight,
  height, raw move text, normalized fast/charged/second charged/Max Move fields,
  shadow and Dynamax/Gigantamax gates.
- `appraisal` fragments include CP, display-name text, HP current/max, weight,
  story name/date/location/country text, and IV audit fields. `appraisal`
  fragments use `has_iv_complete` for complete IV rows, while incomplete
  `has_iv` rows keep decoded audit values with `iv_complete=false`.
- Detail/appraisal values are emitted behind their feature gates: CP behind `has_CP`, HP
  behind `has_hp`, height behind `has_height`, move text behind `has_moves`,
  story fields behind `has_story`, and IV/appraisal fields behind `has_iv`.
  Height remains extraction evidence rather than matching evidence.
- `list` fragments keep weak list-layout evidence and any list CP/display-name
  OCR snippets when they are available.
- `non_extractable` and `has_transition` records do not produce fragments.

Each fragment keeps `source_file`, `source_type`, `frame_index`, `timestamp_s`,
`classification`, and `raw_classification`. Extracted fields are stored under a
`fields` map; each field records its `value`, broad `source` category
(`ocr_value`, `ocr_text`, `story_ocr`, `decoded_iv`, `feature_gate`, or
`metadata_catalog`), and the specific frame evidence used.

After extraction, complete story names in `canonical_name_text` are resolved
through the local metadata catalog. If no story name is present, exact
display-name OCR can provide the same species metadata. A conservative one-edit
fuzzy fallback repairs unique story/display names with at least five characters,
such as an OCR-dropped leading letter. Unambiguous matches add `species_key`,
canonical `species_name`, and `pokedex_id` fields. Unresolved or ambiguous names
remain as visible OCR text only and do not fail the scan.

After extraction, gated `moves_text` is parsed against the local metadata
catalog. The first three unambiguous non-Max move names found in UI order fill
`fast_move_*`, `charged_move_*`, and `second_charged_move_*` name/key pairs.
When Dynamax or Gigantamax context is present on a detail frame, lower-panel
context OCR is kept as `power_section_text`; specific Max/G-Max moves resolve
into `max_move_*` from that text first, then from `moves_text` as a fallback.
Generic `Max Moves` header text is not treated as a move. Unresolved or
ambiguous move OCR remains only as raw text.

## Audit Values

The scanner writes candidate values for review, including CP, HP, moves, weight,
height, story text, story completeness, decoded IV values, appraisal star count,
perfect-signal evidence, and IV star/sum agreement. In audit values,
`appraisal_star_count` is the active tier (`0` through `3` amber stars, or `4`
for a red perfect badge), not the number of visible grey star outlines. It is
`null` when no usable appraisal badge/seal and IV panel are visible together.
The audit HTML surfaces CP, HP, weight, height, CP consensus correction
diagnostics, HP anchor coordinates, selected HP/height fallback crop coordinates,
move OCR crop coordinates, moves, story, and IV candidates beside each frame
when available.
For manual review, interpret these values through the same gates used for
extraction: CP behind `has_CP`, HP behind `has_hp`, weight behind `has_weight`,
height behind `has_height`, moves behind `has_moves`, story behind `has_story`,
and IV/appraisal values behind `has_iv`.
The full values remain visible in `frames.jsonl` and feed `fragments.jsonl`;
production export consumes the same gated field semantics but remains separate
from exhaustive audit artifacts.
When `--visible-crop` is enabled, the scanner also writes frame copies with red
rectangles around the visual/OCR crops it used. For JSONL re-scans, these
generated overlays are written inside the new artifact folder instead of beside
the referenced original images.
Visual overlays are a trace of crop work performed during staged analysis:
classification crops run first, and detail/appraisal feature-crop overlays are
written only when those probes actually run for that frame.
These overlays are diagnostics only; OCR text/confidence remains in
`frames.jsonl` and is not cached from the images.

## Production Export

Production export uses source-local stable detail/appraisal evidence only. It
first performs visual-only frame analysis as independent worker tasks. That
screening pass classifies frames as `list`, `detail`, `appraisal`, or `non_extractable`,
keeps only the minimal stability/motion signals needed to split source-local
runs by logical raw type (`detail`, `appraisal`, etc.), and does not settle
final `has_*` extraction gates. Sequence workers own field-gate extraction: each run
walks backward from the latest frame, computes the current frame's gates, and
requests OCR only for fields that are useful for that run type, still missing or
conflicted, and visually probeable on that frame. The scanned production records
then run the same Dynamax/Gigantamax sequence reconciliation used by audit
records, so Gigantamax suppresses generic Dynamax within the stable detail run.
Resolved height and weight are removed from the requested field set, and
unresolved height/weight probes are capped per sequence so stable adjacent frames
are not OCRed repeatedly for the same physical stat. When a probe budget is
exhausted, the live `export.log` records that reason for the next skipped frame.

When `export --max-export-frame-files N` is positive for MP4 input, export
clears only that source's export frame artifact directory and feeds the same
visual screening and production sequence pipeline with bounded frame windows.
The latest `N` frames are extracted first; older chunks use `max(1, N // 5)`
while respecting remaining capacity when possible. Frames classified as `list`
or `non_extractable` are deleted immediately from the export artifact frame
directory. Detail/appraisal runs are grouped source-locally, but the oldest run
touching the current window's older edge is held until the next older chunk can
confirm whether it continues. Completed runs are scanned and repaired through
the same production helpers as unlimited export, then their temporary frame
files are deleted. If FFprobe cannot determine the frame count, export falls
back to unlimited extraction with a `bounded_extraction` warning.
When FFprobe can provide a complete, strictly increasing frame timestamp map,
bounded extraction uses the map to convert each requested frame-index window to
a timestamp-guided FFmpeg seek and writes the result to a temporary chunk
directory first. The files are moved into canonical `frame_%06d.png` names only
after the requested names/count validate. If the timeline is unavailable, FFmpeg
fails, or validation mismatches, that chunk uses the previous exact
`select=between(n, first, last)` extraction path.

Near-miss runs where a physical identity was seen but the anchor was incomplete,
or where short detail move runs may have same-identity support conflicts, are
rescanned automatically with a bounded repair pass that probes the
screen-type-appropriate CP, weight, height, story, IV, and move fields more
deeply for that sequence only. Repair does not relax sequence eligibility: list and transition frames excluded from normal
production sequences are still excluded from repair. Repairable sequences run in
parallel using the export worker setting, while frames within one repaired
sequence are scanned sequentially. `scan-frames` remains the full audit command
and does not use this optimizer.

After repair, export performs an adjacent same-source same-HP stabilization pass
over production sequence results. If a dominant weight is clear across that
stable run, isolated sequence-level weight outliers are corrected and the
affected accepted fields are recomputed before fragments are extracted.

Row assembly builds source-local evidence buckets keyed primarily by
`hp + weight`. Field collection is generic: every processed frame may contribute
any clean exportable field it contains, regardless of raw screen type. Repeated
matching values confirm the selected value, noisy values are ignored, and
conflicting clean identity or anchor fields quarantine the candidate instead of
being silently overwritten. Weak optional conflicts, including display-name,
height, and appraisal-star variants, are reported without dropping an otherwise
anchored identity bucket; conflicted display names are left blank. A valid
raw-appraisal anchor requires `hp + weight`, resolved species, catch
date/location, and a complete IV triplet. If IV/star agreement is absent, the
row may still be accepted with `iv_complete=false` and a warning. A valid
raw-detail anchor requires `hp + weight` plus resolved moves. `hp + weight`
without an anchor is support evidence only: it can merge into an anchored bucket
and fill blanks, but it cannot reject an accepted detail/appraisal anchor.
When repeated same-identity appraisal anchors differ only by minor catch-location
OCR text, a dominant appraisal value can be selected with a warning instead of
rejecting the whole bucket. Move-bearing detail anchors are merged into matching
appraisal rows before export; final rows that still lack normal moves log
whether a compatible detail candidate was missing or lacked resolved moves.
After exact physical-key groups are preferred, export can recover one
same-source, same-HP complete appraisal anchor and one move-bearing detail
anchor when the physical keys differ only by weight or one side lacks weight.
If both sides have species evidence it must agree; multiple same-HP candidates
are recovered only when exactly one reciprocal species match exists, otherwise
they remain separate diagnostics. The recovered merge logs the physical-key
mismatch and keeps the appraisal anchor primary while detail evidence fills
normal moves and other missing fields.
When a single appraisal fragment has `iv_complete=true` and all three decoded IV
values, row assembly treats that same-frame triplet as priority structured
evidence and ignores weaker incomplete neighboring IV candidates unless another
complete triplet conflicts. This keeps bounded artifact cleanup from changing
already-extracted IV quality.
Support evidence with the same source and HP can also fill move/shadow fields
when there is exactly one compatible anchored physical bucket and the support
sequence's own weight was too noisy to anchor. CP is optional and uses
validated/consensus evidence; suffix-polluted CP variants such as an extra OCR
digit are ignored when the clean prefix has repeated evidence, and unresolved CP
conflicts are blanked rather than exported. Appraisal-star conflicts prefer the
visible tier implied by the consensus IV sum when that tier is present in the
candidate evidence. Height conflicts prefer raw detail physical-stat evidence
over fallback height. If required physical
identity evidence is missing, no anchor is present, or clean identity evidence
conflicts, the candidate is normally omitted from `pokemon.csv` and
`pokemon.xlsx` and explained in `warnings.jsonl` and `row_diagnostics.jsonl`.
When complementary support-only detail/appraisal candidates share a unique
source-local `hp + weight` identity and also have canonical identity plus strong
corroboration, export may emit a partial row instead of silently dropping the
Pokemon-like evidence. Scrolled move sequences use HP-anchor-aware
weight and height recovery so visible upper physical stats can still drive the
normal identity path. Production export samples optional CP on only a small
bounded set of probeable frames per sequence, including post-anchor probes, so
clean CP can be preserved without walking long stable runs for CP alone.

List fragments do not participate in production export matching. Cross-source
matching remains future work.

Visible OCR fields remain separate from canonical metadata. Exact or uniquely
fuzzy display-name OCR is used for species resolution only when story species
text is unavailable; unresolved OCR names never overwrite canonical species
data.
