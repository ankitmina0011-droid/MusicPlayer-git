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
import wave
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
from core.groups import get_group, set_group, set_title
from pytgcalls.types.stream import MediaStream
from pyrogram.handlers import RawUpdateHandler
from pyrogram.raw.types import InputPeerChannel, InputChannel
from pyrogram.raw.types import UpdateGroupCall, UpdateGroupCallParticipants
from pyrogram.raw.functions.channels import GetFullChannel
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

NOTICES = {
    "not_in_group": "❌ Music account is not in this group. Add it to the group, give it admin rights with Manage video chats, then send /play again.",
    "admin_required": "❌ Music account needs admin rights with Manage video chats in this group.",
    "not_found": "❌ Song not found or could not be played. Try another name or link.",
    "video_needs_reply": "🎬 Video plays only when /vplay replies to a video message. This one plays as audio.",
}

# Silence is played while the song is still downloading. It is long enough
# that the end of it is never reached in normal use.
SILENCE_PATH = os.path.abspath("silence.wav")

# Chats where the music account is already inside a voice chat.
_IN_CALL = set()
# When the music account last joined each voice chat (see the VC notices below).
_JOINED_AT = {}
# Chats whose stream is being replaced right now (see _switch).
_SWITCHING = set()


class SongError(Exception):
    pass


class UserbotError(Exception):
    pass


def _make_silence() -> None:
    if os.path.exists(SILENCE_PATH):
        return
    try:
        with wave.open(SILENCE_PATH, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 8000 * 300)
    except Exception as e:
        print(f"[stream] could not create the silence file: {type(e).__name__}: {e}")


_make_silence()

# Join the call while the song downloads needs change_stream from py-tgcalls.
# If the installed version does not have it, the bot joins after the download.
PARALLEL_START = hasattr(pytgcalls, "change_stream") and os.path.exists(SILENCE_PATH)
print(f"[stream] start mode: {'parallel (join + download together)' if PARALLEL_START else 'sequential'}")


def forget_call(chat_id: int) -> None:
    """Call this when the music account has left a voice chat."""
    _IN_CALL.discard(chat_id)


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


def _is_error(error: Exception, *names: str) -> bool:
    code = getattr(error, "ID", "") or ""
    text = str(error)
    return any(code == name or name in text for name in names)


def _media(path: str, headers=None, video: bool = False) -> MediaStream:
    key = config.QUALITY.lower()
    if key not in _AUDIO_QUALITY:
        print("WARNING: Invalid Quality Specified. Defaulting to High!")
        key = "high"
    if video:
        return MediaStream(path, _AUDIO_QUALITY[key], _VIDEO_QUALITY[key], headers=headers)
    return MediaStream(
        path, _AUDIO_QUALITY[key], video_flags=MediaStream.Flags.IGNORE, headers=headers
    )


def get_quality(song: Song) -> MediaStream:
    # /vplay sets song.video for that one song, so /play after /vplay is audio again.
    # The /mode switch of the group still turns video on for every song.
    video = bool(getattr(song, "video", False)) or get_group(song.request_msg.chat.id)["stream_mode"] == "video"
    return _media(song.remote, song.headers, video)


async def _create_group_call(chat_id: int) -> None:
    peer = await app.resolve_peer(chat_id)
    await app.invoke(
        CreateGroupCall(
            peer=InputPeerChannel(
                channel_id=peer.channel_id,
                access_hash=peer.access_hash,
            ),
            random_id=app.rnd_id() // 9000000000,
        )
    )


_PEERS_LOADED = False


async def _load_peers() -> None:
    """The music account keeps its list of chats in memory only, so after every
    restart a group it is already in is unknown until its dialogs are read."""
    global _PEERS_LOADED
    if _PEERS_LOADED:
        return
    _PEERS_LOADED = True
    try:
        async for _ in app.get_dialogs():
            pass
        print("[userbot] dialogs read, known chats are loaded")
    except Exception as e:
        print(f"[userbot] could not read the dialogs: {type(e).__name__}: {e}")


