# requires: py-tgcalls yt-dlp ShazamAPI aiohttp

"""
    VoiceMod — Voice chat module for Heroku UserBot
    
    Commands:
        .vjoin / .vleave     - Join/leave voice chat
        .vplay / .vaplay     - Play video+audio / audio only
        .vqueue              - Add to queue
        .vskip / .vclear     - Skip track / clear queue
        .vshuffle / .vloop   - Shuffle queue / loop track
        .vplaylist           - Play YouTube playlist
        .vpanel              - Inline control panel
        .vpause / .vresume   - Pause/resume
        .vmute / .vunmute    - Mute/unmute
        .vstop               - Stop playback
        .sm / .shazam        - Search music / recognize track
"""

__version__ = (3, 0, 0)
# meta developer: @samuray43k
# scope: hikka_only

# =============================================================================
# HEROKU COMPATIBILITY PATCHES
# =============================================================================

import sys
import os

SPOOFED_VERSION = "1.37.0"


def _spoof_telethon_version():
    """Spoof telethon version for py-tgcalls compatibility"""
    for mod in ("telethon", "herokutl"):
        if mod in sys.modules:
            sys.modules[mod].__version__ = SPOOFED_VERSION

    class _VersionSpoofer:
        __version__ = SPOOFED_VERSION

        def __getattr__(self, name):
            import herokutl
            herokutl.__version__ = SPOOFED_VERSION
            return getattr(herokutl, name)

    if "telethon" not in sys.modules:
        sys.modules["telethon"] = _VersionSpoofer()


def _patch_pytgcalls_mtproto():
    """Create herokutl_client.py from telethon_client.py"""
    try:
        import pytgcalls
        import shutil

        mtproto_dir = os.path.dirname(pytgcalls.mtproto.__file__)
        src = os.path.join(mtproto_dir, "telethon_client.py")
        dst = os.path.join(mtproto_dir, "herokutl_client.py")

        if not os.path.exists(src):
            return

        with open(src, "r") as f:
            content = f.read()

        patched = content.replace("from telethon", "from herokutl")
        patched = patched.replace(
            "update.chat_id",
            "(update.chat_id if hasattr(update, 'chat_id') "
            "else getattr(getattr(update, 'peer', None), 'channel_id', 0))",
        )

        with open(dst, "w") as f:
            f.write(patched)

        cache = os.path.join(mtproto_dir, "__pycache__")
        if os.path.exists(cache):
            shutil.rmtree(cache, ignore_errors=True)
    except Exception:
        pass


_spoof_telethon_version()
_patch_pytgcalls_mtproto()

# =============================================================================
# IMPORTS
# =============================================================================

import logging
import asyncio
import aiohttp
import random
import re

from ShazamAPI import Shazam
from yt_dlp import YoutubeDL
from telethon import types
from telethon.tl.types import Chat, DocumentAttributeFilename

from .. import loader, utils
from ..inline.types import InlineCall

from pytgcalls import PyTgCalls
from pytgcalls import filters as call_filters
from pytgcalls.types import (
    MediaStream,
    AudioQuality,
    VideoQuality,
    StreamEnded,
    ChatUpdate,
)
from pytgcalls.types.raw import Stream

logger = logging.getLogger(__name__)

# =============================================================================
# CONSTANTS
# =============================================================================

COOKIES_URL = (
    "https://gist.githubusercontent.com/Samuray4ik04/"
    "d85f029ad63c1e6e07ecf2acd2c52eac/raw/.temp_cookies.txt"
)
COOKIES_FILE = ".voicemod_cookies.txt"

YTDL_VIDEO_OPTS = {
    "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
    "merge_output_format": "mp4",
    "outtmpl": "ytdl_%(id)s.%(ext)s",
    "quiet": True,
    "no_warnings": True,
}

YTDL_AUDIO_OPTS = {
    "format": "bestaudio[ext=m4a]/bestaudio/best",
    "outtmpl": "ytdl_%(id)s.%(ext)s",
    "quiet": True,
    "no_warnings": True,
}

