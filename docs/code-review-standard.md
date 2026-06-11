# TigerHarness code review standard

The bar every change must clear before it merges. It applies to all
contributors — Shohoku team personas and external contributors alike.
Reviewers cite findings by section number ("standard §2: missing
regression test"); authors are expected to have checked their own work
against every section before requesting review. Workflow QA gates and
review playbooks cite this document as the single source of truth.

Severity vocabulary used in verdicts: **blocker** (must fix before
merge), **should-fix** (fix before merge unless explicitly waived),
**nit** (author's discretion). A review approval means "I understand
this change and would maintain it."

## §1 Correctness

1. The change does exactly what its commit message and docs claim —
   nothing more. The reviewer must be able to trace every claim to
   code. Untraceable claims are blockers.
2. No silent behavior changes: anything that alters observable
   behavior (output, exit codes, file formats, defaults) is named in
   the commit body.
3. Edge cases are handled where the data lives: empty inputs, missing
   files, malformed YAML/JSON frontmatter, concurrent writers. If an
   edge case is consciously unsupported, the code says so (explicit
   error), not the review thread.

## §2 Tests

1. The coverage floor is **100% line + branch** (`fail_under = 100`,
   pyproject.toml) and it is a floor, not a target to game: tests
   assert behavior, not implementation details, and must fail when the
   behavior breaks.
2. New code ships with its tests in the same commit. A bug fix ships
   the regression test that would have caught the bug — written first,
   observed failing.
3. No test deletions or skips to make a change pass; weakening an
   assertion is a behavior change and falls under §1.2.
4. Coverage-excluded paths (e.g. `agent_sdk/examples/`) stay excluded
   only with a recorded reason in pyproject.

## §3 Error paths and exit codes

1. Every new failure mode is deliberate: raise a typed exception, or
   document the propagation. Bare `except:` and silent `pass` are
   blockers.
2. CLI commands follow the repo's exit-code contract: `0` success,
   `1` validation/content failure (machine-readable envelope on stdout
   where the command emits JSON), `2` operator error (bad arguments,
   missing files — message on stderr). New commands keep this shape.
3. Error messages name the thing that failed and the path to fix it
   ("no active workflow task with id X at <path>"), not just the
   failure.

## §4 Atomicity and concurrent state

1. Mutations of shared on-disk state use the established patterns:
   atomic write (write-temp-then-rename, as in `tiger_memory/store.py`)
   and compare-and-set re-read (as in `journal claim`). New ad-hoc
   read-modify-write of shared files is a blocker.
2. Anything two processes can touch concurrently (journal status,
   memory stores, thread maps) is reviewed for the second writer, not
   just the happy path.

## §5 Docs

1. User-visible changes update the matching `docs/` page and, where
   applicable, README "Known limitations & roadmap" — in the same
   commit.
2. Public functions, CLI flags, and config keys carry docstrings/help
   text that state what is true, verified against the code — never
   aspirations.
3. If a doc elsewhere (including team knowledge bases) becomes stale
   because of the change, the commit body says which one, so the
   follow-up is traceable.

## §6 Security and secrets

1. No secrets in code, tests, logs, or commit messages. When a secret
   must be referenced, prefix/suffix only (`xoxb-...XYZ`).
2. External input (Slack payloads, file uploads, env vars, CLI args)
   is validated before use; file-path operations are contained to
   their intended roots (no traversal out of a store/journal/team
   directory).
3. New network calls, new subprocess invocations, and new
   environment-variable reads are called out in the commit body and
   reviewed explicitly.

## §7 Scope and size

1. One concern per change: refactors land separately from behavior
   changes; drive-by edits are rejected even when correct.
2. Keep diffs reviewable in one sitting (guideline: ~400 changed
   lines). The guideline yields to atomicity — never split what must
   land together to keep tests green (§2) — but a large diff needs a
   map in the commit body.
3. Generated or vendored content is isolated in its own commit so the
   human-authored diff stays readable.

## §8 Dependencies and compatibility

1. Zero hard dependencies is a design property of this package. Any
   new dependency goes behind an extra in pyproject and requires
   explicit Operator/maintainer approval before the work lands
   (yellow-light surface).
2. Public contracts — CLI verbs and flags, JSON envelopes, on-disk
   schemas (status.json, store layouts), env-var names — are
   versioned promises. A breaking change is named loudly in the
   commit body and the docs, never discovered by a consumer.
3. Supported Python floor (3.11+) is respected; no syntax or stdlib
   features beyond it.

## §9 Author's pre-review: self-critique 2x

1. Every non-trivial change runs two self-critique rounds before
   review: round 1 correctness/completeness, round 2 safety/edge
   cases/security — revising after each.
2. The commit body records what each round caught under a
   `Self-critique 2x applied:` block. "Nothing" is a suspicious
   answer; say what you checked.

## §10 Commit hygiene

1. Format: `<persona-or-author prefix>: <imperative summary, <= 72
   chars>`, body explaining what changed and why (the why is the part
   the diff can't show).
2. Stage files explicitly — never `git add -A` / `git add .` (parallel
   sessions leave unrelated files in the tree).
3. No `--amend` after push, no `--no-verify`, no force-push. History
   on shared branches is append-only; fixes are new commits.

## §11 Review verdicts

1. Findings cite a section of this standard and carry a severity
   (blocker / should-fix / nit). A verdict without citations is an
   opinion, not a review.
2. The reviewer states what they actually verified (ran the tests,
   traced the path, checked the docs) — rubber-stamp approvals are
   themselves a §11 violation.
3. Disagreements between author and reviewer escalate to the
   maintainer/Operator rather than being worn down in-thread.
