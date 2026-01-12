"""
    🎵 VoiceMod — управление голосовыми чатами
    
    Модуль для воспроизведения аудио в голосовых чатах Telegram.
    Поддерживает YouTube, прямые ссылки и аудиофайлы.
"""

__version__ = (3, 0, 0)
# meta developer: @samuray43k @ai
# meta pic: https://img.icons8.com/fluency/512/microphone.png
# scope: hikka_only
# requires: ffmpeg-python yt-dlp shazamio py-tgcalls

import os
import re
import logging
import asyncio
import tempfile
import wave
from typing import Dict, Optional

from .. import loader, utils

# Импорты через telethon — Heroku автоматически подменит на herokutl
from telethon.types import Message

logger = logging.getLogger(__name__)


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
        
        try:
            from pytgcalls import PyTgCalls
            from pytgcalls.types import MediaStream
            
            logger.info("Initializing PyTgCalls...")
            
            # pytgcalls проверяет client.__class__.__module__.split('.')[0]
            # Должен быть 'telethon', 'pyrogram' или 'hydrogram'
            # HerokutTL имеет 'herokutl' — нужна обёртка
            wrapped_client = self._wrap_client(client)
            
            # Также нужно создать herokutl_client.py в pytgcalls
            # потому что Heroku перехватывает import telethon -> herokutl
            self._patch_pytgcalls()
            
            self._call_py = PyTgCalls(wrapped_client)
            logger.info("PyTgCalls instance created")
            
            asyncio.create_task(self._start_pytgcalls())
        except ImportError as e:
            logger.warning(f"pytgcalls not available: {e}")
            self._call_py = None
        except Exception as e:
            logger.exception(f"Failed to initialize PyTgCalls: {e}")
            self._call_py = None

    def _wrap_client(self, client):
        """Обёртка чтобы pytgcalls видел herokutl как telethon"""
        class TelethonClientWrapper:
            def __init__(self, original):
                object.__setattr__(self, '_client', original)
            
            def __getattr__(self, name):
                return getattr(object.__getattribute__(self, '_client'), name)
            
            def __setattr__(self, name, value):
                if name == '_client':
                    object.__setattr__(self, name, value)
                else:
                    setattr(object.__getattribute__(self, '_client'), name, value)
        
        TelethonClientWrapper.__module__ = 'telethon.client.telegramclient'
        return TelethonClientWrapper(client)

    def _patch_pytgcalls(self):
        """Создаёт и патчит herokutl_client.py для совместимости с herokutl"""
        import shutil
        
        try:
            import pytgcalls
            pytgcalls_path = os.path.dirname(pytgcalls.__file__)
            mtproto_path = os.path.join(pytgcalls_path, "mtproto")
            
            src = os.path.join(mtproto_path, "telethon_client.py")
            dst = os.path.join(mtproto_path, "herokutl_client.py")
            pycache = os.path.join(mtproto_path, "__pycache__")
            
            # Всегда удаляем старый herokutl_client и кэш для чистого патча
            if os.path.exists(dst):
                os.remove(dst)
                logger.info("Removed old herokutl_client.py")
            if os.path.exists(pycache):
                shutil.rmtree(pycache)
                logger.info("Removed __pycache__")
            
            if os.path.exists(src):
                # Читаем telethon_client.py
                with open(src, 'r') as f:
                    content = f.read()
                
                # Заменяем telethon на herokutl
                content = content.replace('from telethon', 'from herokutl')
                content = content.replace('import telethon', 'import herokutl')
                
                # ГЛАВНЫЙ ПАТЧ: полностью переписываем обработку UpdateGroupCall
                # herokutl имеет peer вместо chat_id
                old_block = '''if isinstance(
                update,
                UpdateGroupCall,
            ):
                chat_id = self.chat_id(
                    await self._get_entity_group(
                        update.chat_id,
                    ),
                )'''
                
                new_block = '''if isinstance(
                update,
                UpdateGroupCall,
            ):
                # herokutl compatibility patch
                try:
                    if hasattr(update, 'peer') and update.peer:
                        if hasattr(update.peer, 'channel_id'):
                            raw_id = update.peer.channel_id
                        elif hasattr(update.peer, 'chat_id'):
                            raw_id = update.peer.chat_id
                        else:
                            return
                    elif hasattr(update, 'chat_id'):
                        raw_id = update.chat_id
                    else:
                        return
                    chat_id = self.chat_id(
                        await self._get_entity_group(raw_id),
                    )
                except Exception:
                    return'''
                
                content = content.replace(old_block, new_block)
                
                # Записываем
                with open(dst, 'w') as f:
                    f.write(content)
                
                logger.info(f"Created/updated herokutl_client.py with patches")
        except Exception as e:
            logger.warning(f"Could not patch pytgcalls: {e}")

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
        
        if args:
            try:
                chat_id = int(args.split()[0])
            except ValueError:
                try:
                    entity = await message.client.get_entity(args.split()[0])
                    chat_id = entity.id
                except Exception as e:
                    await utils.answer(message, self.strings("error").format(str(e)))
                    return None
        else:
            chat_id = utils.get_chat_id(message)
        
        # pytgcalls проверяет: is_p2p = chat_id > 0
        # Для group calls нужен ОТРИЦАТЕЛЬНЫЙ chat_id
        # Формат: -100XXXXXXXXXX для каналов/супергрупп
        if chat_id and chat_id > 0:
            try:
                entity = await message.client.get_entity(message.peer_id)
                # Канал или супергруппа
                if hasattr(entity, 'broadcast') or hasattr(entity, 'megagroup'):
                    chat_id = int(f"-100{chat_id}")
                # Обычная группа  
                elif hasattr(entity, 'chat_id') or (hasattr(entity, 'id') and not hasattr(entity, 'username')):
                    chat_id = -chat_id
            except:
                # Если не получилось определить тип, пробуем как супергруппу
                chat_id = int(f"-100{chat_id}")
        
        logger.info(f"Resolved chat_id: {chat_id}")
        return chat_id

    def _check_pytgcalls(self) -> bool:
        """Проверка доступности pytgcalls"""
        return self._call_py is not None

    def _create_silent_wav(self) -> str:
        """Создаёт временный WAV-файл с тишиной"""
        fd, path = tempfile.mkstemp(suffix='.wav')
        os.close(fd)
        
        with wave.open(path, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(48000)
            wav.writeframes(b'\x00\x00' * 48000)  # 1 секунда тишины
        
        return path

    @loader.command(ru_doc="[чат] — подключиться к голосовому чату")
    async def vjoincmd(self, message: Message):
        """Join voice chat"""
        if not self._check_pytgcalls():
            return await utils.answer(message, self.strings("no_pytgcalls"))
        
        chat_id = await self._get_chat_id(message)
        if not chat_id:
            return
        
        try:
            from pytgcalls.types import MediaStream
            
            silent_file = self._create_silent_wav()
            
            await self._call_py.play(chat_id, MediaStream(silent_file))
            self._active_chats[chat_id] = True
            await utils.answer(message, self.strings("join"))
            
            # Удаляем временный файл
            try:
                os.remove(silent_file)
            except:
                pass
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
        
        link = None
        audio_file = None
        
        # Парсинг аргументов
        if args:
            match = re.match(r"(-?\d+|@[\w]{5,})\s+(.*)", args)
            if match:
                # Первый аргумент — chat_id, остальное — ссылка
                link = match.group(2)
            else:
                link = args
        
        # Проверка реплая на аудио
        if reply and reply.audio and not link:
            audio_file = reply
        
        if not link and not audio_file:
            return await utils.answer(message, self.strings("no_audio"))
        
        # Получаем chat_id через нашу функцию (с правильным -100 префиксом)
        chat_id = await self._get_chat_id(message)
        if not chat_id:
            return
        
        try:
            from pytgcalls.types import MediaStream
            
            message = await utils.answer(message, self.strings("downloading"))
            
            if audio_file:
                file_path = await audio_file.download_media()
            else:
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
            
            await self._call_py.play(chat_id, MediaStream(file_path))
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
            
            audio_data = await reply.download_media(bytes)
            
            shazam = Shazam()
            result = await shazam.recognize(audio_data)
            
            if not result.get("track"):
                return await utils.answer(message, self.strings("not_recognized"))
            
            track = result["track"]
            title = track.get("title", "Unknown")
            artist = track.get("subtitle", "Unknown")
            
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
