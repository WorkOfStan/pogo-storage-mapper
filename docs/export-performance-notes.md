# Export Performance Notes

Source artifacts inspected:

- `output/litwick_export`
- `output/machamp_export`
- `output/Alola_export`

## Summary

The meaningful bottlenecks are production sequence scanning, visual analysis, and
bounded MP4 frame extraction. CSV/XLSX writing, row assembly, cross-sequence
stabilization, and sequence grouping are small by comparison. No recommended
speedup should reduce OCR/probe quality; the safest opportunities are better
diagnostics, avoiding repeated work, and avoiding repeated MP4 decode/scanning
work while preserving exact requested frame indices.

| Export  |    Runtime | Frames | Scanned frames | Sequences |                Workers | Cap | Peak files | Peak % | Largest phases                                                |
| ------- | ---------: | -----: | -------------: | --------: | ---------------------: | --: | ---------: | -----: | ------------------------------------------------------------- |
| Litwick | 11,044.1 s |  2,150 |            536 |        37 |  visual 16, sequence 6 | 400 |        400 |  18.6% | sequence 9,131.8 s; visual 1,710.0 s; extraction 149.1 s      |
| Machamp | 19,146.5 s |  3,841 |          1,292 |       137 | visual 16, sequence 12 | 500 |        651 |  16.9% | sequence 14,793.1 s; visual 3,602.3 s; extraction 651.7 s     |
| Alola   | 64,407.0 s | 18,446 |          5,225 |       520 | visual 16, sequence 11 | 600 |      3,085 |  16.7% | sequence 34,496.2 s; visual 15,494.0 s; extraction 13,826.9 s |

## Bottlenecks

| Operation                  | Evidence                                                                                                                                                                                                  | Parallelized                                                                 | Worker idle evidence                                                              | Speedup opportunity                                                                                                 | Accuracy risk                                              |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| Sequence scanning          | Largest phase in all three runs; 53.6% of Alola runtime.                                                                                                                                                  | Yes, by sequence batch. Frames inside one sequence are scanned sequentially. | Existing `export.log` records starts only, so exact idle time is not recoverable. | High: reduce redundant repair/probe work only when accepted fields are already resolved; improve batch diagnostics. | Medium if probes are removed; low for diagnostics/caching. |
| Visual analysis            | Second-largest phase for Litwick/Machamp and 24.1% of Alola.                                                                                                                                              | Yes, by frame batch.                                                         | Existing thread IDs are not stable worker identities.                             | Medium: cache visual records for repeated scans or improve scheduling.                                              | Low if cached by exact frame content/path and settings.    |
| MP4 frame extraction       | 21.5% of Alola runtime; bounded extraction repeatedly invoked FFmpeg range filters. The output ranges differ, but each invocation can still rescan/decode earlier MP4 data to reach its requested frames. | No; chunk extraction is serial.                                              | Workers wait while chunks are extracted.                                          | High for timestamp-guided seek extraction with exact frame-index validation/fallback, or persistent decode.         | Medium unless frame indexes/timestamps remain identical.   |
| OCR/probe groups           | Alola lifecycle shows 8,831 sequence probe/skip entries and 3,584 repair entries. Common groups include `display_name,height,hp,special_sections,weight` and `height,hp,special_sections,weight`.         | Via sequence workers; OCR within one frame is sequential.                    | New diagnostics are needed for active/idle worker counts.                         | Medium: skip only already-resolved fields and cache per-frame OCR results for identical requested field sets.       | Medium if field gates change; low for exact cache hits.    |
| CP probing                 | Alola has 431 CP-only processed frames and 215 `stop:probe_budget_exhausted:cp` skips.                                                                                                                    | Within sequence workers.                                                     | Not available from old logs.                                                      | Low to medium: current probe budget already limits CP.                                                              | Medium; CP is noisy and consensus-driven.                  |
| Height/weight probing      | Alola has repeated physical-stat groups; height/weight budgets limit unresolved repeats.                                                                                                                  | Within sequence workers.                                                     | Not available from old logs.                                                      | Low to medium: avoid re-probing once accepted and confirmed.                                                        | Medium; physical stats anchor identity.                    |
| Move extraction            | Alola has 858 broad move-section groups and 145 `height,hp,moves,special_sections,weight` groups.                                                                                                         | Within sequence workers.                                                     | Not available from old logs.                                                      | Medium: avoid repair rescans when move anchor is already complete.                                                  | High if move evidence is skipped too early.                |
| Appraisal/IV extraction    | Good output quality is reported; complete IV extraction must be preserved.                                                                                                                                | Within sequence workers.                                                     | Not available from old logs.                                                      | Low: diagnostics first.                                                                                             | High.                                                      |
| Sequence pairing/anchoring | Grouping is small, but retention depends on open edge sequences.                                                                                                                                          | Mostly serial grouping; scanning is parallel.                                | Not a CPU bottleneck.                                                             | Medium for disk predictability, not runtime.                                                                        | High if early deletion loses provenance.                   |
| Artifact cleanup           | Lifecycle confirms all newer Machamp/Alola frame files were deleted by run end.                                                                                                                           | Serial cleanup.                                                              | Not a CPU bottleneck.                                                             | Low: improve accounting/logs.                                                                                       | Low if deletion remains after extraction.                  |
| CSV/XLSX writing           | Not separately timed in old runs; row counts are small.                                                                                                                                                   | No.                                                                          | Not material.                                                                     | Low.                                                                                                                | Low.                                                       |

## Frame Retention

Alola extracted 18,446 total frames. The recorded peak was 3,085 temporary frame
files, or 16.7% of total frames. The final retained frame count was 0, and
18,446 frame files were deleted by lifecycle accounting: 949 list/non-extractable
frames, 14,716 completed-sequence frames, and 2,781 unsequenced visual frames.

The peak exceeded `--max-export-frame-files 600` because the setting is a soft
temporary frame-artifact target. Bounded export processes newest-to-oldest
chunks, deletes list/non-extractable frames immediately, and keeps detail or
appraisal frames until a sequence is complete and structured data has been
extracted. The oldest open sequence touching a chunk boundary must remain until
the next older chunk proves whether it continues. This is accuracy/provenance
retention, not a final cleanup leak.

## `--workers auto`

The implementation resolves `auto` with `os.cpu_count()` logical CPUs, capped by
the queued item count. The recent runs selected 16 visual workers. Sequence
worker counts were lower because completed sequence batches sometimes contained
fewer ready sequences than logical cores, and frames inside a single sequence are
processed sequentially. Existing logs cannot prove active/idle worker time; new
`performance_summary.json` and `export.log` worker batch diagnostics should be
used for future runs.

For the i5-13450HX, `--max-export-frame-files 800` to `1000` is a practical
starting range when disk space allows. Increasing the cap may reduce extraction
chunk churn and provide larger sequence batches, but it does not directly raise
the configured worker count and can increase temporary disk usage. Use lower
values only when disk pressure matters.

## Recommended Next Steps

- Use the new `artifacts/performance_summary.json` on the next Alola-style run
  to inspect worker batch sizes, chunk retention, time-guided extraction hit vs.
  fallback counts, probe groups, and artifact writing time.
- Benchmark the new time-guided MP4 extraction on an Alola-style partial run and
  compare representative rows/files against exact range-select extraction.
- Consider per-frame OCR result caching only for identical frame path, settings,
  and requested field set.
- Keep CP, height/weight, move, and IV probe quality unchanged unless a targeted
  regression proves no output data changes.
