"""Regression tests for notify.py's explicit TLS trust store.

The incident: every autodrive heartbeat, drive-status update and journal
notify since a reboot failed TLS verification, silently, because
``notify.py`` posted on the interpreter's default SSL context and this
host's OpenSSL ``capath`` holds no hashed symlinks.

**No test here opens a socket, and no test imports the real ``certifi``.**
A test that reaches ``slack.com`` passes or fails on the runner's trust
store, which is the thing under test. And ``certifi`` is *not* a declared
dependency of this package (measured: it reaches this box only via
``claude-agent-sdk -> mcp -> httpx -> certifi``, so a plain
``tigerharness[slack]`` install has none) -- a module-scope ``import
certifi`` here would make the whole file uncollectable on exactly the
install profile that runs the notifier. Rung 2 is therefore driven by a
stub module injected into ``sys.modules``, which is also what the
production code imports through.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import sys
import types
import urllib.error
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from tigerharness.slack_bridge import notify
from tigerharness.slack_bridge.notify import (
    SlackNotifier,
    _put_bytes,
    _slack_post_form,
    _slack_post_json,
    _ssl_context,
)


#: Two real, public root CAs, embedded rather than read off the host.
#: Loading a bundle does not check validity dates, so these are inert
#: test data; what matters is that they are *parseable* and *distinct*,
#: which lets a cert count alone say which rung was loaded.
_CERT_A = """\
-----BEGIN CERTIFICATE-----
MIIBtjCCAVugAwIBAgITBmyf1XSXNmY/Owua2eiedgPySjAKBggqhkjOPQQDAjA5
MQswCQYDVQQGEwJVUzEPMA0GA1UEChMGQW1hem9uMRkwFwYDVQQDExBBbWF6b24g
Um9vdCBDQSAzMB4XDTE1MDUyNjAwMDAwMFoXDTQwMDUyNjAwMDAwMFowOTELMAkG
A1UEBhMCVVMxDzANBgNVBAoTBkFtYXpvbjEZMBcGA1UEAxMQQW1hem9uIFJvb3Qg
Q0EgMzBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IABCmXp8ZBf8ANm+gBG1bG8lKl
ui2yEujSLtf6ycXYqm0fc4E7O5hrOXwzpcVOho6AF2hiRVd9RFgdszflZwjrZt6j
QjBAMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgGGMB0GA1UdDgQWBBSr
ttvXBp43rDCGB5Fwx5zEGbF4wDAKBggqhkjOPQQDAgNJADBGAiEA4IWSoxe3jfkr
BqWTrBqYaGFy+uGh0PsceGCmQ5nFuMQCIQCcAu/xlJyzlvnrxir4tiz+OpAUFteM
YyRIHN8wfdVoOw==
-----END CERTIFICATE-----
"""

_CERT_B = """\
-----BEGIN CERTIFICATE-----
MIIB3DCCAYOgAwIBAgINAgPlfvU/k/2lCSGypjAKBggqhkjOPQQDAjBQMSQwIgYD
VQQLExtHbG9iYWxTaWduIEVDQyBSb290IENBIC0gUjQxEzARBgNVBAoTCkdsb2Jh
bFNpZ24xEzARBgNVBAMTCkdsb2JhbFNpZ24wHhcNMTIxMTEzMDAwMDAwWhcNMzgw
MTE5MDMxNDA3WjBQMSQwIgYDVQQLExtHbG9iYWxTaWduIEVDQyBSb290IENBIC0g
UjQxEzARBgNVBAoTCkdsb2JhbFNpZ24xEzARBgNVBAMTCkdsb2JhbFNpZ24wWTAT
BgcqhkjOPQIBBggqhkjOPQMBBwNCAAS4xnnTj2wlDp8uORkcA6SumuU5BwkWymOx
uYb4ilfBV85C+nOh92VC/x7BALJucw7/xyHlGKSq2XE/qNS5zowdo0IwQDAOBgNV
HQ8BAf8EBAMCAYYwDwYDVR0TAQH/BAUwAwEB/zAdBgNVHQ4EFgQUVLB7rUW44kB/
+wpu+74zyTyjhNUwCgYIKoZIzj0EAwIDRwAwRAIgIk90crlgr/HmnKAWBVBfw147
bmF0774BxL4YSFlhgjICICadVGNA3jdgUM/I2O2dgq43mLyjj0xMqTQrbO/7lZsm
-----END CERTIFICATE-----
"""

#: The verify-path / loader overrides these tests write directly. Each is
#: restored by hand rather than by ``monkeypatch`` -- see :func:`_restored`.
_ENV_KEYS = ("SSL_CERT_FILE", "SSL_CERT_DIR", "TIGERHARNESS_SLACK_ENV")

_NOTIFY_LOGGER = "tigerharness.slack_bridge.notify"


@contextmanager
def _restored(*names: str):
    """Restore each name's PRIOR state on exit -- **including absence**.

    Not ``monkeypatch``, and not an unconditional ``del``, for one measured
    reason each. ``monkeypatch.delenv(name, raising=False)`` on an *absent*
    key records no undo entry, so a key the code under test writes into
    ``os.environ`` afterwards (which is exactly what
    ``_load_slack_bridge_dotenv`` does) survives into every later test.
    An unconditional ``del`` is itself the leak on a host where
    ``SSL_CERT_FILE`` was exported before pytest started -- and a host that
    has been through this incident is precisely that host.
    """
    prior = {name: os.environ.get(name) for name in names}
    try:
        yield
    finally:
        for name, value in prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """WARNING+ messages from notify's own logger only.

    ``caplog.text`` would also carry any other logger that happens to warn
    during the test, so an emptiness assertion over it proves less than it
    appears to.
    """
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _NOTIFY_LOGGER and r.levelno >= logging.WARNING
    ]


@pytest.fixture(autouse=True)
def isolated_journal_root(tmp_path, monkeypatch):
    """Autouse: the transport seam records health into the journal root.

    Without this, every test that lets a real ``_record_transport_result``
    run would write ``.notify_health.json`` into the operator's live
    journal and skew ``autodrive status``. Pointing the override at an
    absent tmp dir also exercises the writer's missing-root no-op.
    """
    monkeypatch.setenv("TIGERHARNESS_JOURNAL_DIR", str(tmp_path / "no-journal"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


@pytest.fixture
def broken_host(tmp_path):
    """Reproduce the incident host in-process, and neutralize the ``.env``
    loader so the code under test cannot repair it mid-test.

    ``SSL_CERT_DIR`` is the lever: monkeypatching
    ``ssl.get_default_verify_paths`` would change nothing, because
    ``create_default_context()`` reaches OpenSSL's
    ``set_default_verify_paths()``, which reads the environment *below* the
    Python function -- an inert fixture that displaces nothing.

    What it does NOT displace: the interpreter's compiled-in default
    ``cafile``. Leaving ``SSL_CERT_FILE`` unset -- required, since the rung
    tests read that variable as their input -- is precisely what makes
    OpenSSL fall back to it, so on a host that ships a CA bundle a default
    context built under this fixture still trusts it. Assert on the store
    only with the file rung displaced as well; see
    :func:`test_fixture_reproduces_the_broken_host`.

    ``TIGERHARNESS_SLACK_ENV`` points at an **empty file that exists**: the
    loader's candidate loop returns after the first candidate that exists,
    so this short-circuits ``cwd/.env``, ``cwd/configs/.env`` and the
    package ``.env`` in one line. A non-existent path would NOT neutralize
    -- the candidate is skipped and the loop falls through to ``cwd/.env``.
    """
    empty_capath = tmp_path / "emptycapath"
    empty_capath.mkdir()
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    with _restored(*_ENV_KEYS):
        os.environ.pop("SSL_CERT_FILE", None)
        os.environ["SSL_CERT_DIR"] = str(empty_capath)
        os.environ["TIGERHARNESS_SLACK_ENV"] = str(empty_env)
        yield empty_capath


@pytest.fixture
def install_certifi(monkeypatch):
    """Install a stub ``certifi`` for the duration of one test.

    ``notify._ssl_context`` imports ``certifi`` *inside* the function, so
    ``sys.modules`` is the seam. Passing ``None`` installs the sentinel that
    makes ``import certifi`` raise ``ImportError`` -- which is what makes
    the guarded-import branch reachable, and so keeps the 100% branch floor
    without a pragma.

    ``monkeypatch.setitem`` is correct here where ``delenv`` was not: it
    records "was absent" properly and deletes the key on undo, so the stub
    cannot leak into a later test.
    """
    def _install(where: str | None):
        if where is None:
            monkeypatch.setitem(sys.modules, "certifi", None)
            return None
        module = types.ModuleType("certifi")
        module.where = lambda: where  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "certifi", module)
        return where

    return _install


@pytest.fixture
def certifi_bundle(tmp_path, install_certifi):
    """A working rung 2: a stub ``certifi`` whose bundle holds TWO certs.

    Two, against :func:`one_cert_bundle`'s one, so ``x509`` alone
    distinguishes the rungs even if the spy were deleted.
    """
    bundle = tmp_path / "certifi-cacert.pem"
    bundle.write_text(_CERT_A + _CERT_B, encoding="utf-8")
    return install_certifi(str(bundle))


@pytest.fixture
def one_cert_bundle(tmp_path):
    """A valid CA bundle holding exactly ONE certificate -- the rung-1 store."""
    bundle = tmp_path / "one-cert.pem"
    bundle.write_text(_CERT_A, encoding="utf-8")
    return bundle


@pytest.fixture
def cafile_spy(monkeypatch):
    """Record the ``cafile`` handed to every ``create_default_context`` call
    while still building the real context, so a test can assert BOTH which
    rung was selected and what the resulting store actually holds."""
    calls: list[str | None] = []
    real = ssl.create_default_context

    def _spy(*args, **kwargs):
        calls.append(kwargs.get("cafile"))
        return real(*args, **kwargs)

    monkeypatch.setattr(notify.ssl, "create_default_context", _spy)
    return calls


# ---------------------------------------------------------------------------
# The fixtures themselves, asserted before anything relies on them
# ---------------------------------------------------------------------------

def test_fixture_reproduces_the_broken_host(broken_host, tmp_path):
    """The fixture's contract, asserted so it holds on ANY interpreter.

    This used to end on a bare
    ``ssl.create_default_context().cert_store_stats()["x509"] == 0``. That
    line measured the **host**, not the fixture, for two independently
    verified reasons:

    1. ``SSL_CERT_DIR`` feeds OpenSSL's hash-dir lookup, which is consulted
       lazily during verification and never loaded into the store up front.
       A correctly hashed, *populated* capath also reports ``x509 == 0``, so
       that count never measured the capath override at all.
    2. What it did measure is the compiled-in default **cafile** -- and
       ``SSL_CERT_FILE`` being ABSENT, which this fixture guarantees, is
       exactly the condition under which OpenSSL falls back to it. A
       uv-managed interpreter looks for ``/etc/ssl/cert.pem``, finds nothing
       on the incident box, and trusts zero certs; a distro interpreter
       resolves the system bundle and the very same line reads in the
       hundreds. No environment variable removes that fallback.

    So: assert the fixture's own contract, then assert the incident's
    mechanic with the file rung displaced too -- an unloadable
    ``SSL_CERT_FILE`` set **here and nowhere else**, because every rung test
    below needs it genuinely absent.
    """
    assert ssl.get_default_verify_paths().capath == str(broken_host)
    assert not any(broken_host.iterdir())
    assert "SSL_CERT_FILE" not in os.environ

    with _restored("SSL_CERT_FILE"):
        # Present-but-unloadable, not absent: presence is what suppresses
        # the built-in fallback, so this is the only spelling that
        # reproduces "no trust anchors" on a host that ships a CA bundle.
        os.environ["SSL_CERT_FILE"] = str(tmp_path / "no-such-bundle.pem")
        assert ssl.create_default_context().cert_store_stats()["x509"] == 0


def test_embedded_bundles_are_parseable_and_distinct(one_cert_bundle, certifi_bundle):
    """The cert counts every rung assertion below leans on.

    If a paste ever mangles ``_CERT_A``/``_CERT_B``, this fails here rather
    than turning a rung assertion into a confusing false negative.
    """
    assert ssl.create_default_context(
        cafile=str(one_cert_bundle)
    ).cert_store_stats()["x509"] == 1
    assert ssl.create_default_context(
        cafile=certifi_bundle
    ).cert_store_stats()["x509"] == 2


def test_fixture_survives_a_real_dotenv_and_try_load(broken_host, tmp_path, monkeypatch):
    """The negative control is dismantled by the code under test unless the
    loader is neutralized -- ``_load_slack_bridge_dotenv`` sets keys *not
    already present*, and "SSL_CERT_FILE unset" is exactly that condition.

    The hazard is constructed here rather than depended on: cwd is a team
    directory whose ``configs/.env`` carries ``SSL_CERT_FILE``. Without the
    neutralizer this test's last assertion fails; with it, the fixture
    holds through a real ``try_load()``.
    """
    team = tmp_path / "team"
    (team / "configs").mkdir(parents=True)
    (team / "configs" / ".env").write_text(
        "SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt\n"
        "SLACK_BOT_TOKEN=xoxb-from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(team)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CEO_USER_ID", "U0CEO")

    assert "SSL_CERT_FILE" not in os.environ
    assert SlackNotifier.try_load() is not None
    assert "SSL_CERT_FILE" not in os.environ


def test_dotenv_loader_would_restore_it_without_the_neutralizer(tmp_path, monkeypatch):
    """Proves the neutralizer is load-bearing rather than decorative.

    Same team ``.env`` as above, but with ``TIGERHARNESS_SLACK_ENV`` unset:
    one real loader call puts ``SSL_CERT_FILE`` straight back. If this ever
    goes green, the fixture above has stopped protecting anything.
    """
    team = tmp_path / "team"
    (team / "configs").mkdir(parents=True)
    (team / "configs" / ".env").write_text(
        "SSL_CERT_FILE=/some/bundle/from/dotenv.crt\n", encoding="utf-8"
    )
    monkeypatch.chdir(team)
    with _restored(*_ENV_KEYS):
        os.environ.pop("SSL_CERT_FILE", None)
        os.environ.pop("TIGERHARNESS_SLACK_ENV", None)
        notify._load_slack_bridge_dotenv()
        assert os.environ.get("SSL_CERT_FILE") == "/some/bundle/from/dotenv.crt"


# ---------------------------------------------------------------------------
# The resolution order, one claim per rung
# ---------------------------------------------------------------------------

def test_rung1_ssl_cert_file_wins_over_an_available_certifi(
    broken_host, cafile_spy, one_cert_bundle, certifi_bundle
):
    """Rung 1 beats rung 2 -- asserted with rung 2 *available*, so this is a
    precedence claim and not merely "the only candidate was chosen"."""
    with _restored("SSL_CERT_FILE"):
        os.environ["SSL_CERT_FILE"] = str(one_cert_bundle)
        ctx = _ssl_context()
    assert cafile_spy == [str(one_cert_bundle)]
    # One cert, not certifi's two: rung 1 really was the store that loaded.
    assert ctx.cert_store_stats()["x509"] == 1


def test_rung2_certifi_when_env_unset(broken_host, cafile_spy, certifi_bundle):
    ctx = _ssl_context()
    assert cafile_spy == [certifi_bundle]
    assert ctx.cert_store_stats()["x509"] == 2


def test_rung3_neither_selects_default_and_warns(
    broken_host, cafile_spy, install_certifi, caplog
):
    """Deliberately does NOT assert on ``x509``: zero trusted certs at rung 3
    under this fixture is the EXPECTED degraded state, not a bug. Asserting
    ``> 0`` here would be false, and weakening every rung to ``>= 0`` would
    turn the strongest assertion in this file into the weakest.
    """
    install_certifi(None)
    with caplog.at_level(logging.DEBUG, logger=_NOTIFY_LOGGER):
        _ssl_context()
    assert cafile_spy == [None]
    assert any("default TLS trust store" in m for m in _warnings(caplog))
    # The certifi-absent diagnostic is emitted, and at DEBUG.
    assert any(
        r.levelno == logging.DEBUG and "certifi not installed" in r.getMessage()
        for r in caplog.records
        if r.name == _NOTIFY_LOGGER
    )


def test_missing_ssl_cert_file_falls_through_and_logs(
    broken_host, cafile_spy, certifi_bundle, tmp_path, caplog
):
    """A path that does not exist raises ``FileNotFoundError``."""
    missing = tmp_path / "not-there.pem"
    with _restored("SSL_CERT_FILE"):
        os.environ["SSL_CERT_FILE"] = str(missing)
        with caplog.at_level(logging.WARNING, logger=_NOTIFY_LOGGER):
            ctx = _ssl_context()
    assert cafile_spy == [str(missing), certifi_bundle]
    assert any("unusable" in m and str(missing) in m for m in _warnings(caplog))
    assert ctx.cert_store_stats()["x509"] == 2


def test_non_bundle_ssl_cert_file_falls_through(
    broken_host, cafile_spy, certifi_bundle, tmp_path, caplog
):
    """The other half of the single ``except OSError``: a file that exists
    but is not a CA bundle raises ``ssl.SSLError``, an OSError subclass, so
    one handler covers both stories."""
    junk = tmp_path / "junk.pem"
    junk.write_text("this is not a certificate\n", encoding="utf-8")
    with _restored("SSL_CERT_FILE"):
        os.environ["SSL_CERT_FILE"] = str(junk)
        with caplog.at_level(logging.WARNING, logger=_NOTIFY_LOGGER):
            ctx = _ssl_context()
    assert cafile_spy == [str(junk), certifi_bundle]
    assert any("unusable" in m and str(junk) in m for m in _warnings(caplog))
    assert ctx.cert_store_stats()["x509"] == 2


def test_every_rung_failing_still_returns_a_context(
    broken_host, cafile_spy, install_certifi, tmp_path, caplog
):
    """All candidates exhausted -> the last rung. A bad ``SSL_CERT_FILE``
    must never take down every notification; it degrades, loudly."""
    missing = tmp_path / "not-there.pem"
    install_certifi(str(tmp_path / "also-missing.pem"))
    with _restored("SSL_CERT_FILE"):
        os.environ["SSL_CERT_FILE"] = str(missing)
        with caplog.at_level(logging.WARNING, logger=_NOTIFY_LOGGER):
            ctx = _ssl_context()
    assert cafile_spy == [str(missing), str(tmp_path / "also-missing.pem"), None]
    assert isinstance(ctx, ssl.SSLContext)
    assert any("default TLS trust store" in m for m in _warnings(caplog))


def test_blank_ssl_cert_file_is_not_a_candidate(
    broken_host, cafile_spy, certifi_bundle
):
    """``SSL_CERT_FILE=""`` (or whitespace) is how a ``.env`` spells "unset".
    Treating it as a candidate would spend a WARNING on every single post."""
    with _restored("SSL_CERT_FILE"):
        os.environ["SSL_CERT_FILE"] = "   "
        _ssl_context()
    assert cafile_spy == [certifi_bundle]


def test_certifi_absent_logs_at_debug_not_warning(
    broken_host, cafile_spy, one_cert_bundle, install_certifi, caplog
):
    """A working rung 1 must not emit a WARNING just because certifi is not
    installed -- noise on a correctly configured host trains people to
    ignore the log, which is how this incident stayed invisible."""
    install_certifi(None)
    with _restored("SSL_CERT_FILE"):
        os.environ["SSL_CERT_FILE"] = str(one_cert_bundle)
        with caplog.at_level(logging.DEBUG, logger=_NOTIFY_LOGGER):
            _ssl_context()
    assert cafile_spy == [str(one_cert_bundle)]
    assert _warnings(caplog) == []


# ---------------------------------------------------------------------------
# Every call site is handed the context -- both halves
# ---------------------------------------------------------------------------

def _json_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_slack_post_json_passes_the_context(broken_host):
    sentinel = ssl.create_default_context()
    with patch.object(notify, "_ssl_context", return_value=sentinel):
        with patch(
            "tigerharness.slack_bridge.notify.urllib.request.urlopen",
            return_value=_json_response({"ok": True}),
        ) as urlopen:
            assert _slack_post_json("chat.postMessage", "xoxb-t", {"text": "hi"})["ok"]
    # Both halves: the call happened AND it carried the expected context.
    # "no unverified request is made" is satisfied trivially by a component
    # that made no request at all.
    assert urlopen.call_count == 1
    assert urlopen.call_args.kwargs["context"] is sentinel


def test_slack_post_form_passes_the_context(broken_host):
    sentinel = ssl.create_default_context()
    with patch.object(notify, "_ssl_context", return_value=sentinel):
        with patch(
            "tigerharness.slack_bridge.notify.urllib.request.urlopen",
            return_value=_json_response({"ok": True}),
        ) as urlopen:
            assert _slack_post_form("conversations.open", "xoxb-t", {"users": "U0"})["ok"]
    assert urlopen.call_count == 1
    assert urlopen.call_args.kwargs["context"] is sentinel


def test_put_bytes_passes_the_context(broken_host):
    sentinel = ssl.create_default_context()
    resp = MagicMock()
    resp.status = 200
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    with patch.object(notify, "_ssl_context", return_value=sentinel):
        with patch(
            "tigerharness.slack_bridge.notify.urllib.request.urlopen",
            return_value=resp,
        ) as urlopen:
            assert _put_bytes("https://upload.url", b"data") is True
    assert urlopen.call_count == 1
    assert urlopen.call_args.kwargs["context"] is sentinel


def test_resolve_dm_channel_is_covered_transitively(broken_host):
    """``_resolve_dm_channel`` contains no ``urlopen`` of its own -- it
    delegates to ``_slack_post_form``. Patching only the transport proves
    the delegation, so there is no fourth call site to patch."""
    sentinel = ssl.create_default_context()
    with patch.object(notify, "_ssl_context", return_value=sentinel):
        with patch(
            "tigerharness.slack_bridge.notify.urllib.request.urlopen",
            return_value=_json_response({"ok": True, "channel": {"id": "D0C"}}),
        ) as urlopen:
            assert notify._resolve_dm_channel("xoxb-t", "U0CEO") == "D0C"
    assert urlopen.call_args.kwargs["context"] is sentinel


# ---------------------------------------------------------------------------
# The transport seam
# ---------------------------------------------------------------------------

def test_json_decode_error_is_not_swallowed_by_the_seam(broken_host):
    """The success record sits in an ``else:`` clause, not at the ``return``
    inside the ``with``. A body that is not JSON still propagates, and
    ``ok=True`` is recorded because the transport genuinely completed."""
    resp = MagicMock()
    resp.read.return_value = b"<html>not json</html>"
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    with patch("tigerharness.slack_bridge.notify.urllib.request.urlopen", return_value=resp):
        with patch.object(notify, "_record_transport_result") as record:
            with pytest.raises(json.JSONDecodeError):
                _slack_post_json("chat.postMessage", "xoxb-t", {"text": "hi"})
    assert record.call_args_list[-1].args[0] is True


@pytest.mark.parametrize(
    "call",
    [
        lambda: _slack_post_json("chat.postMessage", "xoxb-t", {"text": "hi"}),
        lambda: _slack_post_form("conversations.open", "xoxb-t", {"users": "U0"}),
        lambda: _put_bytes("https://upload.url", b"data"),
    ],
    ids=["post_json", "post_form", "put_bytes"],
)
def test_url_error_from_a_real_call_site_reaches_the_seam(broken_host, call):
    """Asserting the seam function exists is not enough -- a real
    ``URLError`` out of each of the three transports must arrive at it."""
    with patch(
        "tigerharness.slack_bridge.notify.urllib.request.urlopen",
        side_effect=urllib.error.URLError("handshake failed"),
    ):
        with patch.object(notify, "_record_transport_result") as record:
            call()
    assert record.call_count == 1
    assert record.call_args.args[0] is False
    assert isinstance(record.call_args.args[1], urllib.error.URLError)
    assert record.call_args.kwargs["site"]


def test_seam_logs_the_site_and_never_raises(caplog):
    with caplog.at_level(logging.WARNING, logger=_NOTIFY_LOGGER):
        assert notify._record_transport_result(
            False, urllib.error.URLError("boom"), site="POST chat.postMessage"
        ) is None
    assert any(
        "POST chat.postMessage" in m and "transport failed" in m
        for m in _warnings(caplog)
    )