async def _ensure_userbot_in_chat(chat_id: int) -> bool:
    """Make sure the music account is in the group. If it is not, join it with
    an invite link made by the bot (the bot must be an admin with invite rights)."""
    await _load_peers()
    try:
        await app.get_chat_member(chat_id, "me")
        return True
    except Exception:
        pass
    try:
        link = await ui.call("exportChatInviteLink", {"chat_id": chat_id})
        await app.join_chat(link)
        print(f"[userbot] joined chat {chat_id} with an invite link")
        return True
    except Exception as e:
        if _is_error(e, "USER_ALREADY_PARTICIPANT"):
            return True
        print(f"[userbot] could not join chat {chat_id}: {type(e).__name__}: {e}")
        return False


async def _leave_quietly(chat_id: int) -> None:
    _IN_CALL.discard(chat_id)
    try:
        await pytgcalls.leave_call(chat_id)
    except Exception:
        pass


async def _play(chat, stream: MediaStream) -> None:
    """Join the voice chat and start `stream`. Handles the errors this bot has
    shown: no call running yet, the music account not in the group, and an
    old call that was not left."""
    fixed = False
    for _ in range(5):
        try:
            try:
                await pytgcalls.play(chat.id, stream)
                _IN_CALL.add(chat.id)
                _JOINED_AT[chat.id] = time.time()
                return
            except NoActiveGroupCall:
                await _create_group_call(chat.id)
        except Exception as e:
            if type(e).__name__ == "AlreadyJoinedError":
                await _leave_quietly(chat.id)
            elif _is_error(
                e, "CHANNEL_PRIVATE", "CHANNEL_INVALID", "USER_NOT_PARTICIPANT",
                "PEER_ID_INVALID", "ID not found",
            ):
                if fixed or not await _ensure_userbot_in_chat(chat.id):
                    raise UserbotError("not_in_group") from e
                fixed = True
            elif _is_error(e, "CHAT_ADMIN_REQUIRED"):
                raise UserbotError("admin_required") from e
            else:
                raise
    raise UserbotError("not_in_group")


async def _switch(chat, song: Song) -> None:
    """Change the stream of a call we are already in."""
    _SWITCHING.add(chat.id)
    try:
        await pytgcalls.change_stream(chat.id, get_quality(song))
        _IN_CALL.add(chat.id)
    except Exception as e:
        print(f"[stream] change_stream failed ({type(e).__name__}: {e}), joining again")
        await _leave_quietly(chat.id)
        await _play(chat, get_quality(song))
    finally:
        _SWITCHING.discard(chat.id)
        # The end of the stream that was replaced can arrive a moment later.
        get_group(chat.id)["stream_started_at"] = time.time()


async def _set_media(chat, song: Song) -> None:
    if PARALLEL_START and chat.id in _IN_CALL:
        await _switch(chat, song)
    else:
        await _play(chat, get_quality(song))


