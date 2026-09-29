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


#: Node types that mean the env var name was *built* rather than written:
#: an f-string, a concatenation or ``%``, or a call such as ``.format()``
#: / ``.join()`` / ``.upper()``.
_COMPUTED_NAME_NODES = (ast.JoinedStr, ast.BinOp, ast.Call, ast.IfExp)


def _named(node: ast.AST, name: str) -> bool:
    """``os.<name>`` or a bare ``<name>``.

    The bare form matters: ``from os import environ`` would otherwise walk
    straight past an attribute-only check, and the package would have lost
    the guarantee without anything saying so. No module imports it that way
    today -- which is the moment to make sure one cannot start quietly.
    """
    return (
        (isinstance(node, ast.Attribute) and node.attr == name)
        or (isinstance(node, ast.Name) and node.id == name)
    )


def _is_environ(node: ast.AST) -> bool:
    return _named(node, "environ")


def _is_getenv(node: ast.AST) -> bool:
    return _named(node, "getenv")


def computed_env_reads(root: Path | None = None) -> list[str]:
    """Locations where an ``os.environ`` key is a *computed* expression.

    :func:`env_name_literals` rests on one assumption: a variable has to be
    named as a literal somewhere to be read at all. That assumption is true
    of this package today -- every dynamic read resolves to a name written
    down nearby (``_logging.py``'s ``ENV_VAR`` constant, ``progress.py``'s
    ``CHANNEL_ENV_VARS`` chain, ``notify.py``'s candidate-spelling tuple) --
    but nothing enforces it, and an assumption a guard depends on silently
    is the shape of defect this whole task exists to remove.

    So it is checked instead of assumed. A name passed through a variable is
    fine (the literal still exists to be found); a name *assembled* --
    ``os.environ.get(f"TIGERHARNESS_{suffix}")`` -- is not, because no scan
    can enumerate it and the guard would go on reporting completeness it no
    longer has.

    Scope is ``os.environ`` and ``os.getenv``, the ambient channel itself.
    ``Settings.get`` funnels into ``self.env``, which *is* ``os.environ``;
    its keys are literals or module constants today and the shape scan
    covers them.
    """
    root = PACKAGE_ROOT if root is None else root
    bad: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            key: ast.AST | None = None
            if isinstance(node, ast.Subscript) and _is_environ(node.value):
                key = node.slice
            elif isinstance(node, ast.Call) and node.args:
                func = node.func
                # ``getenv`` is checked without an Attribute gate so the
                # bare ``from os import getenv`` form is covered too.
                environ_method = (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("get", "pop", "setdefault")
                    and _is_environ(func.value)
                )
                if environ_method or _is_getenv(func):
                    key = node.args[0]
            if key is not None and isinstance(key, _COMPUTED_NAME_NODES):
                bad.append(f"{path.relative_to(root)}:{node.lineno}")
    return sorted(bad)


#: ``os.environ`` mutators. ``pop`` / ``setdefault`` / ``clear`` /
#: ``update`` are the dict-API spellings, and the three dunders are the
#: same operations spelled explicitly -- included because a guard that
#: only understands the sugar is one ``__setitem__`` away from going
#: blind, and both were *measured* to really mutate the environment rather
#: than assumed to. ``putenv`` / ``unsetenv`` are the ``os``-level ones,
#: listed because they mutate the process environment without ``environ``
#: appearing anywhere in the expression -- a scan keyed only on
#: ``environ`` would walk straight past them.
_ENVIRON_MUTATOR_METHODS = frozenset({
    "setdefault", "update", "pop", "clear",
    "__setitem__", "__delitem__", "__ior__",
})
_OS_LEVEL_MUTATORS = frozenset({"putenv", "unsetenv"})


def environ_write_sites(root: Path | None = None) -> list[str]:
    """Locations where the package **writes** the process environment.

    The counterpart to :func:`computed_env_reads`, and the guard behind
    ``TestNothingWritesTheProcessEnvironment``. Reading ambient config is
    ordinary; *writing* it is not, because the write outlives the call: it
    is visible to every later reader in the process and is copied wholesale
    into every child spawned with ``{**os.environ}`` (``claude_p.py``,
    ``codex_exec.py``).

    That is not hypothetical. ``slack_bridge/notify.py``'s ``.env`` loader
    used to do exactly this -- ``if key and key not in os.environ:
    os.environ[key] = value`` over every line of a team's ``.env`` -- so one
    Slack post exported that whole file, unrelated secrets included, for the
    life of the process. ``tests/conftest.py`` had to scrub before *every*
    test because one test loading a fixture ``.env`` could turn
    ``TIGERHARNESS_AUTODRIVE_AUTOSTART`` on for the rest of the run, and
    ``monkeypatch`` cannot undo a write it did not make.

    Scoped to the ambient channel itself, exactly as
    :func:`computed_env_reads` is: ``some_dict["K"] = v`` is an ordinary
    assignment and flagging it would bury the signal.

    A write is reported by location rather than name because the name is
    usually a variable at a write site (the loader's was ``key``), so there
    is nothing for the shape scan to find -- which is precisely why this
    needed its own detector rather than another register entry.

    Locations are de-duplicated: one expression can legitimately match two
    branches below (``os.environ |= d`` is both an ``AugAssign`` and a
    Store-context ``environ`` attribute), and reporting the same line twice
    reads as two defects.
    """
    root = PACKAGE_ROOT if root is None else root
    bad: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            hit = False
            if isinstance(node, ast.Subscript) and _is_environ(node.value):
                # ``os.environ[k]`` in a *read* context is fine; only Store
                # (assignment) and Del (``del os.environ[k]``) are writes.
                hit = isinstance(node.ctx, (ast.Store, ast.Del))
            elif isinstance(node, ast.AugAssign):
                # ``os.environ |= {...}`` -- verified to really mutate the
                # environment, and invisible to the Subscript check above
                # because there is no subscript.
                hit = _is_environ(node.target)
            elif isinstance(node, ast.Attribute) and node.attr == "environ":
                # ``os.environ = {...}`` / ``del os.environ`` -- replacing
                # the attribute outright, which every later reader in the
                # process sees. Only the attribute form: a bare
                # ``environ = ...`` is a local rebind, and flagging it would
                # fire on any innocent variable of that name.
                hit = isinstance(node.ctx, (ast.Store, ast.Del))
            elif isinstance(node, ast.Call):
                func = node.func
                hit = (
                    isinstance(func, ast.Attribute)
                    and func.attr in _ENVIRON_MUTATOR_METHODS
                    and _is_environ(func.value)
                ) or any(_named(func, name) for name in _OS_LEVEL_MUTATORS)
            if hit:
                bad.add(f"{path.relative_to(root)}:{node.lineno}")
    return sorted(bad)
