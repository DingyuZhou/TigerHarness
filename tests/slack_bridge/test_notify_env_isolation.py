"""Sending a Slack message must not change the process environment.

``notify.py``'s ``.env`` loader used to copy **every** key of the first
candidate file into ``os.environ`` and leave it there for the life of the
process -- despite a docstring promising it loaded only "the relevant
vars." Three consequences, all closed here:

1. an operator's unrelated secrets (other services' API keys, tokens,
   paths) were exported process-wide by the first Slack post, and then
   inherited by every child spawned with ``{**os.environ}``
   (``agent_sdk/backends/claude_p.py``, ``codex_exec.py``);
2. first file wins *forever* -- the ``key not in os.environ`` guard made a
   later load for a second team a no-op for every key the first had set,
   which on a multi-team box is a cross-team value leak with no error and
   no log line;
3. the docstring was wrong, so the next reader believed the blast radius
   was a handful of ``SLACK_*`` names.

``tests/test_env_hygiene.py`` carries the *structural* guard (no
``os.environ`` write anywhere in ``src/``). This file carries the
*behavioural* one: the real code paths, run against a real team ``.env``,
asserted not to have touched ``os.environ`` afterwards. Both are wanted --
the structural check cannot see a write that moves into a helper the scan
does not model, and the behavioural check cannot see a write on a path no
test exercises.

The scope table this file asserts over is every name any code path
legitimately needs out of a team ``.env``:
``SLACK_BOT_TOKEN``, ``SLACK_CEO_USER_ID``, ``SLACK_ALLOWED_USER_IDS``,
``ALLOWED_SLACK_USER_IDS``, ``SSL_CERT_FILE``,
``TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL`` and ``SLACK_NOTIFY_CHANNEL``.
``TIGERHARNESS_SLACK_ENV`` is deliberately absent: it *chooses* the file,
so it can never come from it.
"""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.slack_bridge import notify
from tigerharness.slack_bridge.notify import (
    SlackNotifier,
    _read_slack_bridge_dotenv,
    _resolve_env,
)
from tigerharness.slack_bridge.progress import (
    CHANNEL_ENV_VARS,
    resolve_progress_channel,
)


#: An unrelated secret of the kind an operator really does keep in a team
#: ``.env`` -- the "every key, Slack-related or not" half of the defect.
_UNRELATED = "UNRELATED_SECRET"
_UNRELATED_VALUE = "sk-some-other-service-key"

#: Every name the team ``.env`` legitimately carries for this package, plus
#: the unrelated one. Asserted as a set so a new consumer added to
#: ``notify``/``progress`` without a thought about hygiene shows up here.
_TEAM_ENV_NAMES = (
    "SLACK_BOT_TOKEN",
    "SLACK_CEO_USER_ID",
    "SLACK_ALLOWED_USER_IDS",
    "ALLOWED_SLACK_USER_IDS",
    "SSL_CERT_FILE",
    "TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL",
    "SLACK_NOTIFY_CHANNEL",
    _UNRELATED,
)