async def _notify(chat_id: int, text: str) -> None:
    try:
        result = await ui.call(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
    except Exception as e:
        print(f"[stream] could not send the notice: {type(e).__name__}: {e}")
        return
    await asyncio.sleep(30)
    try:
        await ui.call(
            "deleteMessage", {"chat_id": chat_id, "message_id": result["message_id"]}
        )
    except Exception:
        pass


async def _busy_message(song: Song, lang):
    try:
        return await song.request_msg.reply_text(lang["downloading"])
    except Exception:
        pass
    try:
        result = await ui.call(
            "sendMessage",
            {"chat_id": song.request_msg.chat.id, "text": "Processing..."},
        )
        return ui.SentMessage(song.request_msg.chat.id, result["message_id"])
    except Exception:
        return None


async def start_stream(song: Song, lang, infomsg=None, pending=None):
    """Start `song` in its voice chat.

    `pending` is an asyncio task that finishes song.parse(). When it is given
    (or the song is not parsed yet) the voice chat join and the download run
    at the same time, so the join time is the main delay, not both together.
    """
    chat = song.request_msg.chat
    if infomsg is None:
        infomsg = await _busy_message(song, lang)
    t0 = time.time()
    if not song.parsed and pending is None:
        pending = asyncio.create_task(song.parse())
    try:
        if song.parsed:
            await _set_media(chat, song)
        elif PARALLEL_START and chat.id not in _IN_CALL:
            await _play(chat, _media(SILENCE_PATH))
            print(f"[stream] joined the voice chat at {time.time() - t0:.1f}s")
            ok, status = await pending
            print(f"[stream] song ready at {time.time() - t0:.1f}s")
            if not ok:
                raise SongError(status)
            await _switch(chat, song)
        else:
            ok, status = await pending
            print(f"[stream] song ready at {time.time() - t0:.1f}s")
            if not ok:
                raise SongError(status)
            await _set_media(chat, song)
    except (UserbotError, SongError) as e:
        if pending is not None and not pending.done():
            pending.cancel()
        await _leave_quietly(chat.id)
        set_group(chat.id, is_playing=False, now_playing=None)
        key = "not_found" if isinstance(e, SongError) else str(e)
        _spawn(_notify(chat.id, NOTICES.get(key, NOTICES["not_found"])))
        await _delete_quietly(infomsg)
        return
    except Exception:
        # An unexpected error must not leave the chat stuck in "playing".
        if pending is not None and not pending.done():
            pending.cancel()
        set_group(chat.id, is_playing=False, now_playing=None)
        await _leave_quietly(chat.id)
        await _delete_quietly(infomsg)
        raise
    get_group(chat.id)["stream_started_at"] = time.time()
    print(f"[stream] playing at {time.time() - t0:.1f}s in chat {chat.id}")
    # The song is already playing. The cover and the card are made in the
    # background, so nothing here delays the song.
    _spawn(_announce(song, lang, chat, infomsg, t0))


async def _set_title_quietly(chat_id: int, title: str) -> None:
    try:
        await set_title(chat_id, title, client=app)
    except BaseException as e:
        print(f"[stream] could not set the voice chat title: {type(e).__name__}: {e}")


async def _make_cover(song: Song, chat, title: str):
    for thumb in (song.thumb or DEFAULT_THUMB, DEFAULT_THUMB):
        try:
            return await generate_cover(title, chat.title, chat.id, thumb)
        except Exception as e:
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
    if song.requested_by:
        who = song.requested_by.mention
    else:
        sender = song.request_msg.sender_chat
        who = sender.title if sender else ""
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
        except Exception as e:
            print(f"[stream] styled card failed ({type(e).__name__}: {e}), sending the plain one")
            try:
                message = await _plain_card(song, lang, cover)
            except Exception as e2:
                print(f"[stream] could not send the now-playing message: {type(e2).__name__}: {e2}")
        if message is not None:
            safone[chat.id] = message
            print(f"[stream] now-playing card sent at {time.time() - t0:.1f}s")
    finally:
        await _delete_quietly(infomsg)
        if cover and os.path.exists(cover):
            os.remove(cover)


# --- Voice chat join and leave notices ---------------------------------------
# Telegram tells the music account when someone joins or leaves the voice chat
# it is in. The bot then posts "VC JOIN" or "VC LEFT" with the person's name,
# user ID and username, and deletes that message after VC_NOTICE_SECONDS.
VC_NOTICE_SECONDS = 5
# Right after the music account joins, Telegram lists the people already inside.
# Those are remembered but not announced.
VC_SNAPSHOT_SECONDS = 4
_CALL_CHATS = {}  # group call id -> chat id
_VC_STATE = {}  # (chat id, user id) -> ("JOIN" or "LEFT", time it was seen)


def _marked_chat_id(chat_id: int) -> int:
    """Some updates carry a bare channel id. The bot uses the -100 form."""
    return chat_id if chat_id < 0 else int("-100" + str(chat_id))


async def _chat_of_call(call_id: int):
    if call_id in _CALL_CHATS:
        return _CALL_CHATS[call_id]
    # The call is not known yet: ask each chat where the music account is in a call.
    for chat_id in list(_IN_CALL):
        try:
            peer = await app.resolve_peer(chat_id)
            full = await app.invoke(
                GetFullChannel(
                    channel=InputChannel(
                        channel_id=peer.channel_id, access_hash=peer.access_hash
                    )
                )
            )
        except Exception:
            continue
        call = getattr(full.full_chat, "call", None)
        if call is not None:
            _CALL_CHATS[call.id] = chat_id
            if call.id == call_id:
                return chat_id
    return None


async def _user_details(user_id: int, users: dict):
    user = users.get(user_id)
    if user is None:
        try:
            user = await app.get_users(user_id)
        except Exception:
            user = None
    parts = [getattr(user, "first_name", None) or "", getattr(user, "last_name", None) or ""]
    name = " ".join(part for part in parts if part) or "Unknown"
    return name, getattr(user, "username", None) or ""


async def _vc_notice(chat_id: int, status: str, user_id: int, name: str, username: str) -> None:
    heading = "🟢 VC JOIN" if status == "JOIN" else "🔴 VC LEFT"
    handle = "@" + escape(username) if username else "None"
    text = (
        f"<b>{heading}</b>\n"
        f"Name: {escape(name)}\n"
        f"User ID: <code>{user_id}</code>\n"
        f"Username: {handle}"
    )
    try:
        result = await ui.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "disable_notification": True,
            },
        )
    except Exception as e:
        print(f"[vc] could not send the notice: {type(e).__name__}: {e}")
        return
    await asyncio.sleep(VC_NOTICE_SECONDS)
    try:
        await ui.call(
            "deleteMessage", {"chat_id": chat_id, "message_id": result["message_id"]}
        )
    except Exception:
        pass


