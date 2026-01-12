"""
    🎵 VoiceMod — управление голосовыми чатами
    
    Модуль для воспроизведения аудио в голосовых чатах Telegram.
    Поддерживает YouTube, прямые ссылки и аудиофайлы.
    
    Обновлено для pytgcalls 3.x
"""

__version__ = (2, 2, 0)
# meta developer: @samuray43k @ai
# meta pic: https://img.icons8.com/fluency/512/microphone.png
# scope: hikka_only
# requires: ffmpeg-python yt-dlp shazamio py-tgcalls

import io
import os
import re
import logging
import asyncio
import subprocess
import sys
import site
import importlib
from typing import Dict, Optional, Union

from .. import loader, utils
from herokutl.types import Message

logger = logging.getLogger(__name__)

# Глобальная переменная для правильного модуля pytgcalls
_pytgcalls_module = None


def _find_correct_pytgcalls():
    """
    Находит правильный py-tgcalls даже если установлен конфликтующий MarshalX/pytgcalls.
    py-tgcalls имеет класс PyTgCalls, MarshalX — нет.
    """
    global _pytgcalls_module
    
    if _pytgcalls_module is not None:
        return _pytgcalls_module
    
    # Сначала пробуем обычный импорт
    try:
        import pytgcalls
        if hasattr(pytgcalls, 'PyTgCalls'):
            _pytgcalls_module = pytgcalls
            logger.info("Found py-tgcalls via direct import")
            return pytgcalls
    except ImportError:
        pass
    
    # Если не нашли PyTgCalls — ищем в user site-packages
    user_site = site.getusersitepackages()
    pytgcalls_paths = [
        os.path.join(user_site, 'pytgcalls'),
        os.path.expanduser('~/.local/lib/python3.10/site-packages/pytgcalls'),
        os.path.expanduser('~/.local/lib/python3.11/site-packages/pytgcalls'),
        os.path.expanduser('~/.local/lib/python3.12/site-packages/pytgcalls'),
    ]
    
    for path in pytgcalls_paths:
        if os.path.isdir(path):
            parent = os.path.dirname(path)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            
            # Перезагружаем модуль
            if 'pytgcalls' in sys.modules:
                del sys.modules['pytgcalls']
            
            # Удаляем все субмодули pytgcalls
            to_delete = [k for k in sys.modules.keys() if k.startswith('pytgcalls')]
            for k in to_delete:
                del sys.modules[k]
            
            try:
                import pytgcalls
                if hasattr(pytgcalls, 'PyTgCalls'):
                    _pytgcalls_module = pytgcalls
                    logger.info(f"Found py-tgcalls in {parent}")
                    return pytgcalls
            except Exception as e:
                logger.debug(f"Failed to import from {parent}: {e}")
                continue
    
    return None


def ensure_pytgcalls():
    """Проверяет/устанавливает py-tgcalls"""
    module = _find_correct_pytgcalls()
    if module is not None:
        return True
    
    logger.info("py-tgcalls not found, installing...")
    try:
        subprocess.check_call([
            sys.executable, "-m", "pip", "install", 
            "py-tgcalls", "-q", "--user", "--force-reinstall"
        ])
        logger.info("py-tgcalls installed successfully")
        
        # Пробуем найти после установки
        return _find_correct_pytgcalls() is not None
    except Exception as e:
        logger.error(f"Failed to install py-tgcalls: {e}")
        return False


def get_pytgcalls():
    """Возвращает модуль py-tgcalls"""
    global _pytgcalls_module
    if _pytgcalls_module is None:
        _find_correct_pytgcalls()
    return _pytgcalls_module


