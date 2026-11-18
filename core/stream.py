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
import sys
import time
import shutil
import asyncio
import tempfile
from shlex import quote
from subprocess import PIPE
from typing import Optional, Tuple
from config import config
from core.song import Song
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


async def _probe(song: Song) -> Tuple[bool, str]:
    """Let ffmpeg read 3 seconds of the stream and report what went wrong."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if song.headers:
        cmd += ["-headers", "".join(f"{k}: {v}\r\n" for k, v in song.headers.items())]
    cmd += ["-i", song.remote, "-t", "3", "-f", "null", "-"]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=25)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "timed out after 25 seconds"
    except BaseException as e:
        return False, f"{type(e).__name__}: {e}"
    return proc.returncode == 0, err.decode(errors="ignore").strip()[-500:]


async def _download_local(song: Song) -> Optional[str]:
    """Download the song to the server and return the file path."""
    os.makedirs("downloads", exist_ok=True)
    # Remove copies older than an hour so the disk does not fill up.
    now = time.time()
    for name in os.listdir("downloads"):
        old = os.path.join("downloads", name)
        try:
            if now - os.path.getmtime(old) > 3600:
                os.remove(old)
        except OSError:
            pass
    template = os.path.join("downloads", f"{time.time_ns()}.%(ext)s")
    url = getattr(song, "page_url", None) or song.source
    process = await asyncio.create_subprocess_shell(
        f"{quote(sys.executable)} -m yt_dlp --no-warnings --no-playlist -f best "
        f"-o {quote(template)} --print after_move:filepath {quote(url)}",
        stdout=PIPE,
        stderr=PIPE,
    )
    out, err = await process.communicate()
    lines = [x.strip() for x in out.decode(errors="ignore").splitlines() if x.strip()]
    path = lines[-1] if lines else None
    if path and os.path.exists(path):
        return path
    print(f"[download] failed:\n{err.decode(errors='ignore')[-500:]}")
    return None


async def start_stream(song: Song, lang):
    chat = song.request_msg.chat
    if safone.get(chat.id) is not None:
        try:
            await safone[chat.id].delete()
        except BaseException:
            pass
    infomsg = await song.request_msg.reply_text(lang["downloading"])
    _t0 = time.time()
    print(f"[stream] starting {song.title!r} in chat {chat.id}")
    # HLS (m3u8) streams, like SoundCloud's, start very slowly when read live.
    # Download the song first and play the local file instead.
    if "m3u8" in (getattr(song, "protocol", "") or "") and not os.path.exists(
        song.remote
    ):
        _d0 = time.time()
        local = await _download_local(song)
        if local:
            print(f"[download] saved in {time.time() - _d0:.1f}s: {local}")
            song.remote = local
            song.headers = None
        else:
            print("[download] failed, playing the live stream instead")
    try:
        await pytgcalls.play(
            chat.id,
            get_quality(song),
        )
    except (NoActiveGroupCall):
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
        return await start_stream(song, lang)
    print(f"[stream] play() finished, joining the call took {time.time() - _t0:.1f}s")
    await set_title(chat.id, song.title, client=app)
    thumb = await generate_cover(
        song.title,
        chat.title,
        chat.id,
        song.thumb,
    )
    print(f"[stream] cover image ready at {time.time() - _t0:.1f}s")
    safone[chat.id] = await song.request_msg.reply_photo(
        photo=thumb,
        caption=lang["playing"]
        % (
            song.title,
            song.source,
            song.duration,
            song.request_msg.chat.id,
            (
                song.requested_by.mention
                if song.requested_by
                else song.request_msg.sender_chat.title
            ),
        ),
        quote=False,
    )
    print(f"[stream] all done in {time.time() - _t0:.1f}s")
    await infomsg.delete()
    if os.path.exists(thumb):
        os.remove(thumb)


def get_quality(song: Song) -> MediaStream:
    group = get_group(song.request_msg.chat.id)
    if group["stream_mode"] == "video":
        if config.QUALITY.lower() == "high":
            return MediaStream(
                song.remote,
                AudioQuality.HIGH,
                VideoQuality.FHD_1080p,
                headers=song.headers,
            )
        elif config.QUALITY.lower() == "medium":
            return MediaStream(
                song.remote,
                AudioQuality.MEDIUM,
                VideoQuality.HD_720p,
                headers=song.headers,
            )
        elif config.QUALITY.lower() == "low":
            return MediaStream(
                song.remote,
                AudioQuality.LOW,
                VideoQuality.SD_480p,
                headers=song.headers,
            )
        else:
            print("WARNING: Invalid Quality Specified. Defaulting to High!")
            return MediaStream(
                song.remote,
                AudioQuality.HIGH,
                VideoQuality.FHD_1080p,
                headers=song.headers,
            )
    else:
        if config.QUALITY.lower() == "high":
            return MediaStream(
                song.remote,
                AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE,
                headers=song.headers,
            )
        elif config.QUALITY.lower() == "medium":
            return MediaStream(
                song.remote,
                AudioQuality.MEDIUM,
                video_flags=MediaStream.Flags.IGNORE,
                headers=song.headers,
            )
        elif config.QUALITY.lower() == "low":
            return MediaStream(
                song.remote,
                AudioQuality.LOW,
                video_flags=MediaStream.Flags.IGNORE,
                headers=song.headers,
            )
        else:
            print("WARNING: Invalid Quality Specified. Defaulting to High!")
            return MediaStream(
                song.remote,
                AudioQuality.HIGH,
                video_flags=MediaStream.Flags.IGNORE,
                headers=song.headers,
            )