async def _vc_participants(update, users: dict) -> None:
    try:
        chat_id = await _chat_of_call(update.call.id)
        if chat_id is None:
            return
        users = users or {}
        me = getattr(app, "me", None)
        my_id = me.id if me else None
        for participant in update.participants:
            user_id = getattr(getattr(participant, "peer", None), "user_id", None)
            if not user_id or user_id == my_id:
                continue
            key = (chat_id, user_id)
            before, when = _VC_STATE.get(key, ("", 0.0))
            now = time.time()
            if now - _JOINED_AT.get(chat_id, 0.0) < VC_SNAPSHOT_SECONDS:
                _VC_STATE[key] = ("LEFT" if getattr(participant, "left", False) else "JOIN", now)
                continue
            if getattr(participant, "left", False):
                if before == "LEFT":
                    continue
                status = "LEFT"
            elif getattr(participant, "just_joined", False) or before == "LEFT":
                if before == "JOIN" and now - when < 10:
                    continue  # the same join, sent again
                status = "JOIN"
            else:
                _VC_STATE[key] = ("JOIN", now)  # already inside, nothing new
                continue
            _VC_STATE[key] = (status, now)
            name, username = await _user_details(user_id, users)
            _spawn(_vc_notice(chat_id, status, user_id, name, username))
    except Exception as e:
        print(f"[vc] could not handle a participant update: {type(e).__name__}: {e}")


async def _on_raw_update(client, update, users, chats) -> None:
    if isinstance(update, UpdateGroupCall):
        call_id = getattr(getattr(update, "call", None), "id", None)
        chat_id = getattr(update, "chat_id", None)
        if call_id and chat_id:
            _CALL_CHATS[call_id] = _marked_chat_id(chat_id)
    elif isinstance(update, UpdateGroupCallParticipants):
        _spawn(_vc_participants(update, users))


# Group 10 so this handler does not take updates away from py-tgcalls.
app.add_handler(RawUpdateHandler(_on_raw_update), group=10)