YTDL_PLAYLIST_OPTS = {
    "format": "bestaudio[ext=m4a]/bestaudio/best",
    "outtmpl": "ytdl_%(id)s.%(ext)s",
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
    "ignoreerrors": True,
}

# =============================================================================
# HELPERS
# =============================================================================


class _ClientWrapper:
    """Wraps HerokutTL client to look like TelegramClient"""

    def __init__(self, client):
        self._client = client
        for attr in dir(client):
            if not attr.startswith("_"):
                try:
                    setattr(self, attr, getattr(client, attr))
                except Exception:
                    pass

    def __getattr__(self, name):
        return getattr(self._client, name)

    @property
    def __class__(self):
        class _FakeTelegramClient:
            __module__ = "telethon.client.telegramclient"
            __name__ = "TelegramClient"
        return _FakeTelegramClient


def to_call_id(chat_id: int) -> int:
    """Convert chat_id to py-tgcalls format: -100XXXXXXXXXX"""
    if chat_id is None:
        return None
    s = str(chat_id)
    if chat_id < 0 and s.startswith("-100"):
        return chat_id
    if chat_id > 1_000_000_000:
        return int(f"-100{chat_id}")
    if chat_id < 0:
        return int(f"-100{abs(chat_id)}")
    return chat_id


async def get_voice_chat(client, message) -> tuple[int | None, str | None]:
    """Validate chat supports voice calls"""
    chat_id = message.chat_id
    if not chat_id:
        return None, "no_chat"
    try:
        entity = await client.get_entity(message.peer_id)
    except Exception:
        entity = None
    if isinstance(entity, Chat):
        return None, "not_supergroup"
    return to_call_id(chat_id), None


def get_filename(message: types.Message) -> str:
    """Extract filename from message"""
    if not message or not hasattr(message, "document") or not message.document:
        return "Unknown Track"
    try:
        for attr in message.document.attributes:
            if isinstance(attr, DocumentAttributeFilename):
                return attr.file_name
            if hasattr(attr, "performer") or hasattr(attr, "title"):
                performer = getattr(attr, "performer", "") or ""
                title = getattr(attr, "title", "") or ""
                if performer and title:
                    return f"{performer} - {title}"
                return title or performer
    except Exception:
        pass
    return "Unknown Track"


# =============================================================================
# MODULE
# =============================================================================


