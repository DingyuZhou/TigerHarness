"""Briefing rebuild — event-driven walking + atomic folder swap.

Implements §8 of the design doc:
    Layer 1: full shorts (newest F working days)
    Layer 2: dailies (next D working days; shorts on fallback)
    Layer 3: weeklies (next W working days; covers a calendar range)
    Layer 4: monthlies (next M working days; covers a calendar range)
    + longer_memory.md and must_memorize.md (always read first)

Output layout:
    briefing/
        MANIFEST.md
        must_memorize.md
        longer_memory.md
        recent/   layer 1 shorts
        daily/    layer 2
        weekly/   layer 3
        monthly/  layer 4

All written to briefing.tmp/ then mv-swap with briefing/.
"""
from __future__ import annotations

import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

from . import frontmatter
from .config import Config
from .state import iso_now
from .store import (
    DAILY_RE,
    MONTHLY_RE,
    SHORT_RE,
    WEEKLY_RE,
    Store,
)


def rebuild_briefing(cfg: Config, store: Store) -> None:
    """Atomic briefing rebuild. No-op shortcut: if journal/ unchanged, skip."""
    if _briefing_up_to_date(store):
        return

    # Stage in a temp directory next to briefing/ so the rename is on the
    # same filesystem (atomic on POSIX).
    parent = store.paths.briefing.parent
    tmp = Path(tempfile.mkdtemp(prefix="briefing.tmp.", dir=parent))
    try:
        for sub in ("recent", "daily", "weekly", "monthly"):
            (tmp / sub).mkdir()

        # README.md — agent-facing instructions (single source of truth).
        (tmp / "README.md").write_text(
            _render_readme(cfg), encoding="utf-8"
        )

        # must_memorize.md (always first)
        mm_src = store.paths.journal / "must_memorize.md"
        if mm_src.exists():
            shutil.copy2(mm_src, tmp / "must_memorize.md")

        # longer_memory.md
        lm_src = store.paths.journal / "longer_memory.md"
        if lm_src.exists():
            shutil.copy2(lm_src, tmp / "longer_memory.md")

        # Walk working days for layered copies.
        working = store.working_days()  # newest-first
        layer1_dates, layer2_dates, layer3_dates, layer4_dates = _slice_layers(
            working, cfg
        )

        layer1_files = _copy_layer1(store, layer1_dates, tmp)
        layer2_files = _copy_layer2(store, layer2_dates, tmp)
        layer3_files = _copy_layer3(store, layer3_dates, tmp)
        layer4_files = _copy_layer4(store, layer4_dates, tmp)

        manifest_text = _render_manifest(
            cfg=cfg,
            store=store,
            layer1=layer1_files,
            layer2=layer2_files,
            layer3=layer3_files,
            layer4=layer4_files,
            has_longer=lm_src.exists(),
            has_mm=mm_src.exists(),
        )
        (tmp / "MANIFEST.md").write_text(manifest_text, encoding="utf-8")
        (tmp / ".fingerprint").write_text(_compute_fingerprint(store),
                                          encoding="utf-8")

        # Atomic swap.
        store.atomic_swap_dir(tmp, store.paths.briefing)
    except Exception:
        if tmp.exists():  # pragma: no branch  # mkdtemp always creates dir before exception
            shutil.rmtree(tmp, ignore_errors=True)
        raise


# ----- README.md (agent-facing instructions) ------------------------------


def _render_readme(cfg: Config) -> str:
    """Substitute agent-specific values into the README template.

    Uses a SafeFormat shim so any future ``{placeholder}`` added to the
    template that isn't yet wired up here renders as a literal rather
    than crashing the rebuild.
    """
    template_path = Path(__file__).parent / "templates" / "briefing_readme.md"
    template = template_path.read_text(encoding="utf-8")
    # Compute agent slug from the same logic used by config loader.
    from .config import _slugify
    return template.format_map(_SafeFormat({
        "agent_name": cfg.agent.name,
        "agent_slug": _slugify(cfg.agent.name),
    }))


class _SafeFormat(dict):
    def __missing__(self, key):
        return "{" + key + "}"


# ----- no-op shortcut (§7.3 step 5) ----------------------------------------


def _briefing_up_to_date(store: Store) -> bool:
    """No-op shortcut: detect whether journal/ has any change since last
    briefing rebuild.

    Uses a fingerprint stored in ``briefing/.fingerprint`` — the sorted
    list of journal *.md filenames + their mtimes. Comparing this to the
    live filesystem catches both (a) new/removed files and (b) re-writes
    that bumped mtime, even when sub-second mtime precision would alias
    the comparison to "equal".
    """
    manifest = store.paths.briefing / "MANIFEST.md"
    fingerprint_path = store.paths.briefing / ".fingerprint"
    if not manifest.exists() or not fingerprint_path.exists():
        return False
    try:
        saved = fingerprint_path.read_text(encoding="utf-8")
    except OSError:
        return False
    return saved == _compute_fingerprint(store)


def _compute_fingerprint(store: Store) -> str:
    """sorted list of "<name>:<mtime>\\n" for every journal/*.md."""
    lines = []
    for f in sorted(store.paths.journal.glob("*.md")):
        try:
            lines.append(f"{f.name}:{f.stat().st_mtime_ns}")
        except OSError:
            continue
    return "\n".join(lines) + "\n"


# ----- layer slicing (§8.1) ------------------------------------------------


