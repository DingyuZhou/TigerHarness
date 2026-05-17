---
name: lab-notebook-quarter-roll
description: Roll the oldest quarter of `<project>/lab_notebook.md` Activity entries out to `<project>/lab_notebook_archive/YYYY-QN.md`. Use at the start of each new quarter or when an active lab_notebook.md keeps more than two quarters of Activity.
---

# lab-notebook-quarter-roll

A quarterly maintenance task: trim the active `lab_notebook.md` so it
keeps only the current + previous quarter of Activity entries. Older
entries roll out to a dated archive file.

## Procedure

1. Identify the project's `lab_notebook.md`.
2. Determine the current quarter from today's date.
3. The "oldest kept" quarter is current - 1. Everything older rolls out.
4. Append rolled entries to `<project>/lab_notebook_archive/YYYY-QN.md`.
5. Remove rolled entries from the active `lab_notebook.md` Activity.
6. For directives whose Activity rolled out, append `-> archived in YYYY-QN.md`.
7. Commit.

## Rules

- Archive is append-only and immutable.
- Directives stay whole in the active notebook.
- Always retain current + previous quarter of Activity.