@loader.tds
class VoiceMod(loader.Module):
    """Voice chat control with queue and inline panel"""

    strings = {
        "name": "VoiceMod",
        # Status
        "downloading": "📥 <b>Downloading...</b>",
        "playing": "🎶 <b>Playing:</b> {}",
        "playing_next": "🎶 <b>Playing:</b> {}\n➡️ <b>Next:</b> {}",
        "searching": "🔍 <b>Searching...</b>",
        # Queue
        "queue_add": "➕ <b>Added to queue:</b> {}",
        "queue_list": "📋 <b>Queue:</b>\n\n{}",
        "queue_empty": "📋 <b>Queue is empty</b>",
        "queue_cleared": "🗑 <b>Queue cleared</b>",
        "queue_shuffled": "🔀 <b>Queue shuffled</b>",
        "skipped": "⏭ <b>Skipped</b>",
        "loop_on": "🔁 <b>Loop enabled</b>",
        "loop_off": "🔁 <b>Loop disabled</b>",
        # Success
        "join": "🎙 <b>Joined!</b>",
        "leave": "👋 <b>Left!</b>",
        "stop": "⏹ <b>Stopped!</b>",
        "pause": "⏸ <b>Paused!</b>",
        "resume": "▶️ <b>Resumed!</b>",
        "mute": "🔇 <b>Muted!</b>",
        "unmute": "🔊 <b>Unmuted!</b>",
        "recognized": "🎵 <b>Shazam:</b> {}",
        # Errors
        "error": "❌ <b>Error:</b> <code>{}</code>",
        "no_audio": "❌ <b>No audio/link provided</b>",
        "no_chat": "❌ <b>Use in a group/channel with voice chat</b>",
        "not_supergroup": "❌ <b>Voice chats only work in supergroups</b>",
        "not_found": "❌ <b>Not found:</b> <code>{}</code>",
        "not_recognized": "❌ <b>Could not recognize</b>",
        "reply_audio": "❌ <b>Reply to audio</b>",
        "not_playing": "❌ <b>Nothing is playing</b>",
        # Panel buttons
        "btn_pause": "⏸ Pause",
        "btn_play": "▶️ Play",
        "btn_mute": "🔇 Mute",
        "btn_unmute": "🔊 Unmute",
        "btn_loop": "🔁 Loop",
        "btn_looping": "🔁 Looping",
        "btn_next": "⏭ Next",
        "btn_stop": "⏹ Stop",
    }

    strings_ru = {
        "downloading": "📥 <b>Загрузка...</b>",
        "playing": "🎶 <b>Играет:</b> {}",
        "playing_next": "🎶 <b>Играет:</b> {}\n➡️ <b>Далее:</b> {}",
        "searching": "🔍 <b>Поиск...</b>",
        "queue_add": "➕ <b>Добавлено в очередь:</b> {}",
        "queue_list": "📋 <b>Очередь:</b>\n\n{}",
        "queue_empty": "📋 <b>Очередь пуста</b>",
        "queue_cleared": "🗑 <b>Очередь очищена</b>",
        "queue_shuffled": "🔀 <b>Очередь перемешана</b>",
        "skipped": "⏭ <b>Пропущено</b>",
        "loop_on": "🔁 <b>Повтор включён</b>",
        "loop_off": "🔁 <b>Повтор выключен</b>",
        "join": "🎙 <b>Подключён!</b>",
        "leave": "👋 <b>Отключён!</b>",
        "stop": "⏹ <b>Остановлено!</b>",
        "pause": "⏸ <b>Пауза!</b>",
        "resume": "▶️ <b>Продолжено!</b>",
        "mute": "🔇 <b>Заглушено!</b>",
        "unmute": "🔊 <b>Звук вкл!</b>",
        "recognized": "🎵 <b>Shazam:</b> {}",
        "error": "❌ <b>Ошибка:</b> <code>{}</code>",
        "no_audio": "❌ <b>Укажи ссылку или реплай</b>",
        "no_chat": "❌ <b>Используй в группе с войсом</b>",
        "not_supergroup": "❌ <b>Войсы только в супергруппах</b>",
        "not_found": "❌ <b>Не найдено:</b> <code>{}</code>",
        "not_recognized": "❌ <b>Не распознано</b>",
        "reply_audio": "❌ <b>Реплай на аудио</b>",
        "not_playing": "❌ <b>Ничего не играет</b>",
        "btn_pause": "⏸ Пауза",
        "btn_play": "▶️ Играть",
        "btn_mute": "🔇 Мут",
        "btn_unmute": "🔊 Звук",
        "btn_loop": "🔁 Повтор",
        "btn_looping": "🔁 Повтор вкл",
        "btn_next": "⏭ Далее",
        "btn_stop": "⏹ Стоп",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "auto_panel",
                True,
                "Show inline panel when playing",
                validator=loader.validators.Boolean(),
            ),
        )
        self.call: PyTgCalls | None = None
        self._cookies_path: str | None = None
        # Per-chat state
        self._queue: dict[int, list] = {}      # chat_id -> [{file, title, audio}, ...]
        self._loop: dict[int, bool] = {}       # chat_id -> loop enabled
        self._paused: dict[int, bool] = {}     # chat_id -> is paused
        self._muted: dict[int, bool] = {}      # chat_id -> is muted
        self._panel_msg: dict[int, int] = {}   # chat_id -> panel message id

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def client_ready(self, client, db):
        self._client = client
        self._db = db
        self._restore_cookies()
        if not self._cookies_path:
            await self._fetch_cookies()

        self.call = PyTgCalls(_ClientWrapper(client))
        self._register_handlers()

        try:
            await self.call.start()
        except Exception as e:
            logger.exception(f"Failed to start PyTgCalls: {e}")

    async def on_unload(self):
        if self.call:
            try:
                await self.call.stop()
            except Exception:
                pass
        if self._cookies_path and os.path.exists(self._cookies_path):
            try:
                os.remove(self._cookies_path)
            except Exception:
                pass

    def _register_handlers(self):
        """Register py-tgcalls event handlers"""

        @self.call.on_update(call_filters.stream_end())
        async def on_stream_end(_, update: StreamEnded):
            chat_id = update.chat_id
            await self._on_track_end(chat_id)

        @self.call.on_update(call_filters.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def on_left(_, update: ChatUpdate):
            self._cleanup_chat(update.chat_id)

    # =========================================================================
    # Queue Management
    # =========================================================================

    def _cleanup_chat(self, chat_id: int):
        """Clean up all state for a chat"""
        self._queue.pop(chat_id, None)
        self._loop.pop(chat_id, None)
        self._paused.pop(chat_id, None)
        self._muted.pop(chat_id, None)
        self._panel_msg.pop(chat_id, None)

    async def _on_track_end(self, chat_id: int):
        """Handle track end - play next or loop"""
        queue = self._queue.get(chat_id, [])
        
        if not queue:
            await self._leave_and_cleanup(chat_id)
            return

        # If loop enabled, replay current
        if self._loop.get(chat_id):
            await self._play_current(chat_id)
            return

        # Remove finished track
        queue.pop(0)
        
        if not queue:
            await self._leave_and_cleanup(chat_id)
            return

        # Play next
        await self._play_current(chat_id)

    async def _leave_and_cleanup(self, chat_id: int):
        """Leave call and cleanup"""
        try:
            await self.call.leave_call(chat_id)
        except Exception:
            pass
        self._cleanup_chat(chat_id)

    async def _play_current(self, chat_id: int, message: types.Message = None):
        """Play the first track in queue"""
        queue = self._queue.get(chat_id, [])
        if not queue:
            return

        track = queue[0]
        self._paused[chat_id] = False

        try:
            if track["audio"]:
                stream = MediaStream(
                    track["file"],
                    audio_parameters=AudioQuality.HIGH,
                    video_flags=MediaStream.Flags.IGNORE,
                )
            else:
                stream = MediaStream(
                    track["file"],
                    audio_parameters=AudioQuality.HIGH,
                    video_parameters=VideoQuality.HD_720p,
                    audio_flags=MediaStream.Flags.REQUIRED,
                    video_flags=MediaStream.Flags.AUTO_DETECT,
                )

            await self.call.play(chat_id, stream)

            # Update or send panel
            if self.config["auto_panel"] and message:
                await self._send_panel(chat_id, message)

        except Exception as e:
            logger.exception(f"Failed to play: {e}")

    async def _add_to_queue(self, chat_id: int, file: str, title: str, audio: bool):
        """Add track to queue"""
        if chat_id not in self._queue:
            self._queue[chat_id] = []
        
        self._queue[chat_id].append({
            "file": file,
            "title": title,
            "audio": audio,
        })

    # =========================================================================
    # Download helpers
    # =========================================================================

    def _restore_cookies(self):
        content = self.get("cookies_content")
        if content:
            self._cookies_path = os.path.abspath(COOKIES_FILE)
            with open(self._cookies_path, "w") as f:
                f.write(content)
        else:
            self._cookies_path = None

    async def _fetch_cookies(self):
        if self.get("cookies_content"):
            return
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(COOKIES_URL) as resp:
                    if resp.status == 200:
                        content = await resp.text()
                        if content and "youtube.com" in content:
                            self._cookies_path = os.path.abspath(COOKIES_FILE)
                            with open(self._cookies_path, "w") as f:
                                f.write(content)
        except Exception:
            pass

    async def _download(self, url: str, audio_only: bool = False) -> tuple[str | None, str]:
        """Download from URL, returns (file_path, title)"""
        opts = (YTDL_AUDIO_OPTS if audio_only else YTDL_VIDEO_OPTS).copy()
        if self._cookies_path and os.path.exists(self._cookies_path):
            opts["cookiefile"] = self._cookies_path

        info = None

        def download():
            nonlocal info
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)

        await asyncio.to_thread(download)

        if not info:
            return None, "Unknown"

        title = info.get("title", "Unknown")
        video_id = info.get("id", "unknown")
        ext = "m4a" if audio_only else "mp4"
        file_path = f"ytdl_{video_id}.{ext}"

        # Try to find actual file
        for f in os.listdir("."):
            if f.startswith(f"ytdl_{video_id}"):
                file_path = f
                break

        return file_path if os.path.exists(file_path) else None, title

    async def _download_playlist(self, url: str) -> list[dict]:
        """Download playlist, returns list of {file, title}"""
        opts = YTDL_PLAYLIST_OPTS.copy()
        if self._cookies_path and os.path.exists(self._cookies_path):
            opts["cookiefile"] = self._cookies_path

        entries = []

        def download():
            nonlocal entries
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if "entries" in info:
                    entries = list(info["entries"])
                else:
                    entries = [info]

        await asyncio.to_thread(download)

        result = []
        for entry in entries:
            if not entry:
                continue
            video_id = entry.get("id", "")
            title = entry.get("title", "Unknown")
            # Find downloaded file
            for f in os.listdir("."):
                if f.startswith(f"ytdl_{video_id}"):
                    result.append({"file": f, "title": title})
                    break

        return result

    async def _get_chat(self, message: types.Message) -> int | None:
        """Get chat_id from message, validate for voice chat"""
        args = utils.get_args_raw(message)
        if not args:
            chat_id, error = await get_voice_chat(message.client, message)
            if error:
                await utils.answer(message, self.strings(error))
                return None
            return chat_id
        try:
            chat = int(args)
        except ValueError:
            chat = args
        try:
            entity = await message.client.get_entity(chat)
            return to_call_id(entity.id)
        except Exception as e:
            await utils.answer(message, self.strings("error").format(e))
            return None

    # =========================================================================
    # Inline Panel
    # =========================================================================

    def _get_panel_text(self, chat_id: int) -> str:
        """Get panel text with current track info"""
        queue = self._queue.get(chat_id, [])
        if not queue:
            return self.strings("not_playing")

        current = utils.escape_html(queue[0]["title"])
        if len(queue) > 1:
            next_track = utils.escape_html(queue[1]["title"])
            return self.strings("playing_next").format(current, next_track)
        return self.strings("playing").format(current)

    def _get_panel_markup(self, chat_id: int) -> list:
        """Get inline keyboard for panel"""
        is_paused = self._paused.get(chat_id, False)
        is_muted = self._muted.get(chat_id, False)
        is_looping = self._loop.get(chat_id, False)
        has_next = len(self._queue.get(chat_id, [])) > 1

        # Row 1: Play/Pause, Mute/Unmute, Loop
        row1 = []
        if is_paused:
            row1.append({"text": self.strings("btn_play"), "callback": self._cb_resume, "args": (chat_id,)})
        else:
            row1.append({"text": self.strings("btn_pause"), "callback": self._cb_pause, "args": (chat_id,)})

        if is_muted:
            row1.append({"text": self.strings("btn_unmute"), "callback": self._cb_unmute, "args": (chat_id,)})
        else:
            row1.append({"text": self.strings("btn_mute"), "callback": self._cb_mute, "args": (chat_id,)})

        if is_looping:
            row1.append({"text": self.strings("btn_looping"), "callback": self._cb_loop, "args": (chat_id,)})
        else:
            row1.append({"text": self.strings("btn_loop"), "callback": self._cb_loop, "args": (chat_id,)})

        # Row 2: Next (if available), Stop
        row2 = []
        if has_next:
            row2.append({"text": self.strings("btn_next"), "callback": self._cb_next, "args": (chat_id,)})
        row2.append({"text": self.strings("btn_stop"), "callback": self._cb_stop, "args": (chat_id,)})

        return [row1, row2]

    async def _send_panel(self, chat_id: int, message: types.Message):
        """Send or update inline panel"""
        text = self._get_panel_text(chat_id)
        markup = self._get_panel_markup(chat_id)
        await self.inline.form(message=message, text=text, reply_markup=markup)

    async def _update_panel(self, chat_id: int, call: InlineCall):
        """Update existing panel"""
        queue = self._queue.get(chat_id, [])
        if not queue:
            await call.edit(self.strings("stop"))
            return

        text = self._get_panel_text(chat_id)
        markup = self._get_panel_markup(chat_id)
        await call.edit(text, reply_markup=markup)

    # Panel callbacks
    async def _cb_pause(self, call: InlineCall, chat_id: int):
        try:
            await self.call.pause(chat_id)
            self._paused[chat_id] = True
            await call.answer("Paused")
            await self._update_panel(chat_id, call)
        except Exception as e:
            await call.answer(f"Error: {e}")

    async def _cb_resume(self, call: InlineCall, chat_id: int):
        try:
            await self.call.resume(chat_id)
            self._paused[chat_id] = False
            await call.answer("Resumed")
            await self._update_panel(chat_id, call)
        except Exception as e:
            await call.answer(f"Error: {e}")

    async def _cb_mute(self, call: InlineCall, chat_id: int):
        try:
            await self.call.mute(chat_id)
            self._muted[chat_id] = True
            await call.answer("Muted")
            await self._update_panel(chat_id, call)
        except Exception as e:
            await call.answer(f"Error: {e}")

    async def _cb_unmute(self, call: InlineCall, chat_id: int):
        try:
            await self.call.unmute(chat_id)
            self._muted[chat_id] = False
            await call.answer("Unmuted")
            await self._update_panel(chat_id, call)
        except Exception as e:
            await call.answer(f"Error: {e}")

    async def _cb_loop(self, call: InlineCall, chat_id: int):
        self._loop[chat_id] = not self._loop.get(chat_id, False)
        status = "Loop ON" if self._loop[chat_id] else "Loop OFF"
        await call.answer(status)
        await self._update_panel(chat_id, call)

    async def _cb_next(self, call: InlineCall, chat_id: int):
        queue = self._queue.get(chat_id, [])
        if len(queue) < 2:
            await call.answer("No next track")
            return

        self._loop[chat_id] = False
        queue.pop(0)
        await call.answer("Skipped")
        await self._play_current(chat_id)
        await self._update_panel(chat_id, call)

    async def _cb_stop(self, call: InlineCall, chat_id: int):
        await self._leave_and_cleanup(chat_id)
        await call.answer("Stopped")
        await call.edit(self.strings("stop"))

    # =========================================================================
    # Voice chat commands
    # =========================================================================

    @loader.command(ru_doc="[чат] — войти в войс-чат")
    async def vjoincmd(self, message: types.Message):
        """[chat] - Join voice chat"""
        chat = await self._get_chat(message)
        if not chat:
            return
        try:
            await self.call.play(chat, Stream())
            await utils.answer(message, self.strings("join"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(e))

    @loader.command(ru_doc="[чат] — выйти из войс-чата")
    async def vleavecmd(self, message: types.Message):
        """[chat] - Leave voice chat"""
        chat = await self._get_chat(message)
        if not chat:
            return
        try:
            await self._leave_and_cleanup(chat)
            await utils.answer(message, self.strings("leave"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(e))

    @loader.command(ru_doc="<ссылка/реплай> — воспроизвести видео+аудио")
    async def vplaycmd(self, message: types.Message):
        """<link/reply> - Play video+audio"""
        await self._play_command(message, audio_only=False)

    @loader.command(ru_doc="<ссылка/реплай> — воспроизвести только аудио")
    async def vaplaycmd(self, message: types.Message):
        """<link/reply> - Play audio only"""
        await self._play_command(message, audio_only=True)

    async def _play_command(self, message: types.Message, audio_only: bool):
        """Common play logic"""
        args = utils.get_args_raw(message)
        reply = await message.get_reply_message()

        chat, error = await get_voice_chat(message.client, message)
        if error:
            return await utils.answer(message, self.strings(error))

        has_media = reply and reply.media
        if not has_media and not args:
            return await utils.answer(message, self.strings("no_audio"))

        message = await utils.answer(message, self.strings("downloading"))

        try:
            if has_media:
                file = await reply.download_media()
                title = get_filename(reply)
            else:
                file, title = await self._download(args, audio_only=audio_only)

            if not file or not os.path.exists(file):
                return await utils.answer(message, self.strings("error").format("Download failed"))

            # Clear queue and add new track
            self._queue[chat] = []
            await self._add_to_queue(chat, file, title, audio_only)
            await self._play_current(chat, message)

        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(e))

    @loader.command(ru_doc="<ссылка/реплай> — добавить в очередь")
    async def vqueuecmd(self, message: types.Message):
        """<link/reply> - Add to queue"""
        args = utils.get_args_raw(message)
        reply = await message.get_reply_message()

        chat, error = await get_voice_chat(message.client, message)
        if error:
            return await utils.answer(message, self.strings(error))

        has_media = reply and reply.media
        if not has_media and not args:
            # Show queue
            queue = self._queue.get(chat, [])
            if not queue:
                return await utils.answer(message, self.strings("queue_empty"))
            
            lines = []
            for i, track in enumerate(queue):
                icon = "🎶" if i == 0 else "🕐"
                lines.append(f"{icon} <code>{utils.escape_html(track['title'])}</code>")
            
            return await utils.answer(message, self.strings("queue_list").format("\n".join(lines)))

        message = await utils.answer(message, self.strings("downloading"))

        try:
            if has_media:
                file = await reply.download_media()
                title = get_filename(reply)
                audio = True
            else:
                file, title = await self._download(args, audio_only=True)
                audio = True

            if not file or not os.path.exists(file):
                return await utils.answer(message, self.strings("error").format("Download failed"))

            was_empty = not self._queue.get(chat)
            await self._add_to_queue(chat, file, title, audio)

            if was_empty:
                await self._play_current(chat, message)
            else:
                await utils.answer(message, self.strings("queue_add").format(utils.escape_html(title)))

        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(e))

    @loader.command(ru_doc="— пропустить трек")
    async def vskipcmd(self, message: types.Message):
        """Skip current track"""
        chat, error = await get_voice_chat(message.client, message)
        if error:
            return await utils.answer(message, self.strings(error))

        queue = self._queue.get(chat, [])
        if not queue:
            return await utils.answer(message, self.strings("queue_empty"))

        self._loop[chat] = False
        queue.pop(0)

        if not queue:
            await self._leave_and_cleanup(chat)
            return await utils.answer(message, self.strings("stop"))

        await self._play_current(chat, message)
        await utils.answer(message, self.strings("skipped"))

    @loader.command(ru_doc="— очистить очередь")
    async def vclearcmd(self, message: types.Message):
        """Clear queue"""
        chat, error = await get_voice_chat(message.client, message)
        if error:
            return await utils.answer(message, self.strings(error))

        await self._leave_and_cleanup(chat)
        await utils.answer(message, self.strings("queue_cleared"))

    @loader.command(ru_doc="— перемешать очередь")
    async def vshufflecmd(self, message: types.Message):
        """Shuffle queue"""
        chat, error = await get_voice_chat(message.client, message)
        if error:
            return await utils.answer(message, self.strings(error))

        queue = self._queue.get(chat, [])
        if len(queue) < 2:
            return await utils.answer(message, self.strings("queue_empty"))

        # Keep first (playing), shuffle rest
        current = queue[0]
        rest = queue[1:]
        random.shuffle(rest)
        self._queue[chat] = [current] + rest

        await utils.answer(message, self.strings("queue_shuffled"))

    @loader.command(ru_doc="— вкл/выкл повтор")
    async def vloopcmd(self, message: types.Message):
        """Toggle loop"""
        chat, error = await get_voice_chat(message.client, message)
        if error:
            return await utils.answer(message, self.strings(error))

        self._loop[chat] = not self._loop.get(chat, False)
        msg = "loop_on" if self._loop[chat] else "loop_off"
        await utils.answer(message, self.strings(msg))

    @loader.command(ru_doc="<url> — воспроизвести плейлист")
    async def vplaylistcmd(self, message: types.Message):
        """<url> - Play YouTube playlist"""
        args = utils.get_args_raw(message)
        if not args:
            return await utils.answer(message, self.strings("no_audio"))

        chat, error = await get_voice_chat(message.client, message)
        if error:
            return await utils.answer(message, self.strings(error))

        message = await utils.answer(message, self.strings("downloading"))

        try:
            tracks = await self._download_playlist(args)
            if not tracks:
                return await utils.answer(message, self.strings("error").format("No tracks found"))

            self._queue[chat] = []
            for track in tracks:
                await self._add_to_queue(chat, track["file"], track["title"], audio=True)

            await self._play_current(chat, message)

        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(e))

    @loader.command(ru_doc="— панель управления")
    async def vpanelcmd(self, message: types.Message):
        """Open control panel"""
        chat, error = await get_voice_chat(message.client, message)
        if error:
            return await utils.answer(message, self.strings(error))

        if not self._queue.get(chat):
            return await utils.answer(message, self.strings("not_playing"))

        await self._send_panel(chat, message)

    # =========================================================================
    # Basic controls
    # =========================================================================

    @loader.command(ru_doc="— остановить")
    async def vstopcmd(self, message: types.Message):
        """Stop playing"""
        chat = await self._get_chat(message)
        if not chat:
            return
        await self._leave_and_cleanup(chat)
        await utils.answer(message, self.strings("stop"))

    @loader.command(ru_doc="— пауза")
    async def vpausecmd(self, message: types.Message):
        """Pause playing"""
        chat = await self._get_chat(message)
        if not chat:
            return
        try:
            await self.call.pause(chat)
            self._paused[chat] = True
            await utils.answer(message, self.strings("pause"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(e))

    @loader.command(ru_doc="— продолжить")
    async def vresumecmd(self, message: types.Message):
        """Resume playing"""
        chat = await self._get_chat(message)
        if not chat:
            return
        try:
            await self.call.resume(chat)
            self._paused[chat] = False
            await utils.answer(message, self.strings("resume"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(e))

    @loader.command(ru_doc="— заглушить")
    async def vmutecmd(self, message: types.Message):
        """Mute stream"""
        chat = await self._get_chat(message)
        if not chat:
            return
        try:
            await self.call.mute(chat)
            self._muted[chat] = True
            await utils.answer(message, self.strings("mute"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(e))

    @loader.command(ru_doc="— включить звук")
    async def vunmutecmd(self, message: types.Message):
        """Unmute stream"""
        chat = await self._get_chat(message)
        if not chat:
            return
        try:
            await self.call.unmute(chat)
            self._muted[chat] = False
            await utils.answer(message, self.strings("unmute"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(e))

    # =========================================================================
    # Music commands
    # =========================================================================

    @loader.command(ru_doc="<запрос> — поиск музыки")
    async def smcmd(self, message: types.Message):
        """<query> - Search music"""
        args = utils.get_args_raw(message)
        if not args:
            return await utils.answer(message, self.strings("no_audio"))

        reply = await message.get_reply_message()
        await utils.answer(message, self.strings("searching"))

        try:
            results = await message.client.inline_query("lybot", args)
            await message.delete()
            await message.client.send_file(
                message.chat_id,
                results[0].result.document,
                reply_to=reply.id if reply else None,
            )
        except Exception:
            await message.client.send_message(
                message.chat_id,
                self.strings("not_found").format(utils.escape_html(args)),
            )

    @loader.command(ru_doc="<реплай> — распознать трек")
    async def shazamcmd(self, message: types.Message):
        """<reply> - Recognize track with Shazam"""
        reply = await message.get_reply_message()
        if not reply or not reply.media:
            return await utils.answer(message, self.strings("reply_audio"))

        await utils.answer(message, self.strings("downloading"))

        try:
            audio = await reply.download_media(bytes)
            shazam = Shazam(audio)
            result = next(shazam.recognizeSong())

            if "track" in result[1]:
                track = result[1]["track"]
                title = track["share"]["subject"]
                image = track["images"].get("background", track["images"].get("coverart"))

                await message.client.send_file(
                    message.chat_id,
                    file=image,
                    caption=self.strings("recognized").format(utils.escape_html(title)),
                    reply_to=reply.id,
                )
                await message.delete()
            else:
                await utils.answer(message, self.strings("not_recognized"))

        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("not_recognized"))
