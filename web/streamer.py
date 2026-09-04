# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)
#
# FileToLink streaming core. The web server runs as its own gunicorn
# process (no TgClient), so this module owns a small pool of Telegram
# clients — the bot token plus every HELPER_TOKEN — and load-balances
# range requests across them. Links are HMAC-signed with a secret both
# processes derive from BOT_TOKEN, so URLs can't be forged.

from asyncio import Lock, sleep
from collections import OrderedDict
from hashlib import sha256
from hmac import compare_digest, new as hmac_new
from math import ceil, floor
from time import time

from pyrogram import Client, raw, utils
from pyrogram.errors import AuthBytesInvalid, FloodWait
from pyrogram.file_id import FileId, FileType, ThumbnailSource
from pyrogram.session import Auth, Session

from bot.core.config_manager import Config

CHUNK_SIZE = 1024 * 1024
PROPS_CACHE_MAX = 512


class NoStreamClients(RuntimeError):
    """No Telegram client could be started for streaming."""


def _secret():
    return sha256(
        (Config.BOT_TOKEN or "neo-wzml").encode()
    ).digest()


def sign(chat_id, message_id):
    """Short signature binding a link to one specific message."""
    payload = f"{chat_id}:{message_id}".encode()
    return hmac_new(_secret(), payload, sha256).hexdigest()[:16]


def verify(chat_id, message_id, token):
    try:
        return compare_digest(sign(chat_id, message_id), str(token))
    except Exception:
        return False


def make_path(chat_id, message_id):
    """URL path component: <message_id>/<sig>"""
    return f"{message_id}/{sign(chat_id, message_id)}"


def bin_chat():
    """The chat /link stores files in. Both processes MUST derive this
    identically — the signature is bound to it, so any difference here
    (a `chat|thread` suffix, stray whitespace, str vs int) makes every
    generated link fail verification."""
    chat = Config.FILETOLINK_CHAT or Config.LEECH_DUMP_CHAT
    if not chat:
        return None
    chat = str(chat).split("|", 1)[0].strip()
    return int(chat) if chat.lstrip("-").isdigit() else chat


class StreamClients:
    """Lazily-started Telegram clients used only for streaming."""

    _clients = []
    _loads = {}
    _lock = Lock()
    _started = False

    @classmethod
    async def start(cls):
        if cls._started:  # lock-free fast path: every request hits this
            return cls._clients
        async with cls._lock:
            if cls._started:
                return cls._clients
            tokens = [Config.BOT_TOKEN]
            if Config.HELPER_TOKENS:
                tokens += Config.HELPER_TOKENS.split()
            for no, token in enumerate(tokens):
                if not token:
                    continue
                try:
                    client = Client(
                        f"NEO-WZML-Stream{no}",
                        api_id=Config.TELEGRAM_API,
                        api_hash=Config.TELEGRAM_HASH,
                        bot_token=token,
                        proxy=Config.TG_PROXY,
                        in_memory=True,
                        no_updates=True,
                        max_concurrent_transmissions=10,
                    )
                    await client.start()
                    cls._clients.append(client)
                    cls._loads[len(cls._clients) - 1] = 0
                except Exception as e:
                    from logging import getLogger

                    getLogger(__name__).error(
                        f"Stream client {no} failed to start: {e}"
                    )
            cls._started = True
            return cls._clients

    @classmethod
    async def stop(cls):
        async with cls._lock:
            await ByteStreamer.close_all_sessions()
            for client in cls._clients:
                try:
                    await client.stop()
                except Exception:
                    pass
            cls._clients = []
            cls._loads = {}
            cls._started = False

    @classmethod
    def pick(cls):
        """Least-loaded client, so concurrent viewers spread across bots.
        The load is booked here rather than when streaming begins — a
        burst of concurrent requests would otherwise all read the same
        stale counts and pile onto one bot. Callers must release()."""
        if not cls._clients:
            raise NoStreamClients("No stream clients available")
        index = min(cls._loads, key=cls._loads.get)
        cls.acquire(index)
        return index, cls._clients[index]

    @classmethod
    def acquire(cls, index):
        cls._loads[index] = cls._loads.get(index, 0) + 1

    @classmethod
    def release(cls, index):
        cls._loads[index] = max(0, cls._loads.get(index, 1) - 1)

    @classmethod
    def loads(cls):
        return dict(cls._loads)


