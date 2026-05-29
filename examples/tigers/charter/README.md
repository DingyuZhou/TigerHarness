# Team charter -- tigers

The single entry point for everyone (human and persona) joining this
team. Read this first; everything else follows from here.

## Mission

> TODO: One paragraph -- why this team exists and what success looks like.

## Project and scope

- Primary project this team owns: TODO (path, repo, or product).
- What this team does NOT own: TODO (so we don't drift).

## Permissions and boundaries

- Allowed write zones for every persona on this team:
  - This team folder (your own configs, knowledge, charter, memories,
    prompts, skills).
  - TODO: the project repo this team owns.
- Everything else is read-only unless the Operator explicitly
  authorizes it in a session.

## Working conventions

- Branch naming: `work/YYYY-MM-DD-<slug>`
- Commit prefix: `<persona>:` (e.g. `chief:`, `scout:`)
- Self-critique 2x on every non-trivial change: round 1 for
  correctness/completeness, round 2 for safety/edge cases. Document
  what each round caught in the commit body under a
  `Self-critique 2x applied:` block.
- Never `git push --force`, never amend after push, never
  `git add -A`, never `--no-verify`.

## Using team knowledge

The `../knowledge/` folder is the team's curated reference base.
Start at `../knowledge/INDEX.md` (or `../knowledge/README.md` if no
index exists yet) and drill into the topic you need -- don't load
the whole base eagerly.

If tiger-memory is set up for this team, each persona also has its
own persistent memory under `../memories/<persona>/briefing/`.

## First-read checklist for new personas

Before any substantive work, every persona reads (in order):

1. This charter (you are here).
2. `../knowledge/INDEX.md` (or `../knowledge/README.md`).
3. The owned project repo's top-level `README.md` for context on
   the work the team actually does.
4. Their own briefing at `../memories/<persona>/briefing/README.md`
   if tiger-memory is enabled. The briefing is most useful once you
   already know what the team is and what it works on.

## Updating this charter

When team scope, permissions, or conventions change, update this
file in the same commit as the change. A stale charter is worse
than no charter.
