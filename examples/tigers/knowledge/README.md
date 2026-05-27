# Team knowledge -- tigers

This folder is the team's curated, lazy-loaded reference base.
Personas read from here on demand -- not eagerly -- so the corpus
stays cheap to keep open.

## How to organize

Top-down, with a clear entry point:

- `INDEX.md` -- one-paragraph header, one line per topic. Personas
  read INDEX first, then drill into the topic they need.
- `<topic>.md` -- one file per topic. Keep each under ~200 lines;
  if it grows, split it and add a topic-local TOC.

## How to use

1. Start at `INDEX.md`. Until you have one, this `README.md` is the
   entry point -- replace with `INDEX.md` once topics accumulate.
2. Read only the topic file you need.
3. When the underlying code or process changes, update the matching
   topic file in the same commit.

## What belongs here

- Curated, evergreen reference the team needs repeatedly.
- Per-module deep dives, architecture maps, working agreements.
- Convention guides specific to this team's project.

## What does NOT belong here

- Team governance -- mission, scope, permissions, and conventions
  live in `../charter/`, not here. Knowledge is reference material;
  the charter is the operating manual.
- Task working notes -- the task-runner writes those to
  `../task_journal/` automatically (gitignored runtime artifact).
- Personal memory -- use `../memories/<persona>/`.
- Source code -- use the project repo.
- Stale content -- prune aggressively.
