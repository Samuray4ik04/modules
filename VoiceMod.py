# requires: py-tgcalls yt-dlp ShazamAPI aiohttp

__version__ = (2, 1, 0)
# meta developer: @samuray43k, @ai
# scope: hikka_only

import os
import logging
import asyncio
import aiohttp

from ShazamAPI import Shazam
from yt_dlp import YoutubeDL
from telethon import types

from .. import loader, utils

logger = logging.getLogger(__name__)

# Default cookies URL (secret gist)
DEFAULT_COOKIES_URL = "https://gist.githubusercontent.com/Samuray4ik04/d85f029ad63c1e6e07ecf2acd2c52eac/raw/.temp_cookies.txt"


def _patch_pytgcalls_for_heroku():
    try:
        import pytgcalls
        mtproto_path = os.path.dirname(pytgcalls.mtproto.__file__)
        herokutl_client = os.path.join(mtproto_path, "herokutl_client.py")
        telethon_client = os.path.join(mtproto_path, "telethon_client.py")
        
        if not os.path.exists(telethon_client):
            return
        
        with open(telethon_client, "r") as f:
            content = f.read()
        
        patched = content.replace("from telethon", "from herokutl")
        patched = patched.replace(
            "update.chat_id",
            "(update.chat_id if hasattr(update, 'chat_id') else getattr(getattr(update, 'peer', None), 'channel_id', 0))"
        )
        
        with open(herokutl_client, "w") as f:
            f.write(patched)
        
        cache_dir = os.path.join(mtproto_path, "__pycache__")
        if os.path.exists(cache_dir):
            import shutil
            shutil.rmtree(cache_dir, ignore_errors=True)
            
    except Exception as e:
        logger.warning(f"Heroku patch failed: {e}")


_patch_pytgcalls_for_heroku()

from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality, StreamEnded, ChatUpdate
from pytgcalls.types.raw import Stream


class _ClientWrapper:
    def __init__(self, client):
        self._client = client
        for attr in dir(client):
            if not attr.startswith('_'):
                try:
                    setattr(self, attr, getattr(client, attr))
                except Exception:
                    pass
    
    def __getattr__(self, name):
        return getattr(self._client, name)
    
    @property
    def __class__(self):
        class FakeTelethonClient:
            __module__ = "telethon.client.telegramclient"
            __name__ = "TelegramClient"
        return FakeTelethonClient


def _get_full_chat_id(chat_id: int) -> int:
    """Convert chat_id to py-tgcalls format (-100XXXXXXXXXX)"""
    if chat_id is None:
        return None
    # Already in correct format
    if chat_id < 0 and str(chat_id).startswith("-100"):
        return chat_id
    # Positive large ID (channel/supergroup without prefix)
    if chat_id > 0 and chat_id > 1000000000:
        return int(f"-100{chat_id}")
    # Negative but not -100 format (old style group)
    if chat_id < 0 and not str(chat_id).startswith("-100"):
        # Convert -XXXX to -100XXXX
        return int(f"-100{abs(chat_id)}")
    return chat_id


async def _get_chat_for_call(client, message) -> tuple:
    """Get chat_id and check if voice chat is supported. Returns (chat_id, error_msg)"""
    from telethon.tl.types import Chat, Channel
    
    chat_id = message.chat_id
    if not chat_id:
        return None, "no_chat"
    
    try:
        entity = await client.get_entity(message.peer_id)
    except Exception:
        entity = None
    
    # Check if it's a basic group (not supergroup)
    if isinstance(entity, Chat):
        # Basic groups need to be converted to supergroup for voice chats
        return None, "not_supergroup"
    
    return _get_full_chat_id(chat_id), None


