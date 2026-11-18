"""
Music Player, Telegram Voice Chat Bot
Copyright (c) 2021-present Asm Safone <https://github.com/AsmSafone>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>
"""

import re
import sys
import json
import asyncio
from shlex import quote
from subprocess import PIPE
from datetime import timedelta
from urllib.parse import urlparse
from aiohttp import ClientSession
from pyrogram.types import User, Message
from typing import Dict, List, Tuple, Union, Optional

DEFAULT_THUMB = "https://telegra.ph/file/820cac7cb7b1a025542e2.jpg"


class Song:
    def __init__(self, link: Union[str, dict], request_msg: Message) -> None:
        if isinstance(link, str):
            self.title: str = None
            self.duration: str = None
            self.thumb: str = None
            self.remote: str = None
            self.source: str = link
            self.headers: dict = None
            self.request_msg: Message = request_msg
            self.requested_by: User = request_msg.from_user
            self.parsed: bool = False
            self._retries: int = 0
        elif isinstance(link, dict):
            self.parsed: bool = True
            self._retries: int = 0
            self.duration: str = "N/A"
            self.headers: dict = None
            self.thumb: str = DEFAULT_THUMB
            for key, value in link.items():
                setattr(self, key, value)
            self.request_msg: Message = request_msg
            self.requested_by: User = request_msg.from_user

    async def parse(self) -> Tuple[bool, str]:
        if self.parsed:
            return (True, "ALREADY_PARSED")

        source = self.source
        is_search_text = "://" not in source and not source.startswith(
            ("ytsearch", "scsearch")
        )

        # 1) Try the source itself (YouTube, SoundCloud, direct link, ...).
        video = None
        if not is_search_text:
            video = await self._extract(source)
            if video and await self._playable(video):
                await self._fill(video)
                return (True, "PARSED")

        # 2) The source did not work (for example YouTube answers 403 on this
        #    host). Fall back to SoundCloud, searching by the song name.
        if "soundcloud.com" not in source:
            query = source if is_search_text else (video or {}).get("title")
            if not query:
                query = await self._guess_title()
            if query:
                query = self._clean_query(query)
                print(f"[fallback] trying SoundCloud for {query!r}")
                # Some results are DRM protected or blocked, so try the first
                # few matches instead of only the first one.
                for sc_video in await self._extract_many(f"scsearch5:{query}"):
                    if await self._playable(sc_video):
                        await self._fill(sc_video)
                        return (True, "PARSED")

        self._retries += 1
        return (False, "MAX_RETRY_LIMIT_REACHED")

    async def _guess_title(self) -> Optional[str]:
        """Find a song name when YouTube refused to give any details.

        1) The words the user typed after the command (/play gata only).
        2) For a pasted YouTube link, the title from YouTube's oEmbed page,
           which normally does not ask for a sign-in.
        """
        try:
            parts = (self.request_msg.text or "").split(maxsplit=1)
            if len(parts) > 1 and "://" not in parts[1]:
                return parts[1].strip()
        except BaseException:
            pass
        if "youtube.com" in self.source or "youtu.be" in self.source:
            session = ClientSession()
            try:
                response = await session.get(
                    "https://www.youtube.com/oembed",
                    params={"url": self.source, "format": "json"},
                    timeout=10,
                )
                if response.status == 200:
                    data = await response.json(content_type=None)
                    title = data.get("title")
                    if title:
                        return title
                print(f"[oembed] status {response.status}")
            except BaseException as e:
                print(f"[oembed] failed: {type(e).__name__}: {e}")
            finally:
                await session.close()
        return None

    @staticmethod
    def _clean_query(text: str) -> str:
        """Remove (Official Video), [4K Remaster] and similar from a title."""
        cleaned = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or text

    async def _extract_many(self, target: str) -> List[dict]:
        # sys.executable -m yt_dlp uses the same Python environment as the bot,
        # so it works even if the yt-dlp command is not in PATH.
        # -i keeps going when one result fails (for example DRM protected).
        process = await asyncio.create_subprocess_shell(
            f"{quote(sys.executable)} -m yt_dlp -i --no-warnings --print-json "
            f"--skip-download -f best {quote(target)}",
            stdout=PIPE,
            stderr=PIPE,
        )
        out, err = await process.communicate()
        results = []
        for line in out.decode(errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        if not results:
            print(
                f"[yt-dlp] failed for {target!r}:\n{err.decode(errors='ignore')[-800:]}"
            )
        return results

    async def _extract(self, target: str) -> Optional[dict]:
        results = await self._extract_many(target)
        return results[0] if results else None

    async def _playable(self, video: dict) -> bool:
        url = video.get("url")
        if not url:
            return False
        return await self.check_remote_url(url, video.get("http_headers"))

    async def _fill(self, video: dict) -> None:
        headers = video.get("http_headers")
        thumb = video.get("thumbnail")
        if not (thumb and await self.check_remote_url(thumb, headers)):
            thumb = DEFAULT_THUMB
        self.title = self._escape(video.get("title") or "Unknown")
        self.duration = str(timedelta(seconds=int(video.get("duration") or 0)))
        self.thumb = thumb
        self.remote = video["url"]
        self.headers = headers
        self.parsed = True

    @staticmethod
    async def check_remote_url(
        path: str, headers: Optional[Dict[str, str]] = None
    ) -> bool:
        host = urlparse(path).netloc
        session = ClientSession()
        try:
            response = await session.get(path, timeout=15, headers=headers)
            response.close()
            if response.status in (200, 206):
                return True
            print(f"[check] {host} answered with status {response.status}")
            return False
        except BaseException as e:
            print(f"[check] request to {host} failed: {type(e).__name__}: {e}")
            return False
        finally:
            await session.close()

    @staticmethod
    def _escape(_title: str) -> str:
        title = _title
        f = ["**", "__", "`", "~~", "--"]
        for i in f:
            title = title.replace(i, f"\\{i}")
        return title

    def to_dict(self) -> Dict[str, str]:
        return {"title": self.title, "source": self.source}
