# Reprocessing regression checks

Use Python 3.10 or later, from `agentnet-annotator`:

```sh
python -m unittest discover -s api/tests -v
```

The tests load production processing code while isolating desktop, logging,
model/network and OpenCV dependencies. They cover encoder failure, invalid time
ranges, empty/unreadable/truncated output, publishing only validated clips,
reusing good clips, regenerating corrupt or playable-but-wrong-length clips,
buffer/source-boundary frame counts, fractional FPS, and persisting/exposing failures.
They do not exercise the installed system's actual codec or packaged application.

Before release, on both Windows and macOS, use copies of recordings to check:

- Normal processing produces playable clips and reaches `done`.
- Interrupt clipping, restart, and confirm valid clips are reused.
- Corrupt an existing nonempty clip, retry, and confirm it is regenerated.
- An unavailable H.264 encoder produces `failed` with a visible reason.
- Paths containing spaces and Chinese characters work in the installed app.

New clips are decoded fully and their frame count is checked before publishing.
Reused clips are decoded fully and compared to the same source-based expected
frame count too, so retry validation takes additional time.

Model failure checks also require the project `requests` dependency (use the
project virtual environment). They mock all HTTP calls: timeout, connection,
HTTP status, non-JSON/invalid result shapes, cache validation, optional model
fallback, manual Retry, and disabled-model behavior. No remote requests occur.

Remote model recognition is optional enrichment. If it fails, the reducer logs
the exception internally, keeps the available local targets and continues dump
and clipping. The task can reach `done` without a model result; no model-failure
toast or Retry is needed. This does not mean model annotation succeeded. Real
video/processing failures still produce visible failed state and Retry.

HTTP requests retain 10-second connect / 60-second read timeouts. Response
validation and cache checks remain strict: invalid results are never saved as
successful predictions, and old invalid entries are removed. The endpoint and
successful-result path remain available for a future replacement service.

These optional-model changes require a new build; the installed macOS
1.0.1-test.2 build still treats model errors as processing failures. Tests exercise
both Darwin and Windows branches with mocked model failures, confirm the pipeline
finishes and emits success, and confirm a subsequent encoder error still fails.
Windows packaged application checks remain pending.

State corruption checks distinguish absent files (legacy fallback) from existing
but unreadable/invalid state (visible failed state with Retry). Root JSON values,
status, attempt counts, stage, error text and processing configuration are
validated. Automatic recovery skips bad recordings and continues with valid
ones; manual Retry rebuilds damaged state without modifying raw input files.
Read-only discovery never rewrites a corrupted state file.

Duplicate Retry checks use synchronization events to reproduce overlapping
requests and a worker finishing before the second request can change state.
Exactly one Retry is queued; failed writes/queue puts release the reservation
and permit another Retry. The frontend also blocks duplicate in-flight requests.

Model cache entries require at least one nonempty UI target text field:
`title`, `value`, `description`, `role`, or `role_description`. Non-null target
fields must be strings; message-only objects, blank targets and wrong types are
rejected and removed from legacy caches. No undocumented no-target sentinel is
treated as success.

The repository ignore rules now include these Python tests and this README,
while keeping generated logs and bytecode ignored.