@loader.tds
class VoiceMod(loader.Module):
    strings = {
        "name": "VoiceMod",
        "downloading": "<b>[VoiceMod]</b> Downloading...",
        "playing": "<b>[VoiceMod]</b> Playing...",
        "plsjoin": "<b>[VoiceMod]</b> Not in voice chat. Use .vjoin first",
        "stop": "<b>[VoiceMod]</b> Stopped!",
        "join": "<b>[VoiceMod]</b> Joined!",
        "leave": "<b>[VoiceMod]</b> Left!",
        "pause": "<b>[VoiceMod]</b> Paused!",
        "resume": "<b>[VoiceMod]</b> Resumed!",
        "mute": "<b>[VoiceMod]</b> Muted!",
        "unmute": "<b>[VoiceMod]</b> Unmuted!",
        "error": "<b>[VoiceMod]</b> Error: <code>{}</code>",
        "no_audio": "<b>[VoiceMod]</b> No audio/link provided",
        "no_chat": "<b>[VoiceMod]</b> Use this command in a group/channel with voice chat",
        "not_supergroup": "<b>[VoiceMod]</b> Voice chats only work in supergroups/channels. Convert this group to supergroup first (enable chat history or add a bot)",
        "recognized": "<b>[Shazam]</b> {}",
        "not_recognized": "<b>[Shazam]</b> Could not recognize",
        "reply_audio": "<b>[Shazam]</b> Reply to audio",
        "searching": "<b>[VoiceMod]</b> Searching...",
        "not_found": "<b>[VoiceMod]</b> Not found: <code>{}</code>",
    }

    strings_ru = {
        "downloading": "<b>[VoiceMod]</b> Скачивание...",
        "playing": "<b>[VoiceMod]</b> Воспроизведение...",
        "plsjoin": "<b>[VoiceMod]</b> Сначала .vjoin",
        "stop": "<b>[VoiceMod]</b> Остановлено!",
        "join": "<b>[VoiceMod]</b> Подключён!",
        "leave": "<b>[VoiceMod]</b> Отключён!",
        "pause": "<b>[VoiceMod]</b> Пауза!",
        "resume": "<b>[VoiceMod]</b> Продолжено!",
        "mute": "<b>[VoiceMod]</b> Заглушено!",
        "unmute": "<b>[VoiceMod]</b> Звук вкл!",
        "error": "<b>[VoiceMod]</b> Ошибка: <code>{}</code>",
        "no_audio": "<b>[VoiceMod]</b> Укажи ссылку или реплай",
        "recognized": "<b>[Shazam]</b> {}",
        "not_recognized": "<b>[Shazam]</b> Не распознано",
        "reply_audio": "<b>[Shazam]</b> Реплай на аудио",
        "searching": "<b>[VoiceMod]</b> Поиск...",
        "not_found": "<b>[VoiceMod]</b> Не найдено: <code>{}</code>",
        "no_chat": "<b>[VoiceMod]</b> Используй эту команду в группе/канале с войс-чатом",
        "not_supergroup": "<b>[VoiceMod]</b> Войс-чаты работают только в супергруппах/каналах. Преобразуй группу в супергруппу (включи историю чата или добавь бота)",
    }

    ytdlopts = {
        'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best',
        'geo_bypass': True,
        'nocheckcertificate': True,
        'merge_output_format': 'mp4',
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
        'outtmpl': 'ytdl_out.%(ext)s',
        'quiet': True,
        'no_warnings': True,
    }

    def __init__(self):
        self.call = None
        self.active_chats = set()
        self._cookies_path = None  # Temporary file path

    async def _fetch_default_cookies(self):
        """Download cookies from default URL if no local cookies set"""
        if self.get("cookies_content"):
            # User has their own cookies, don't override
            return
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(DEFAULT_COOKIES_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        content = await resp.text()
                        if content and "youtube.com" in content:
                            self._cookies_path = os.path.abspath(".voicemod_cookies.txt")
                            with open(self._cookies_path, "w") as f:
                                f.write(content)
                            logger.info("VoiceMod: Loaded default cookies from URL")
        except Exception as e:
            logger.warning(f"VoiceMod: Failed to fetch default cookies: {e}")

    async def client_ready(self, client, db):
        self._client = client
        self._db = db
        # Migrate from old file-based storage
        if self.get("cookies_file"):
            self.set("cookies_file", None)
        # Restore cookies from DB to temp file if exists
        self._restore_cookies()
        # If no local cookies, try to fetch from default URL
        if not self._cookies_path:
            await self._fetch_default_cookies()
        
        wrapped = _ClientWrapper(client)
        self.call = PyTgCalls(wrapped)
        
        @self.call.on_update(fl.stream_end())
        async def on_stream_end(_, update: StreamEnded):
            try:
                await self.call.leave_call(update.chat_id)
                self.active_chats.discard(update.chat_id)
            except Exception:
                pass
        
        @self.call.on_update(fl.chat_update(ChatUpdate.Status.LEFT_CALL))
        async def on_left(_, update: ChatUpdate):
            self.active_chats.discard(update.chat_id)
        
        try:
            await self.call.start()
        except Exception as e:
            logger.exception(e)

    async def on_unload(self):
        if self.call:
            try:
                await self.call.stop()
            except Exception:
                pass
        # Clean up temp cookies file
        if self._cookies_path and os.path.exists(self._cookies_path):
            try:
                os.remove(self._cookies_path)
            except Exception:
                pass

    def _restore_cookies(self):
        """Restore cookies from DB to temporary file"""
        cookies_content = self.get("cookies_content")
        if cookies_content:
            self._cookies_path = os.path.abspath(".voicemod_cookies.txt")
            with open(self._cookies_path, "w") as f:
                f.write(cookies_content)
        else:
            self._cookies_path = None

    def _save_cookies(self, content: str):
        """Save cookies content to DB and restore to temp file"""
        self.set("cookies_content", content)
        self._restore_cookies()

    def _clear_cookies(self):
        """Clear cookies from DB and remove temp file"""
        self.set("cookies_content", None)
        if self._cookies_path and os.path.exists(self._cookies_path):
            try:
                os.remove(self._cookies_path)
            except Exception:
                pass
        self._cookies_path = None

    async def _get_chat(self, message: types.Message):
        args = utils.get_args_raw(message)
        if not args:
            # Use helper function to check chat type
            chat_id, error = await _get_chat_for_call(message.client, message)
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
            return _get_full_chat_id(entity.id)
        except Exception as e:
            await utils.answer(message, self.strings("error").format(str(e)))
            return None

    @loader.command(ru_doc="[чат] — войти в войс-чат")
    async def vjoincmd(self, message: types.Message):
        """[chat] - Join voice chat"""
        chat = await self._get_chat(message)
        if chat is None:
            return
        
        try:
            await self.call.play(chat, Stream())
            self.active_chats.add(chat)
            await utils.answer(message, self.strings("join"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="[чат] — выйти из войс-чата")
    async def vleavecmd(self, message: types.Message):
        """[chat] - Leave voice chat"""
        chat = await self._get_chat(message)
        if chat is None:
            return
        
        try:
            await self.call.leave_call(chat)
            self.active_chats.discard(chat)
            await utils.answer(message, self.strings("leave"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="<ссылка/реплай> — воспроизвести видео+аудио")
    async def vplaycmd(self, message: types.Message):
        """<link/reply> - Play video+audio"""
        args = utils.get_args_raw(message)
        reply = await message.get_reply_message()
        
        # Check chat type
        chat, error = await _get_chat_for_call(message.client, message)
        if error:
            return await utils.answer(message, self.strings(error))
        
        video_file = None
        from_reply = reply and reply.media
        
        if not from_reply and not args:
            return await utils.answer(message, self.strings("no_audio"))
        
        message = await utils.answer(message, self.strings("downloading"))
        
        try:
            if from_reply:
                video_file = await reply.download_media()
            else:
                if os.path.exists('ytdl_video.mp4'):
                    os.remove('ytdl_video.mp4')
                opts = {
                    'format': 'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]',
                    'merge_output_format': 'mp4',
                    'outtmpl': 'ytdl_video.%(ext)s',
                    'quiet': True,
                    'no_warnings': True,
                }
                if self._cookies_path and os.path.exists(self._cookies_path):
                    opts['cookiefile'] = self._cookies_path
                
                def download():
                    with YoutubeDL(opts) as ydl:
                        ydl.extract_info(args, download=True)
                
                await asyncio.to_thread(download)
                video_file = 'ytdl_video.mp4'
            
            if not video_file or not os.path.exists(video_file):
                return await utils.answer(message, self.strings("error").format("File not found"))
            
            await utils.answer(message, self.strings("playing"))
            
            await self.call.play(
                chat,
                MediaStream(
                    video_file,
                    audio_parameters=AudioQuality.HIGH,
                    video_parameters=VideoQuality.HD_720p,
                    audio_flags=MediaStream.Flags.REQUIRED,
                    video_flags=MediaStream.Flags.AUTO_DETECT,
                ),
            )
            self.active_chats.add(chat)
            
        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="— стоп")
    async def vstopcmd(self, message: types.Message):
        """Stop playing"""
        chat = await self._get_chat(message)
        if chat is None:
            return
        try:
            await self.call.leave_call(chat)
            self.active_chats.discard(chat)
            await utils.answer(message, self.strings("stop"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="— пауза")
    async def vpausecmd(self, message: types.Message):
        """Pause playing"""
        chat = await self._get_chat(message)
        if chat is None:
            return
        try:
            await self.call.pause(chat)
            await utils.answer(message, self.strings("pause"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="— продолжить")
    async def vresumecmd(self, message: types.Message):
        """Resume playing"""
        chat = await self._get_chat(message)
        if chat is None:
            return
        try:
            await self.call.resume(chat)
            await utils.answer(message, self.strings("resume"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="— заглушить")
    async def vmutecmd(self, message: types.Message):
        """Mute stream"""
        chat = await self._get_chat(message)
        if chat is None:
            return
        try:
            await self.call.mute(chat)
            await utils.answer(message, self.strings("mute"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="— включить звук")
    async def vunmutecmd(self, message: types.Message):
        """Unmute stream"""
        chat = await self._get_chat(message)
        if chat is None:
            return
        try:
            await self.call.unmute(chat)
            await utils.answer(message, self.strings("unmute"))
        except Exception as e:
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="<запрос> — поиск музыки")
    async def smcmd(self, message: types.Message):
        """<query> - Search music"""
        args = utils.get_args_raw(message)
        reply = await message.get_reply_message()
        
        if not args:
            return await utils.answer(message, self.strings("no_audio"))
        
        await utils.answer(message, self.strings("searching"))
        
        try:
            music = await message.client.inline_query('lybot', args)
            await message.delete()
            await message.client.send_file(
                message.chat_id,
                music[0].result.document,
                reply_to=reply.id if reply else None
            )
        except Exception:
            await message.client.send_message(
                message.chat_id,
                self.strings("not_found").format(utils.escape_html(args))
            )

    @loader.command(ru_doc="<реплай> — распознать трек")
    async def shazamcmd(self, message: types.Message):
        """<reply> - Recognize track with Shazam"""
        reply = await message.get_reply_message()
        
        if not reply or not reply.media:
            return await utils.answer(message, self.strings("reply_audio"))
        
        await utils.answer(message, self.strings("downloading"))
        
        try:
            audio_bytes = await reply.download_media(bytes)
            
            shazam = Shazam(audio_bytes)
            result = next(shazam.recognizeSong())
            
            if 'track' in result[1]:
                track = result[1]['track']
                title = track['share']['subject']
                image = track['images'].get('background', track['images'].get('coverart'))
                
                await message.client.send_file(
                    message.chat_id,
                    file=image,
                    caption=self.strings("recognized").format(utils.escape_html(title)),
                    reply_to=reply.id
                )
                await message.delete()
            else:
                await utils.answer(message, self.strings("not_recognized"))
                
        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("not_recognized"))