@pytest.fixture
def team_root(tmp_path, monkeypatch):
    """A team directory whose ``configs/.env`` carries the whole scope
    table plus an unrelated secret, made the cwd.

    ``configs/.env`` rather than ``.env`` on purpose: that is where
    ``tigerharness init`` writes it, so it is the file real deployments
    expose, and reaching it requires the candidate-list step that exists
    for detached personas running from a team root.

    ``TIGERHARNESS_SLACK_ENV`` is cleared so the candidate list really
    resolves through the cwd. The autouse scrub in ``tests/conftest.py``
    already removes the rest of the family, which is what makes "not in
    ``os.environ`` afterwards" a statement about the code rather than
    about the host.
    """
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / ".env").write_text(
        "SLACK_BOT_TOKEN=xoxb-from-team-dotenv\n"
        "SLACK_CEO_USER_ID=U0FROMFILE\n"
        "SLACK_ALLOWED_USER_IDS=U0ALLOWED\n"
        "ALLOWED_SLACK_USER_IDS=U0LEGACY\n"
        "SSL_CERT_FILE=/bundle/from/team/dotenv.crt\n"
        "TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL=C0PROGRESS\n"
        "SLACK_NOTIFY_CHANNEL=C0NOTIFY\n"
        f"{_UNRELATED}={_UNRELATED_VALUE}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    return tmp_path


def _unset(names=_TEAM_ENV_NAMES) -> list[str]:
    return [n for n in names if n in os.environ]


@pytest.fixture(autouse=True)
def isolated_journal_root(tmp_path, monkeypatch):
    """Autouse: the transport seam records health into the journal root.

    Same guard as ``test_notify_tls.py``'s. Without it, the end-to-end send
    below writes ``.notify_health.json`` into whatever
    ``journal/paths.default_journal_root`` resolves to -- with the
    conftest scrub in force that is ``~/.local/state/...``, i.e. the
    operator's real state dir, which would then skew ``autodrive status``.
    """
    monkeypatch.setenv("TIGERHARNESS_JOURNAL_DIR", str(tmp_path / "no-journal"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


# ---------------------------------------------------------------------------
# The precondition, asserted before anything leans on it
# ---------------------------------------------------------------------------

def test_the_scope_table_starts_absent(team_root):
    """Without this, every "not in ``os.environ``" assertion below could be
    passing because the variable was never going to be there anyway -- and
    every "== value" assertion could be reading the host's copy."""
    assert _unset() == []


def test_the_fixture_env_is_actually_reachable(team_root):
    """And the file really is found: a fixture whose ``.env`` is never read
    would make the leak assertions vacuous in the other direction."""
    parsed = _read_slack_bridge_dotenv()
    assert parsed[_UNRELATED] == _UNRELATED_VALUE
    assert set(parsed) == set(_TEAM_ENV_NAMES)


# ---------------------------------------------------------------------------
# The leak, closed at each real entry point
# ---------------------------------------------------------------------------

def test_reader_writes_nothing(team_root):
    _read_slack_bridge_dotenv()
    assert _unset() == []


def test_try_load_writes_nothing(team_root):
    """The production entry point: ``SlackNotifier.try_load`` ->
    ``_load_creds`` -> the reader. This is the call every notify makes."""
    assert SlackNotifier.try_load() is not None
    assert _unset() == []


def test_resolve_progress_channel_writes_nothing(team_root):
    """The second consumer (``progress.py:resolve_progress_channel``), which
    runs on every embedded-bridge turn -- so a leak here fires far more
    often than one in the notify path."""
    assert resolve_progress_channel() == "C0PROGRESS"
    assert _unset() == []


def test_a_full_dm_send_writes_nothing(team_root):
    """End to end, through the transport: creds, DM target, TLS context and
    the HTTP post, with only ``urlopen`` faked. A write anywhere along that
    path is what this whole task removed.

    ``urlopen.call_count`` is asserted because "nothing leaked" is
    satisfied trivially by a send that never happened."""
    resp = MagicMock()
    resp.read.return_value = json.dumps({"ok": True}).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)

    notifier = SlackNotifier.try_load()
    assert notifier is not None
    with patch(
        "tigerharness.slack_bridge.notify.urllib.request.urlopen",
        return_value=resp,
    ) as urlopen:
        assert notifier.dm_text("hello") is True
    assert urlopen.call_count == 1
    assert _unset() == []


# ---------------------------------------------------------------------------
# Same values as before, for every name in the scope table
# ---------------------------------------------------------------------------

def test_credentials_still_resolve_from_the_file(team_root):
    creds = notify._load_creds()
    assert creds is not None
    assert creds.bot_token == "xoxb-from-team-dotenv"
    # SLACK_CEO_USER_ID is the first rung of the target resolution.
    assert creds.target_user_id == "U0FROMFILE"


def test_allowlist_spellings_still_resolve_in_order(team_root, monkeypatch):
    """With the explicit override gone, the canonical allowlist name wins
    over the legacy one -- both read out of the file, both preserved."""
    parsed = dict(_read_slack_bridge_dotenv())
    del parsed["SLACK_CEO_USER_ID"]
    assert notify._resolve_target_user_id(parsed) == "U0ALLOWED"
    del parsed["SLACK_ALLOWED_USER_IDS"]
    assert notify._resolve_target_user_id(parsed) == "U0LEGACY"


def test_progress_channel_precedence_still_prefers_the_bridge_name(team_root):
    """``TIGERHARNESS_BRIDGE_PROGRESS_CHANNEL`` before
    ``SLACK_NOTIFY_CHANNEL``, resolved out of the file rather than out of
    an injected environment."""
    assert resolve_progress_channel() == "C0PROGRESS"
    parsed = dict(_read_slack_bridge_dotenv())
    del parsed[CHANNEL_ENV_VARS[0]]
    assert _resolve_env(CHANNEL_ENV_VARS[1], parsed) == "C0NOTIFY"


@pytest.mark.parametrize("name", sorted(set(_TEAM_ENV_NAMES) - {_UNRELATED}))
def test_process_env_still_beats_the_file(team_root, monkeypatch, name):
    """Acceptance criterion 3's precedence half, one case per name rather
    than one representative: the old guard was ``key not in os.environ``,
    so a real env var won for *every* key, and a resolution helper that
    got the order wrong for one name would be invisible to a single
    spot-check."""
    monkeypatch.setenv(name, "from-process-env")
    assert _resolve_env(name, _read_slack_bridge_dotenv()) == "from-process-env"


# ---------------------------------------------------------------------------
# Consequence 2: first file no longer wins forever
# ---------------------------------------------------------------------------

def test_a_second_team_is_not_shadowed_by_the_first(tmp_path, monkeypatch):
    """The cross-team leak, as a behavioural claim.

    Under the old loader, team A's ``.env`` landed in ``os.environ`` and
    every key of it made team B's load a silent no-op -- so on a
    multi-team box B posted with A's token. Reading into a dict per call
    removes the shared mutable state that made that possible.
    """
    monkeypatch.delenv("TIGERHARNESS_SLACK_ENV", raising=False)
    for team, token in (("a", "xoxb-team-a"), ("b", "xoxb-team-b")):
        root = tmp_path / team
        (root / "configs").mkdir(parents=True)
        (root / "configs" / ".env").write_text(
            f"SLACK_BOT_TOKEN={token}\nSLACK_CEO_USER_ID=U0{team.upper()}\n",
            encoding="utf-8",
        )

    monkeypatch.chdir(tmp_path / "a")
    first = notify._load_creds()
    monkeypatch.chdir(tmp_path / "b")
    second = notify._load_creds()

    assert first is not None and second is not None
    assert first.bot_token == "xoxb-team-a"
    assert second.bot_token == "xoxb-team-b"
    assert _unset() == []
