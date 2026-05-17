"""Slack file attachment downloader + prompt augmenter.

When a user uploads a file in a Slack DM, the Slack event carries a
``files: [...]`` array with each file's ``url_private_download`` URL.
This module:

1. Fetches each file via HTTPS with the bot token.
2. Writes it to a configurable staging dir (default: /tmp/slack-attachments/).
3. Builds a metadata block appended to the user's text so the agent's
   ``Read`` tool can look at the file by path.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import aiohttp


log = logging.getLogger("tigerharness.slack_bridge.downloader")


def _default_attachment_root() -> Path:
    """Configurable via TIGERHARNESS_ATTACHMENT_DIR env var."""
    override = os.environ.get("TIGERHARNESS_ATTACHMENT_DIR", "").strip()
    if override:
        return Path(override)
    return Path("/tmp/slack-attachments")


@dataclass(frozen=True)
class Attachment:
    """A successfully-staged file from a Slack message."""

    file_id: str
    name: str  # original Slack filename (display only)
    mimetype: str
    size: int
    path: Path  # where we wrote it on disk


@runtime_checkable
class FileDownloader(Protocol):
    """Inject this into the bridge for testability."""

    async def download(self, file_obj: dict[str, Any], thread_ts: str) -> Attachment | None:
        ...


class SlackFileDownloader:
    """Real downloader -- `aiohttp` + bot token."""

    def __init__(
        self,
        bot_token: str,
        *,
        root: Path | None = None,
        timeout_s: int = 60,
    ) -> None:
        self._token = bot_token
        self._root = root or _default_attachment_root()
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def download(
        self, file_obj: dict[str, Any], thread_ts: str
    ) -> Attachment | None:
        url = file_obj.get("url_private_download") or file_obj.get("url_private")
        file_id = file_obj.get("id")
        if not url or not file_id:
            log.warning(
                "skipping attachment with no url/id: keys=%s",
                sorted(file_obj.keys()),
            )
            return None

        ext = _pick_ext(file_obj)
        dest_dir = self._root / thread_ts
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{file_id}{ext}"

        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            async with aiohttp.ClientSession(timeout=self._timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    resp.raise_for_status()
                    content = await resp.read()
        except Exception as exc:
            log.exception(
                "slack file download failed: file_id=%s name=%s err=%r",
                file_id, file_obj.get("name", "?"), exc,
            )
            return None

        dest.write_bytes(content)
        size = file_obj.get("size") or len(content)
        log.info(
            "downloaded slack file: id=%s name=%s -> %s (%d bytes)",
            file_id, file_obj.get("name", "?"), dest, len(content),
        )
        return Attachment(
            file_id=file_id,
            name=file_obj.get("name", "") or "",
            mimetype=file_obj.get("mimetype", "") or "application/octet-stream",
            size=size,
            path=dest,
        )


# ---------------------------------------------------------------------------
# Prompt augmentation
# ---------------------------------------------------------------------------

def augment_prompt(text: str, attachments: list[Attachment]) -> str:
    """Build the final user message, appending file metadata if any."""
    if not attachments:
        return text

    lines = [_describe(a) for a in attachments]
    block = (
        "Attached files (paths on disk -- use Read to view):\n"
        + "\n".join(lines)
    )
    if text:
        return f"{text}\n\n{block}"
    return f"(Files sent with no caption.)\n\n{block}"


def _describe(a: Attachment) -> str:
    size = _human_size(a.size)
    name_hint = f", original name: {a.name}" if a.name else ""
    return f"- {a.path}  ({a.mimetype}, {size}{name_hint})"


def _human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    f = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        f /= 1024
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}"
    return f"{f:.1f} TB"


# ---------------------------------------------------------------------------
# Extension picker
# ---------------------------------------------------------------------------

def _pick_ext(file_obj: dict[str, Any]) -> str:
    """Choose `.ext` for the on-disk filename."""
    ft = (file_obj.get("filetype") or "").strip().lower()
    if ft:
        return f".{ft}"
    name = file_obj.get("name") or ""
    if "." in name:
        ext = name.rsplit(".", 1)[-1].strip().lower()
        if ext:
            return f".{ext}"
    return ""
