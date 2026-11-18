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

import os
import time
import shutil
import asyncio
import tempfile
from html import escape
from config import config
from core import ui
from core.song import Song, DEFAULT_THUMB
from pyrogram import Client
from yt_dlp import YoutubeDL
from pytgcalls import PyTgCalls
from core.funcs import generate_cover
from core.groups import get_group, set_title
from pytgcalls.types.stream import MediaStream
from pyrogram.raw.types import InputPeerChannel
from pytgcalls.types import AudioQuality, VideoQuality
from pyrogram.raw.functions.phone import CreateGroupCall
from pytgcalls.exceptions import NoActiveGroupCall


def _ensure_ffmpeg() -> None:
    """The voice chat library plays audio through the ffmpeg program.
    If the server has none, the call joins but stays silent, so check it
    and, when missing, use the copy bundled in the imageio-ffmpeg package."""
    found = shutil.which("ffmpeg")
    if found:
        print(f"[ffmpeg] found: {found}")
        return
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        bin_dir = os.path.join(tempfile.gettempdir(), "ffmpeg-bin")
        os.makedirs(bin_dir, exist_ok=True)
        link = os.path.join(bin_dir, "ffmpeg")
        if not os.path.exists(link):
            try:
                os.symlink(exe, link)
            except OSError:
                shutil.copy(exe, link)
                os.chmod(link, 0o755)
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
        print(f"[ffmpeg] not installed, using bundled copy: {exe}")
    except BaseException as e:
        print(
            f"[ffmpeg] NOT found and no bundled copy ({type(e).__name__}: {e}). "
            "Songs will stay silent!"
        )


_ensure_ffmpeg()


safone = {}
_background_tasks = set()
ydl_opts = {
    "quiet": True,
    "geo_bypass": True,
    "nocheckcertificate": True,
}
app = Client(
    "MusicPlayerUB",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    session_string=config.SESSION,
    in_memory=True,
)
ytdl = YoutubeDL(ydl_opts)
pytgcalls = PyTgCalls(app)

_AUDIO_QUALITY = {
    "high": AudioQuality.HIGH,
    "medium": AudioQuality.MEDIUM,
    "low": AudioQuality.LOW,
}
_VIDEO_QUALITY = {
    "high": VideoQuality.FHD_1080p,
    "medium": VideoQuality.HD_720p,
    "low": VideoQuality.SD_480p,
}


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _delete_quietly(message) -> None:
    if message is None:
        return
    try:
        await message.delete()
    except BaseException:
        pass


async def start_stream(song: Song, lang, infomsg=None):
    chat = song.request_msg.chat
    await _delete_quietly(safone.pop(chat.id, None))
    if infomsg is None:
        infomsg = await song.request_msg.reply_text(lang["downloading"])
    _t0 = time.time()
    print(f"[stream] starting {song.raw_title or song.title!r} in chat {chat.id}")
    try:
        await pytgcalls.play(chat.id, get_quality(song))
    except NoActiveGroupCall:
        peer = await app.resolve_peer(chat.id)
        await app.invoke(
            CreateGroupCall(
                peer=InputPeerChannel(
                    channel_id=peer.channel_id,
                    access_hash=peer.access_hash,
                ),
                random_id=app.rnd_id() // 9000000000,
            )
        )
        return await start_stream(song, lang, infomsg)
    except Exception:
        await _delete_quietly(infomsg)
        raise
    print(f"[stream] play() finished in {time.time() - _t0:.1f}s")
    # The song is already playing. The cover and the card are built in the
    # background, so nothing here delays the voice chat join.
    _spawn(_announce(song, lang, chat, infomsg, _t0))


async def _set_title_quietly(chat_id: int, title: str) -> None:
    try:
        await set_title(chat_id, title, client=app)
    except BaseException as e:
        print(f"[stream] could not set the voice chat title: {type(e).__name__}: {e}")


async def _make_cover(song: Song, chat, title: str):
    for thumb in (song.thumb or DEFAULT_THUMB, DEFAULT_THUMB):
        try:
            return await generate_cover(title, chat.title, chat.id, thumb)
        except BaseException as e:
            print(f"[stream] cover failed: {type(e).__name__}: {e}")
    return None


def _requester_html(song: Song) -> str:
    if song.requested_by:
        return ui.user_link(song.requested_by)
    sender = getattr(song.request_msg, "sender_chat", None)
    if sender:
        return escape(sender.title or "")
    return ""


async def _plain_card(song: Song, lang, cover):
    """Plain Pyrogram message, used when the styled Bot API card fails."""
    who = song.requested_by.mention if song.requested_by else (
        song.request_msg.sender_chat.title if song.request_msg.sender_chat else ""
    )
    text = lang["playing"] % (
        song.title or "Unknown",
        song.source,
        song.duration,
        song.request_msg.chat.id,
        who,
    )
    if cover:
        return await song.request_msg.reply_photo(photo=cover, caption=text, quote=False)
    return await song.request_msg.reply_text(text, quote=False)


async def _announce(song: Song, lang, chat, infomsg, t0: float) -> None:
    title = song.raw_title or song.title or "Unknown"
    _spawn(_set_title_quietly(chat.id, title))
    cover = None
    try:
        cover = await _make_cover(song, chat, title)
        print(f"[stream] cover ready at {time.time() - t0:.1f}s")
        message = None
        try:
            caption = ui.now_playing_caption(
                title=title,
                page_url=getattr(song, "page_url", None) or song.source,
                duration=song.duration,
                requester_html=_requester_html(song),
                chat_title=chat.title or "",
            )
            message = await ui.send_now_playing(chat.id, None, caption, cover)
        except BaseException as e:
            print(f"[stream] styled card failed ({type(e).__name__}: {e}), sending the plain one")
            try:
                message = await _plain_card(song, lang, cover)
            except BaseException as e2:
                print(f"[stream] could not send the now-playing message: {type(e2).__name__}: {e2}")
        if message is not None:
            safone[chat.id] = message
            print(f"[stream] now-playing card sent at {time.time() - t0:.1f}s")
    finally:
        await _delete_quietly(infomsg)
        if cover and os.path.exists(cover):
            os.remove(cover)


def get_quality(song: Song) -> MediaStream:
    group = get_group(song.request_msg.chat.id)
    key = config.QUALITY.lower()
    if key not in _AUDIO_QUALITY:
        print("WARNING: Invalid Quality Specified. Defaulting to High!")
        key = "high"
    if group["stream_mode"] == "video":
        return MediaStream(
            song.remote,
            _AUDIO_QUALITY[key],
            _VIDEO_QUALITY[key],
            headers=song.headers,
        )
    return MediaStream(
        song.remote,
        _AUDIO_QUALITY[key],
        video_flags=MediaStream.Flags.IGNORE,
        headers=song.headers,
    )