@loader.tds
class VoiceModMod(loader.Module):
    """Управление голосовыми чатами: воспроизведение, пауза, Shazam"""

    strings = {
        "name": "VoiceMod",
        "downloading": "<b>🎵 [VoiceMod]</b> Скачивание...",
        "converting": "<b>🎵 [VoiceMod]</b> Конвертация...",
        "playing": "<b>🎵 [VoiceMod]</b> Воспроизведение...",
        "not_in_call": "<b>🎵 [VoiceMod]</b> Не в звонке. Используй <code>.vjoin</code>",
        "no_audio": "<b>🎵 [VoiceMod]</b> Нет аудио/ссылки",
        "stop": "<b>🎵 [VoiceMod]</b> Воспроизведение остановлено!",
        "join": "<b>🎵 [VoiceMod]</b> Подключён к голосовому чату!",
        "leave": "<b>🎵 [VoiceMod]</b> Отключён от голосового чата!",
        "pause": "<b>🎵 [VoiceMod]</b> Пауза!",
        "resume": "<b>🎵 [VoiceMod]</b> Продолжение!",
        "mute": "<b>🎵 [VoiceMod]</b> Звук выключен!",
        "unmute": "<b>🎵 [VoiceMod]</b> Звук включён!",
        "error": "<b>🎵 [VoiceMod]</b> Ошибка: <code>{}</code>",
        "no_pytgcalls": "<b>🎵 [VoiceMod]</b> pytgcalls не установлен!",
        "recognizing": "<b>🎵 [Shazam]</b> Распознаю...",
        "recognized": "<b>🎵 [Shazam]</b> {}",
        "not_recognized": "<b>🎵 [Shazam]</b> Не удалось распознать",
        "reply_audio": "<b>🎵 [Shazam]</b> Ответь на аудио",
        "searching": "<b>🎵 [VoiceMod]</b> Ищу музыку...",
        "not_found": "<b>🎵 [VoiceMod]</b> Музыка <code>{}</code> не найдена",
        "no_args": "<b>🎵 [VoiceMod]</b> Укажи название",
    }

    strings_ru = strings

    def __init__(self):
        self._call_py = None
        self._active_chats: Dict[int, bool] = {}

    async def client_ready(self, client, db):
        self._client = client
        self._db = db
        
        # Автоустановка py-tgcalls если не установлен
        if not ensure_pytgcalls():
            logger.error("Could not install py-tgcalls")
            self._call_py = None
            return
        
        try:
            # Используем нашу функцию поиска правильного модуля
            pytgcalls_mod = get_pytgcalls()
            if pytgcalls_mod is None:
                logger.warning("py-tgcalls not found (PyTgCalls class missing)")
                self._call_py = None
                return
            
            logger.info(f"pytgcalls module: {pytgcalls_mod}")
            logger.info(f"pytgcalls location: {getattr(pytgcalls_mod, '__file__', 'unknown')}")
            
            if not hasattr(pytgcalls_mod, 'PyTgCalls'):
                logger.error(f"PyTgCalls class not found in module. Available: {dir(pytgcalls_mod)}")
                self._call_py = None
                return
            
            PyTgCalls = pytgcalls_mod.PyTgCalls
            logger.info(f"PyTgCalls class: {PyTgCalls}")
            
            # Создаём обёртку для HerokutTL, чтобы pytgcalls распознал его как Telethon
            wrapped_client = self._wrap_client_for_pytgcalls(client)
            logger.info(f"Wrapped client created: {wrapped_client.__class__.__module__}")
            
            self._call_py = PyTgCalls(wrapped_client)
            logger.info("PyTgCalls instance created successfully")
            asyncio.create_task(self._start_pytgcalls())
        except ImportError as e:
            logger.exception(f"ImportError during pytgcalls init: {e}")
            self._call_py = None
        except Exception as e:
            logger.exception(f"Failed to initialize PyTgCalls: {e}")
            self._call_py = None

    def _wrap_client_for_pytgcalls(self, client):
        """
        Оборачивает HerokutTL клиент так, чтобы pytgcalls распознал его как Telethon.
        pytgcalls проверяет client.__class__.__module__.split('.')[0] == 'telethon'
        """
        # Создаём класс-обёртку с подменённым __module__
        class TelethonClientWrapper:
            """Wrapper that makes HerokutTL look like Telethon for pytgcalls"""
            
            def __init__(self, original_client):
                self._client = original_client
                # Копируем все атрибуты оригинального клиента
                
            def __getattr__(self, name):
                return getattr(self._client, name)
            
            def __setattr__(self, name, value):
                if name == '_client':
                    object.__setattr__(self, name, value)
                else:
                    setattr(self._client, name, value)
        
        # Подменяем __module__ на telethon
        TelethonClientWrapper.__module__ = 'telethon.client.telegramclient'
        
        return TelethonClientWrapper(client)

    async def _start_pytgcalls(self):
        """Запуск PyTgCalls в фоне"""
        try:
            await self._call_py.start()
            logger.info("PyTgCalls started successfully")
        except Exception as e:
            logger.exception(f"Failed to start PyTgCalls: {e}")

    async def _get_chat_id(self, message: Message) -> Optional[int]:
        """Получить ID чата из аргументов или текущего чата"""
        args = utils.get_args_raw(message)
        if not args:
            return utils.get_chat_id(message)
        
        try:
            return int(args.split()[0])
        except ValueError:
            pass
        
        try:
            entity = await message.client.get_entity(args.split()[0])
            return entity.id
        except Exception as e:
            await utils.answer(message, self.strings("error").format(str(e)))
            return None

    def _check_pytgcalls(self) -> bool:
        """Проверка доступности pytgcalls"""
        return self._call_py is not None

    @loader.command(ru_doc="[чат] — подключиться к голосовому чату")
    async def vjoincmd(self, message: Message):
        """Join voice chat"""
        if not self._check_pytgcalls():
            return await utils.answer(message, self.strings("no_pytgcalls"))
        
        chat_id = await self._get_chat_id(message)
        if not chat_id:
            return
        
        try:
            pytgcalls_mod = get_pytgcalls()
            MediaStream = pytgcalls_mod.types.MediaStream
            
            # Создаём тихий стрим для подключения
            await self._call_py.play(
                chat_id,
                MediaStream(
                    media_path=None,
                    audio_flags=MediaStream.Flags.IGNORE,
                    video_flags=MediaStream.Flags.IGNORE,
                ),
            )
            self._active_chats[chat_id] = True
            await utils.answer(message, self.strings("join"))
        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="[чат] — отключиться от голосового чата")
    async def vleavecmd(self, message: Message):
        """Leave voice chat"""
        if not self._check_pytgcalls():
            return await utils.answer(message, self.strings("no_pytgcalls"))
        
        chat_id = await self._get_chat_id(message)
        if not chat_id:
            return
        
        try:
            await self._call_py.leave_call(chat_id)
            self._active_chats.pop(chat_id, None)
            await utils.answer(message, self.strings("leave"))
        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="[чат] <ссылка/реплай на аудио> — воспроизвести в VC")
    async def vplaycmd(self, message: Message):
        """Play audio in voice chat"""
        if not self._check_pytgcalls():
            return await utils.answer(message, self.strings("no_pytgcalls"))
        
        args = utils.get_args_raw(message)
        reply = await message.get_reply_message()
        
        chat_id = utils.get_chat_id(message)
        link = None
        audio_file = None
        
        # Парсинг аргументов
        if args:
            match = re.match(r"(-?\d+|@[\w]{5,})\s+(.*)", args)
            if match:
                try:
                    chat_id = int(match.group(1))
                except ValueError:
                    entity = await message.client.get_entity(match.group(1))
                    chat_id = entity.id
                link = match.group(2)
            else:
                link = args
        
        # Проверка реплая на аудио
        if reply and reply.audio and not link:
            audio_file = reply
        
        if not link and not audio_file:
            return await utils.answer(message, self.strings("no_audio"))
        
        try:
            pytgcalls_mod = get_pytgcalls()
            MediaStream = pytgcalls_mod.types.MediaStream
            
            message = await utils.answer(message, self.strings("downloading"))
            
            if audio_file:
                # Скачиваем аудиофайл
                file_path = await audio_file.download_media()
            else:
                # Используем yt-dlp для YouTube и других источников
                import yt_dlp
                
                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": "%(id)s.%(ext)s",
                    "quiet": True,
                    "no_warnings": True,
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "320",
                    }],
                }
                
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(link, download=True)
                    file_path = f"{info['id']}.mp3"
            
            message = await utils.answer(message, self.strings("playing"))
            
            # Воспроизведение
            await self._call_py.play(
                chat_id,
                MediaStream(file_path),
            )
            self._active_chats[chat_id] = True
            
        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="[чат] — пауза воспроизведения")
    async def vpausecmd(self, message: Message):
        """Pause playback"""
        if not self._check_pytgcalls():
            return await utils.answer(message, self.strings("no_pytgcalls"))
        
        chat_id = await self._get_chat_id(message)
        if not chat_id:
            return
        
        try:
            await self._call_py.pause(chat_id)
            await utils.answer(message, self.strings("pause"))
        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="[чат] — продолжить воспроизведение")
    async def vresumecmd(self, message: Message):
        """Resume playback"""
        if not self._check_pytgcalls():
            return await utils.answer(message, self.strings("no_pytgcalls"))
        
        chat_id = await self._get_chat_id(message)
        if not chat_id:
            return
        
        try:
            await self._call_py.resume(chat_id)
            await utils.answer(message, self.strings("resume"))
        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="[чат] — остановить воспроизведение")
    async def vstopcmd(self, message: Message):
        """Stop playback"""
        if not self._check_pytgcalls():
            return await utils.answer(message, self.strings("no_pytgcalls"))
        
        chat_id = await self._get_chat_id(message)
        if not chat_id:
            return
        
        try:
            await self._call_py.leave_call(chat_id)
            self._active_chats.pop(chat_id, None)
            await utils.answer(message, self.strings("stop"))
        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="[чат] — выключить звук")
    async def vmutecmd(self, message: Message):
        """Mute"""
        if not self._check_pytgcalls():
            return await utils.answer(message, self.strings("no_pytgcalls"))
        
        chat_id = await self._get_chat_id(message)
        if not chat_id:
            return
        
        try:
            await self._call_py.mute(chat_id)
            await utils.answer(message, self.strings("mute"))
        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="[чат] — включить звук")
    async def vunmutecmd(self, message: Message):
        """Unmute"""
        if not self._check_pytgcalls():
            return await utils.answer(message, self.strings("no_pytgcalls"))
        
        chat_id = await self._get_chat_id(message)
        if not chat_id:
            return
        
        try:
            await self._call_py.unmute(chat_id)
            await utils.answer(message, self.strings("unmute"))
        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("error").format(str(e)))

    @loader.command(ru_doc="<название> — найти и отправить музыку")
    async def smcmd(self, message: Message):
        """Search and send music"""
        args = utils.get_args_raw(message)
        if not args:
            return await utils.answer(message, self.strings("no_args"))
        
        reply = await message.get_reply_message()
        
        try:
            message = await utils.answer(message, self.strings("searching"))
            
            music = await self._client.inline_query("lybot", args)
            if not music:
                return await utils.answer(
                    message, 
                    self.strings("not_found").format(utils.escape_html(args))
                )
            
            await message.delete()
            await self._client.send_file(
                message.peer_id,
                music[0].result.document,
                reply_to=reply.id if reply else None,
            )
        except Exception as e:
            logger.exception(e)
            await utils.answer(
                message,
                self.strings("not_found").format(utils.escape_html(args))
            )

    @loader.command(ru_doc="<реплай на аудио> — распознать трек через Shazam")
    async def shazamcmd(self, message: Message):
        """Recognize track with Shazam"""
        reply = await message.get_reply_message()
        
        if not reply or not reply.file:
            return await utils.answer(message, self.strings("reply_audio"))
        
        mime = getattr(reply.file, "mime_type", "")
        if not mime.startswith("audio") and not mime.startswith("video"):
            return await utils.answer(message, self.strings("reply_audio"))
        
        try:
            from shazamio import Shazam
            
            message = await utils.answer(message, self.strings("recognizing"))
            
            # Скачиваем аудио
            audio_data = await reply.download_media(bytes)
            
            # Распознаём
            shazam = Shazam()
            result = await shazam.recognize(audio_data)
            
            if not result.get("track"):
                return await utils.answer(message, self.strings("not_recognized"))
            
            track = result["track"]
            title = track.get("title", "Unknown")
            artist = track.get("subtitle", "Unknown")
            
            # Получаем обложку если есть
            cover_url = None
            if "images" in track:
                cover_url = track["images"].get("coverart")
            
            text = self.strings("recognized").format(
                f"<b>{utils.escape_html(artist)}</b> — {utils.escape_html(title)}"
            )
            
            if cover_url:
                await self._client.send_file(
                    message.peer_id,
                    cover_url,
                    caption=text,
                    reply_to=reply.id,
                )
                await message.delete()
            else:
                await utils.answer(message, text)
                
        except ImportError:
            await utils.answer(
                message, 
                self.strings("error").format("shazamio не установлен")
            )
        except Exception as e:
            logger.exception(e)
            await utils.answer(message, self.strings("not_recognized"))

    async def on_unload(self):
        """Очистка при выгрузке модуля"""
        if self._call_py:
            try:
                for chat_id in list(self._active_chats.keys()):
                    try:
                        await self._call_py.leave_call(chat_id)
                    except Exception:
                        pass
            except Exception:
                pass
