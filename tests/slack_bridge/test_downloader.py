"""Downloader tests: extension picking, prompt augmentation, human_size, async download."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tigerharness.slack_bridge.downloader import (
    Attachment,
    SlackFileDownloader,
    _default_attachment_root,
    _human_size,
    _pick_ext,
    augment_prompt,
)


def test_pick_ext_from_filetype():
    assert _pick_ext({"filetype": "jpg"}) == ".jpg"
    assert _pick_ext({"filetype": "PDF"}) == ".pdf"


def test_pick_ext_from_name():
    assert _pick_ext({"name": "report.pdf"}) == ".pdf"
    assert _pick_ext({"name": "data.CSV"}) == ".csv"


def test_pick_ext_empty():
    assert _pick_ext({}) == ""
    assert _pick_ext({"name": "noext"}) == ""


def test_human_size():
    assert _human_size(500) == "500 B"
    assert _human_size(1024) == "1.0 KB"
    assert _human_size(1024 * 1024) == "1.0 MB"
    assert _human_size(1536 * 1024) == "1.5 MB"


def test_augment_prompt_no_attachments():
    assert augment_prompt("hello", []) == "hello"


def test_augment_prompt_with_text_and_files():
    att = Attachment(
        file_id="F123",
        name="chart.png",
        mimetype="image/png",
        size=2048,
        path=Path("/tmp/slack-attachments/ts/F123.png"),
    )
    result = augment_prompt("Look at this", [att])
    assert "Look at this" in result
    assert "Attached files" in result
    assert "/tmp/slack-attachments/ts/F123.png" in result
    assert "image/png" in result
    assert "chart.png" in result


def test_augment_prompt_no_text():
    att = Attachment(
        file_id="F456",
        name="doc.pdf",
        mimetype="application/pdf",
        size=1000000,
        path=Path("/tmp/slack-attachments/ts/F456.pdf"),
    )
    result = augment_prompt("", [att])
    assert "no caption" in result.lower()
    assert "F456.pdf" in result


def test_default_attachment_root_env(monkeypatch):
    monkeypatch.setenv("TIGERHARNESS_ATTACHMENT_DIR", "/custom/dir")
    assert _default_attachment_root() == Path("/custom/dir")


def test_default_attachment_root_default(monkeypatch):
    monkeypatch.delenv("TIGERHARNESS_ATTACHMENT_DIR", raising=False)
    assert _default_attachment_root() == Path("/tmp/slack-attachments")


class TestSlackFileDownloader:
    @pytest.mark.asyncio
    async def test_download_success(self, tmp_path):
        downloader = SlackFileDownloader("xoxb-test", root=tmp_path)
        file_obj = {
            "id": "F123",
            "name": "report.pdf",
            "filetype": "pdf",
            "mimetype": "application/pdf",
            "size": 1024,
            "url_private_download": "https://files.slack.com/download/F123",
        }

        # Mock aiohttp session
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.read = AsyncMock(return_value=b"PDF_CONTENT")
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tigerharness.slack_bridge.downloader.aiohttp.ClientSession", return_value=mock_session):
            result = await downloader.download(file_obj, "thread-ts-1")

        assert result is not None
        assert result.file_id == "F123"
        assert result.name == "report.pdf"
        assert result.path.exists()
        assert result.path.read_bytes() == b"PDF_CONTENT"

    @pytest.mark.asyncio
    async def test_download_no_url(self, tmp_path):
        downloader = SlackFileDownloader("xoxb-test", root=tmp_path)
        file_obj = {"id": "F123", "name": "test.txt"}  # no url_private_download
        result = await downloader.download(file_obj, "ts")
        assert result is None

    @pytest.mark.asyncio
    async def test_download_no_id(self, tmp_path):
        downloader = SlackFileDownloader("xoxb-test", root=tmp_path)
        file_obj = {"url_private_download": "https://x"}  # no id
        result = await downloader.download(file_obj, "ts")
        assert result is None

    @pytest.mark.asyncio
    async def test_download_network_error(self, tmp_path):
        downloader = SlackFileDownloader("xoxb-test", root=tmp_path)
        file_obj = {
            "id": "F999",
            "name": "fail.txt",
            "url_private_download": "https://x",
        }

        mock_session = AsyncMock()
        mock_session.get = MagicMock(side_effect=Exception("network error"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("tigerharness.slack_bridge.downloader.aiohttp.ClientSession", return_value=mock_session):
            result = await downloader.download(file_obj, "ts")

        assert result is None