def _slice_layers(
    working_days: list[str], cfg: Config
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Slice the working_days list into 4 consecutive segments per config.

    Inputs are newest-first; each layer keeps that order.
    """
    w = cfg.briefing.walking
    F = w.full_shorts_working_days
    D = w.dailies_working_days
    W = w.weeklies_working_days
    M = w.monthlies_working_days
    a = 0
    b = a + F
    c = b + D
    d = c + W
    e = d + M
    return (
        working_days[a:b],
        working_days[b:c],
        working_days[c:d],
        working_days[d:e],
    )


def _date_range_from(working_days: list[str]) -> tuple[date, date] | None:
    """Min/max date represented by a list of YYYYMMDD strings."""
    if not working_days:
        return None
    dates = sorted(date(int(s[:4]), int(s[4:6]), int(s[6:8])) for s in working_days)
    return dates[0], dates[-1]


# ----- per-layer copy helpers ----------------------------------------------


def _copy_layer1(store: Store, dates: list[str], tmp: Path) -> list[Path]:
    out: list[Path] = []
    dest = tmp / "recent"
    for date_str in dates:
        for s in store.shorts_for_date(date_str):
            shutil.copy2(s, dest / s.name)
            out.append(dest / s.name)
    return out


def _copy_layer2(store: Store, dates: list[str], tmp: Path) -> list[Path]:
    out: list[Path] = []
    dest = tmp / "daily"
    for date_str in dates:
        daily = store.daily_for_date(date_str)
        if daily is not None:
            shutil.copy2(daily, dest / daily.name)
            out.append(dest / daily.name)
        else:
            # Fallback per §8.2: include shorts for that day.
            for s in store.shorts_for_date(date_str):
                shutil.copy2(s, dest / s.name)
                out.append(dest / s.name)
    return out


def _copy_layer3(store: Store, dates: list[str], tmp: Path) -> list[Path]:
    out: list[Path] = []
    dest = tmp / "weekly"
    drange = _date_range_from(dates)
    if drange is None:
        return out
    lo, hi = drange
    for f in sorted(store.paths.journal.glob("*.md")):
        m = WEEKLY_RE.match(f.name)
        if not m:
            continue
        s = m.group(1)
        monday = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        if lo <= monday <= hi:
            shutil.copy2(f, dest / f.name)
            out.append(dest / f.name)
    return out


def _copy_layer4(store: Store, dates: list[str], tmp: Path) -> list[Path]:
    out: list[Path] = []
    dest = tmp / "monthly"
    drange = _date_range_from(dates)
    if drange is None:
        return out
    lo, hi = drange
    lo_ym = lo.strftime("%Y%m")
    hi_ym = hi.strftime("%Y%m")
    for f in sorted(store.paths.journal.glob("*.md")):
        m = MONTHLY_RE.match(f.name)
        if not m:
            continue
        ym = m.group(1)
        if lo_ym <= ym <= hi_ym:
            shutil.copy2(f, dest / f.name)
            out.append(dest / f.name)
    return out


# ----- MANIFEST.md render --------------------------------------------------


def _render_manifest(
    *,
    cfg: Config,
    store: Store,
    layer1: list[Path],
    layer2: list[Path],
    layer3: list[Path],
    layer4: list[Path],
    has_longer: bool,
    has_mm: bool,
) -> str:
    """Generate the MANIFEST.md per §8.3."""
    saved = store.read_state() or {}
    last_rebuild = saved.get("last_rebuild_at") or iso_now()

    parts = [
        f"# Briefing manifest",
        "",
        f"- Agent: {cfg.agent.name}",
        f"- Last rebuild: {last_rebuild}",
        f"- Briefing files: {len(layer1) + len(layer2) + len(layer3) + len(layer4)}",
        "",
    ]
    if _is_stale(last_rebuild):
        parts.append("- ⚠ briefing stale (last rebuild > 24h ago)")
        parts.append("")

    if has_mm:
        parts.append("## Must memorize (read first)")
        parts.append("- `must_memorize.md`")
        parts.append("")
    if has_longer:
        parts.append("## Longer memory")
        parts.append("- `longer_memory.md`")
        parts.append("")
    # Order monthlies → weeklies → dailies → shorts, each oldest → newest
    if layer4:
        parts.append("## Monthly summaries")
        for f in sorted(layer4):
            parts.append(f"- `monthly/{f.name}` — {_one_line_preview(f)}")
        parts.append("")
    if layer3:
        parts.append("## Weekly summaries")
        for f in sorted(layer3):
            parts.append(f"- `weekly/{f.name}` — {_one_line_preview(f)}")
        parts.append("")
    if layer2:
        parts.append("## Daily summaries")
        for f in sorted(layer2):
            parts.append(f"- `daily/{f.name}` — {_one_line_preview(f)}")
        parts.append("")
    if layer1:
        parts.append("## Recent shorts (today + previous working days)")
        for f in sorted(layer1):
            parts.append(f"- `recent/{f.name}` — {_one_line_preview(f)}")
        parts.append("")
    parts.append(
        "**Read order**: must_memorize → longer_memory → monthlies → weeklies "
        "→ dailies → shorts. Last mention wins on factual conflict."
    )
    return "\n".join(parts) + "\n"


def _is_stale(iso_ts: str) -> bool:
    from datetime import datetime, timezone
    try:
        t = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return True
    now = datetime.now(timezone.utc)
    return (now - t).total_seconds() > 86400


def _one_line_preview(path: Path) -> str:
    """Pull the first non-empty content line from a summary file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    _, body = frontmatter.parse(text)
    for line in body.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- "):
            return s[2:].strip()[:80]
        return s[:80]
    return ""
