"""Tests for ``tigerharness.journal.ids``: slugify + task-id minting."""

from __future__ import annotations

import datetime as dt
import re

import pytest

from tigerharness.journal.ids import (
    JournalIdError,
    is_safe_task_id,
    new_task_id,
    slugify,
)


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------

class TestSlugify:
    def test_basic_ascii(self):
        assert slugify("Hello World") == "hello-world"

    def test_punctuation_stripped(self):
        assert slugify("Add the subscription backend!") == (
            "add-the-subscription-backend"
        )

    def test_unicode_falls_back_to_default(self):
        """Pure-unicode input has no [a-z0-9] tokens after lowercasing
        and so falls back to 'task' rather than producing an empty
        slug."""
        assert slugify("???!!!") == "task"
        assert slugify("你好") == "task"

    def test_max_len_truncates_and_trims(self):
        out = slugify("a" * 60, max_len=10)
        assert out == "a" * 10
        assert len(out) == 10

    def test_max_len_trims_trailing_hyphen(self):
        """A truncation that lands mid-token should not leave a dangling
        hyphen at the end."""
        out = slugify("aaaa bbbb cccc dddd", max_len=10)
        assert not out.endswith("-")

    def test_consecutive_specials_collapse(self):
        assert slugify("foo   bar---baz!!!qux") == "foo-bar-baz-qux"

    def test_max_len_below_one_raises(self):
        with pytest.raises(JournalIdError):
            slugify("x", max_len=0)


# ---------------------------------------------------------------------------
# new_task_id
# ---------------------------------------------------------------------------

class TestNewTaskId:
    def test_format(self):
        tid = new_task_id(
            "Hello world",
            now=dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc),
        )
        # <YYYYMMDD>-<slug>-<uuid8>
        assert re.match(r"^20260602-hello-world-[0-9a-f]{8}$", tid)

    def test_now_default_uses_utc(self):
        """No ``now`` injected -> default uses utcnow; just verify the
        date prefix has the right shape."""
        tid = new_task_id("anything")
        assert re.match(r"^\d{8}-[a-z0-9-]+-[0-9a-f]{8}$", tid)

    def test_uniqueness_in_a_burst(self):
        """Two consecutive mints for the same slug at the same instant
        should differ in the uuid8 suffix (32-bit CSPRNG, vanishing
        collision risk)."""
        when = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)
        a = new_task_id("hello", now=when)
        b = new_task_id("hello", now=when)
        assert a != b

    def test_slug_overrider_wins(self):
        """``--slug`` should override the title-derived slug."""
        tid = new_task_id(
            "Long Original Title",
            slug_overrider="short-slug",
            now=dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc),
        )
        assert tid.startswith("20260602-short-slug-")

    def test_collision_retries_once_then_succeeds(self):
        """First proposed id collides, second succeeds. The exists_check
        is called with each candidate."""
        seen = []

        def exists(cand: str) -> bool:
            seen.append(cand)
            return len(seen) == 1  # only the first candidate collides

        tid = new_task_id(
            "hello",
            now=dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc),
            exists_check=exists,
        )
        assert tid == seen[-1]
        assert len(seen) == 2

    def test_collision_persists_raises(self):
        """If both attempts collide, we hard-error instead of falling
        into an infinite loop."""
        def always(cand: str) -> bool:  # noqa: ARG001
            return True
        with pytest.raises(JournalIdError):
            new_task_id(
                "hello",
                now=dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc),
                exists_check=always,
            )


# ---------------------------------------------------------------------------
# is_safe_task_id
# ---------------------------------------------------------------------------

class TestIsSafeTaskId:
    def test_normal_id(self):
        assert is_safe_task_id("20260602-foo-12345678") is True

    @pytest.mark.parametrize("bad", [
        "",
        "   ",
        ".",
        "..",
        ".hidden",
        "foo/bar",
        "foo\\bar",
        "foo..bar",
        "..bar",
    ])
    def test_unsafe(self, bad):
        assert is_safe_task_id(bad) is False
