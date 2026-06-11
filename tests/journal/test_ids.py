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
        # <YYYYMMDD>-<HHmmSS>-<slug>-<uuid8>
        assert re.match(r"^20260602-000000-hello-world-[0-9a-f]{8}$", tid)

    def test_time_of_day_flows_through_verbatim(self):
        """An injected tz-aware clock lands in the id exactly: the
        HHmmSS component comes from the same strftime as the date, so
        14:30:52 UTC produces the literal 143052."""
        tid = new_task_id(
            "x",
            now=dt.datetime(2026, 6, 2, 14, 30, 52, tzinfo=dt.timezone.utc),
        )
        assert tid.startswith("20260602-143052-")

    def test_now_default_uses_utc(self):
        """No ``now`` injected -> default uses utcnow; just verify the
        timestamp prefix has the right shape."""
        tid = new_task_id("anything")
        assert re.match(r"^\d{8}-\d{6}-[a-z0-9-]+-[0-9a-f]{8}$", tid)

    def test_same_second_ids_stay_unique(self):
        """Two ids minted within the same second share the full
        timestamp but differ in the uuid8 suffix; their relative sort
        order within that second is the uuid's accident, by design."""
        when = dt.datetime(2026, 6, 2, 9, 0, 0, tzinfo=dt.timezone.utc)
        a = new_task_id("same second", now=when)
        b = new_task_id("same second", now=when)
        assert a != b
        assert a.startswith("20260602-090000-")
        assert b.startswith("20260602-090000-")

    def test_same_day_ids_sort_in_scheduled_order(self):
        """The feature: same-day new-format ids order by time of day
        under a plain lexicographic sort."""
        day = dict(year=2026, month=6, day=2, tzinfo=dt.timezone.utc)
        nine = new_task_id("zzz late slug", now=dt.datetime(hour=9, **day))
        nine01 = new_task_id(
            "mmm mid slug",
            now=dt.datetime(hour=9, second=1, **day),
        )
        noonish = new_task_id(
            "aaa early slug",
            now=dt.datetime(hour=12, minute=30, **day),
        )
        assert sorted([noonish, nine01, nine]) == [nine, nine01, noonish]

    def test_uniqueness_in_a_burst(self):
        """Two consecutive mints for the same slug at the same instant
        should differ in the uuid8 suffix (32-bit CSPRNG, vanishing
        collision risk)."""
        when = dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc)
        a = new_task_id("hello", now=when)
        b = new_task_id("hello", now=when)
        assert a != b

    def test_cross_day_order_holds_across_formats(self):
        """Ordering rule 2 (defense pin): different dates decide the
        order regardless of format -- YYYYMMDD is fixed-width, so a
        day-earlier legacy id sorts before a day-later new-format id
        even when the legacy slug is lexicographically huge."""
        legacy_earlier = "20260610-zzz-aaaaaaaa"
        new_later = new_task_id(
            "aaa",
            now=dt.datetime(2026, 6, 11, 0, 0, 0, tzinfo=dt.timezone.utc),
        )
        assert sorted([new_later, legacy_earlier]) == [
            legacy_earlier, new_later,
        ]

    def test_max_length_id_is_bounded(self):
        """A maximal 40-char slug yields exactly 65 chars
        (8+1+6+1+40+1+8) and still passes the path-safety filter --
        the time component added 7 chars without squeezing the slug
        budget."""
        tid = new_task_id(
            "a" * 80,
            now=dt.datetime(2026, 6, 11, 23, 59, 59, tzinfo=dt.timezone.utc),
        )
        assert len(tid) == 65
        assert is_safe_task_id(tid)

    def test_slug_overrider_wins(self):
        """``--slug`` should override the title-derived slug."""
        tid = new_task_id(
            "Long Original Title",
            slug_overrider="short-slug",
            now=dt.datetime(2026, 6, 2, tzinfo=dt.timezone.utc),
        )
        assert tid.startswith("20260602-000000-short-slug-")

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
        # Tightened in response to critique: control chars + shell-
        # hazardous chars + leading hyphen + leading/trailing space all
        # reject too.
        "foo\x00bar",   # NUL
        "foo\nbar",     # newline
        "foo\rbar",     # CR
        "foo\tbar",     # tab
        "\x7fbar",      # DEL
        "foo:bar",      # colon (Windows alt stream)
        "foo bar",      # embedded space
        "-rf",          # leading hyphen (CLI confusion)
        " 20260602-x-12345678",   # leading whitespace
        "20260602-x-12345678 ",   # trailing whitespace
    ])
    def test_unsafe(self, bad):
        assert is_safe_task_id(bad) is False
