"""Discover, from the source itself, every env var name the package names.

The mechanism behind ``tests/test_env_hygiene.py``. It exists because the
suite's protection against ambient environment is a hand-maintained list in
``tests/conftest.py``, and a hand-maintained list has no way to notice the
variable nobody added to it. ``TestClaimRailGuard`` was red on one host and
green in CI for weeks for exactly that reason (PR #109): the fix there was
one variable; the hole was the class.

**Why literal shape, not read mechanism.** The obvious scan -- find
``os.environ.get("X")`` and ``os.getenv("X")`` -- is the scan that produced
the wrong number twice. It misses every layer of indirection this package
actually uses, and there are five distinct ones:

1. direct ``os.environ.get`` / ``os.environ[...]`` (``journal/sweep.py``);
2. ``Settings.get`` / ``.flag`` / ``.number``, which read ``os.environ``
   and *then* a team ``configs/.env`` (``autodrive/settings.py``) -- the
   indirection that hid the rail knob;
3. ``from_env(env=None)`` classmethods that bind ``e = os.environ`` and
   read through the local name ``e`` (``slack_bridge/reconnect.py``,
   ``slack_bridge/idle_compact.py``);
4. a module-level constant holding the name, read somewhere else entirely
   (``AUTOSTART_ENV = "TIGERHARNESS_AUTODRIVE_AUTOSTART"``);
5. a *tuple of candidate spellings* iterated as data -- one knob, two
   accepted names, invisible to any per-call-site matcher
   (``notify.py``'s ``SLACK_ALLOWED_USER_IDS`` / ``ALLOWED_SLACK_USER_IDS``).

Matching on receivers means maintaining a second hand-maintained list --
of receiver names -- which rots the moment someone writes ``cfg.get`` or
``src.get``, and rots *silently*, which is the whole failure mode. So this
scan deliberately ignores syntax and matches on the **shape of the string**:
every SCREAMING_SNAKE_CASE literal anywhere in the package is a candidate,
and ``conftest.py`` must account for each one. A sixth read mechanism
invented tomorrow is covered on the day it is written, because the name
still has to appear as a literal to be read at all.

The trade is false positives -- constants of that shape that are not env
vars at all. That is the right trade: a false positive is a one-line
decision recorded in ``NOT_ENV_VAR_LITERALS``, while a false negative is
another fortnight of a red suite nobody can explain.

**Out of scope, deliberately.** ``{**os.environ, ...}`` in the agent-SDK
backends (``claude_p.py``, ``codex_exec.py``) hands the *whole* ambient
environment to a spawned child. That is a bulk passthrough, not a named
read: no literal exists to find, and by design -- a spawned agent inherits
the operator's shell. It cannot skew an assertion unless a test asserts on
the child env's full contents, which none do (they index single keys).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

#: The package source. Derived from this file's location rather than
#: configured, so moving or renaming a module cannot silently narrow the
#: scan -- there is no file list to fall out of date.
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "tigerharness"

#: SCREAMING_SNAKE_CASE with at least one underscore. The underscore is
#: load-bearing: without it the pattern also matches the single-word
#: all-caps dict keys used as record fields and marker names throughout
#: ``tiger_memory`` and ``journal/wfcore`` (``KIND``, ``SUMMARY``,
#: ``TOPIC``, ``TRIGGER``, ...), which would bury the real signal under
#: dozens of entries that are obviously not environment variables. Every
#: env var this package reads matches.
ENV_NAME_SHAPE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")


def _all_literal_ids(tree: ast.Module) -> set[int]:
    """Node ids of strings inside a module-level ``__all__``.

    Those strings are Python *identifiers* being re-exported, not env var
    names, and three of them already match :data:`ENV_NAME_SHAPE`
    (``AKAGI_CRITIC_PROMPT_TEMPLATE``, ``AYAKO_CRITIC_PROMPT_TEMPLATE``,
    ``DEFAULT_SESSIONS_PATH``). Excluding them structurally rather than
    naming them one by one matters for the guard's credibility: a
    contributor who exports a new SCREAMING_SNAKE constant should not be
    told they broke an environment-hygiene test, because the next thing
    they learn is that this test says confusing things, and the one after
    that is how to silence it.
    """
    skip: set[int] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            continue
        for child in ast.walk(node.value):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                skip.add(id(child))
    return skip


def env_name_literals(root: Path | None = None) -> dict[str, list[str]]:
    """Map every env-var-shaped literal under *root* to its source locations.

    Locations are ``<path-relative-to-root>:<lineno>``, sorted, so a guard
    failure can name where the offending read lives instead of only that a
    count moved.
    """
    root = PACKAGE_ROOT if root is None else root
    found: dict[str, set[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        skip = _all_literal_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            if not isinstance(node.value, str) or id(node) in skip:
                continue
            if ENV_NAME_SHAPE.match(node.value):
                where = f"{path.relative_to(root)}:{node.lineno}"
                found.setdefault(node.value, set()).add(where)
    return {name: sorted(locs) for name, locs in sorted(found.items())}