class ByteStreamer:
    """Serves byte ranges of a Telegram file over raw upload.GetFile."""

    def __init__(self, client, index):
        self.client = client
        self.index = index

    # Media sessions are pooled across requests keyed by (client, DC):
    # building one costs an Auth handshake plus up to 6 authorization
    # round-trips on a foreign DC, and players issue many range requests
    # per file — per-request sessions made every seek pay that cost.
    _sessions = {}
    _session_lock = Lock()

    _props_cache = OrderedDict()
    _CACHE_TTL = 30 * 60

    @staticmethod
    def _media(message):
        for attr in (
            "document",
            "video",
            "audio",
            "photo",
            "animation",
            "voice",
            "video_note",
            "sticker",
        ):
            if media := getattr(message, attr, None):
                return media
        return None

    @classmethod
    def invalidate(cls, chat_id, message_id):
        cls._props_cache.pop((chat_id, message_id), None)

    async def get_properties(self, chat_id, message_id, refresh=False):
        """(FileId, file_size, file_name, mime_type), LRU-cached."""
        key = (chat_id, message_id)
        now = time()
        if not refresh and (hit := self._props_cache.get(key)):
            props, stamp = hit
            if now - stamp < self._CACHE_TTL:
                self._props_cache.move_to_end(key)
                return props
            del self._props_cache[key]

        message = await self.client.get_messages(chat_id, message_id)
        if not message or message.empty:
            raise FileNotFoundError("Message not found or deleted")
        media = self._media(message)
        if media is None:
            raise FileNotFoundError("Message has no downloadable media")

        props = (
            FileId.decode(media.file_id),
            getattr(media, "file_size", 0) or 0,
            getattr(media, "file_name", "") or f"{message_id}.bin",
            getattr(media, "mime_type", "") or "application/octet-stream",
        )
        self._props_cache[key] = (props, now)
        self._props_cache.move_to_end(key)
        while len(self._props_cache) > PROPS_CACHE_MAX:
            self._props_cache.popitem(last=False)
        return props

    async def _media_session(self, file_id):
        dc_id = file_id.dc_id
        key = (self.index, dc_id)
        if (session := self._sessions.get(key)) is not None:
            return session

        async with self._session_lock:
            if (session := self._sessions.get(key)) is not None:
                return session
            return await self._build_session(key, dc_id)

    async def _build_session(self, key, dc_id):
        client = self.client
        if dc_id != await client.storage.dc_id():
            session = Session(
                client,
                dc_id,
                await Auth(client, dc_id, await client.storage.test_mode()).create(),
                await client.storage.test_mode(),
                is_media=True,
            )
            await session.start()
            for _ in range(6):
                exported = await client.invoke(
                    raw.functions.auth.ExportAuthorization(dc_id=dc_id)
                )
                try:
                    await session.invoke(
                        raw.functions.auth.ImportAuthorization(
                            id=exported.id, bytes=exported.bytes
                        )
                    )
                    break
                except AuthBytesInvalid:
                    await sleep(1)
            else:
                await session.stop()
                raise AuthBytesInvalid
        else:
            session = Session(
                client,
                dc_id,
                await client.storage.auth_key(),
                await client.storage.test_mode(),
                is_media=True,
            )
            await session.start()

        self._sessions[key] = session
        return session

    @staticmethod
    def _location(file_id):
        file_type = file_id.file_type
        if file_type == FileType.CHAT_PHOTO:
            if file_id.chat_id > 0:
                peer = raw.types.InputPeerUser(
                    user_id=file_id.chat_id, access_hash=file_id.chat_access_hash
                )
            else:
                peer = (
                    raw.types.InputPeerChat(chat_id=-file_id.chat_id)
                    if file_id.chat_access_hash == 0
                    else raw.types.InputPeerChannel(
                        channel_id=utils.get_channel_id(file_id.chat_id),
                        access_hash=file_id.chat_access_hash,
                    )
                )
            return raw.types.InputPeerPhotoFileLocation(
                peer=peer,
                volume_id=file_id.volume_id,
                local_id=file_id.local_id,
                big=file_id.thumbnail_source == ThumbnailSource.CHAT_PHOTO_BIG,
            )
        if file_type == FileType.PHOTO:
            return raw.types.InputPhotoFileLocation(
                id=file_id.media_id,
                access_hash=file_id.access_hash,
                file_reference=file_id.file_reference,
                thumb_size=file_id.thumbnail_size,
            )
        return raw.types.InputDocumentFileLocation(
            id=file_id.media_id,
            access_hash=file_id.access_hash,
            file_reference=file_id.file_reference,
            thumb_size=file_id.thumbnail_size,
        )

    async def yield_file(self, file_id, offset, first_cut, last_cut, part_count):
        """Yield the requested byte range, chunk by chunk."""
        session = await self._media_session(file_id)
        location = self._location(file_id)
        current_part = 1
        fails = 0

        while current_part <= part_count:
            try:
                r = await session.invoke(
                    raw.functions.upload.GetFile(
                        location=location, offset=offset, limit=CHUNK_SIZE
                    )
                )
            except FloodWait as f:
                fails += 1
                if fails > 5:
                    raise
                await sleep(f.value + 1)
                continue
            except (TimeoutError, ConnectionError):
                fails += 1
                if fails > 5:
                    raise
                await sleep(1)
                continue

            if not isinstance(r, raw.types.upload.File):
                raise ValueError(f"Unexpected response: {r}")
            chunk = r.bytes
            if not chunk:
                break

            fails = 0
            if part_count == 1:
                yield chunk[first_cut:last_cut]
            elif current_part == 1:
                yield chunk[first_cut:]
            elif current_part == part_count:
                yield chunk[:last_cut]
            else:
                yield chunk

            current_part += 1
            offset += CHUNK_SIZE

    @classmethod
    async def close_all_sessions(cls):
        """Only on shutdown — sessions are intentionally long-lived."""
        for session in list(cls._sessions.values()):
            try:
                await session.stop()
            except Exception:
                pass
        cls._sessions.clear()


def range_params(start, end, file_size):
    """Translate an HTTP byte range into GetFile chunk parameters."""
    until_bytes = min(end, file_size - 1)
    offset = start - (start % CHUNK_SIZE)
    first_cut = start - offset
    last_cut = until_bytes % CHUNK_SIZE + 1
    part_count = until_bytes // CHUNK_SIZE - offset // CHUNK_SIZE + 1
    return offset, first_cut, last_cut, part_count
