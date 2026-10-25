+++
id = "0001"                      # 4 digits, unique; the file is named 0001-short-slug.md
phase = "01"
title = "Parse CSV upload into rows"
depends_on = []                  # task ids that must be merged first (only real prerequisites)
files = ["src/app/upload.py"]    # the ONLY paths the implementer may edit (files, dirs or globs)
tests = ["tests/acceptance/test_0001_upload.py"]   # where QA writes the frozen acceptance tests
# lane = "risk"                  # optional override: light | ui | risk | default (default: from project.toml globs)
+++

## Goal
One or two sentences: the user-visible outcome and why it matters. Spec refs: R-3, R-4.

## Interface
The exact contract the tests will pin: signatures, routes, request and response shapes, error
cases. For example: `parse_upload(data: bytes) -> list[Row]`, which raises `UploadError` with
code `EMPTY`, `TOO_LARGE` (over 5 MB) or `BAD_HEADER`.

## Done When
Each item can be checked by an automated test.
- [ ] A valid 3-row CSV returns 3 `Row`s whose fields are typed as in the spec.
- [ ] An empty upload raises `UploadError(code="EMPTY")`.
- [ ] A 6 MB upload raises `UploadError(code="TOO_LARGE")` without reading the whole body.

## Non-Goals
What NOT to build in this task, so scope stays tight (e.g. no XLSX, no streaming UI).

## Notes
How to run or see it (for QA functional and UI review), known pitfalls, links to spikes and ADRs.
