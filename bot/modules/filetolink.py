# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)
#
# /link — turn a Telegram file into direct streaming + download URLs
# served by the web process (see web/streamer.py, web/wserver.py).
# Works three ways:
#   • reply to a file with /link
#   • /link N  (batch: that message and the next N-1)
#   • just send a file to the bot in PM (auto, opt-out per user)

from pyrogram.enums import ButtonStyle
from bot import LOGGER, user_data
from bot.core.config_manager import Config
from bot.core.tg_client import TgClient
from bot.helper.ext_utils.bot_utils import new_task
from bot.helper.ext_utils.status_utils import get_readable_file_size
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.telegram_helper.message_utils import (
    edit_message,
    send_message,
)

_MEDIA_ATTRS = (
    "document",
    "video",
    "audio",
    "photo",
    "animation",
    "voice",
    "video_note",
    "sticker",
)

_STREAMABLE = (
    ".mkv", ".mp4", ".webm", ".avi", ".mov", ".m4v", ".ts", ".flv",
    ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wav",
)

MAX_BATCH = 50


def _get_media(message):
    for attr in _MEDIA_ATTRS:
        if media := getattr(message, attr, None):
            return media
    return None


def _bin_chat():
    # single source of truth shared with the web process — the link
    # signature is bound to this exact value
    from web.streamer import bin_chat

    return bin_chat()


def _is_streamable(message, media, file_name):
    mime = (getattr(media, "mime_type", "") or "").lower()
    return (
        file_name.lower().endswith(_STREAMABLE)
        or mime.startswith(("video/", "audio/"))
        or any(
            getattr(message, attr, None) is not None
            for attr in ("video", "audio", "voice", "animation", "video_note")
        )
    )


def _preflight():
    """Returns an error string, or None when FileToLink can run."""
    if not Config.FILETOLINK_ENABLED:
        return (
            "FileToLink is disabled. Enable <code>FILETOLINK_ENABLED</code> "
            "in /bsetting."
        )
    if not (Config.BASE_URL or "").strip():
        return "<code>BASE_URL</code> is not set — can't build links."
    if not _bin_chat():
        return (
            "Set <code>FILETOLINK_CHAT</code> (or <code>LEECH_DUMP_CHAT</code>) "
            "and make the bot an admin there."
        )
    return None


async def _build_links(source_msg, title="Link Generated"):
    """Copy one media message into the bin chat and render its links.
    Returns (text, markup) or raises."""
    bin_chat = _bin_chat()

    if source_msg.chat and source_msg.chat.id == bin_chat:
        # Already sitting in the bin chat — copying would post a duplicate
        # of the file right back into the conversation.
        stored = source_msg
    else:
        # Copy the message object we already hold rather than
        # Client.copy_message(), which re-fetches by id — that fetch comes
        # back empty wherever the bot can't read history (groups without
        # admin/privacy-off), and Message.copy() then silently returns None.
        stored = await source_msg.copy(chat_id=bin_chat, disable_notification=True)
        if isinstance(stored, list):
            stored = stored[0] if stored else None
        if stored is None:
            raise ValueError(
                "Telegram refused to copy this message. If it's in a group, "
                "make the bot an admin there (or disable its privacy mode) so "
                "it can read the file."
            )

    from web.streamer import make_path

    # Name/size come from the ORIGINAL message: Telegram rewrites the
    # filename on some copied media (dots become underscores), and the
    # user should see the name they recognise.
    src_media = _get_media(source_msg) or _get_media(stored)
    media = _get_media(stored) or src_media
    file_name = (
        getattr(src_media, "file_name", "")
        or getattr(media, "file_name", "")
        or "file"
    )
    file_size = getattr(src_media, "file_size", 0) or getattr(
        media, "file_size", 0
    ) or 0
    path = make_path(bin_chat, stored.id)

    base_url = Config.BASE_URL.rstrip("/")
    stream_url = f"{base_url}/stream/{path}"
    download_url = f"{base_url}/dl/{path}"
    watch_url = f"{base_url}/watch/{path}"
    streamable = _is_streamable(stored, media, file_name)

    buttons = ButtonMaker()
    if streamable:
        buttons.url_button("Watch", watch_url, style=ButtonStyle.PRIMARY)
    buttons.url_button("Download", download_url, style=ButtonStyle.SUCCESS)

    text = (
        f"<blockquote><b>◈ {title}</b></blockquote>\n"
        f"┃ <b>File:</b> <code>{file_name}</code>\n"
        f"┃ <b>Size:</b> {get_readable_file_size(file_size)}\n\n"
        f"┃ <b>Download:</b> <a href='{download_url}'>Click Here</a>"
    )
    if streamable:
        text += f"\n┃ <b>Stream:</b> <a href='{stream_url}'>Click Here</a>"

    return text, buttons.build_menu(2)


async def _reply_links(message, source_msg, title="Link Generated", status=None):
    """Generate links for one message and reply (or edit `status`)."""
    if not _get_media(source_msg):
        target = "That message has no file."
        if status:
            await edit_message(status, target)
        else:
            await send_message(message, target)
        return False
    try:
        text, markup = await _build_links(source_msg, title)
    except Exception as e:
        LOGGER.error(f"FileToLink: {e}", exc_info=True)
        err = (
            "<blockquote><b>✕ Couldn't generate the link</b></blockquote>\n"
            f"┃ <code>{e}</code>\n\n"
            f"┃ Check that the bot is an admin in the bin chat\n"
            f"┃ (<code>{_bin_chat()}</code>) and can read the source file."
        )
        if status:
            await edit_message(status, err)
        else:
            await send_message(message, err)
        return False
    if status:
        await edit_message(status, text, markup)
    else:
        await send_message(message, text, markup)
    return True


async def _status_report(message):
    """Ask the web process how the streaming pool is doing."""
    base_url = (Config.BASE_URL or "").rstrip("/")
    if not base_url:
        await send_message(message, "<code>BASE_URL</code> is not set.")
        return
    from aiohttp import ClientSession, ClientError

    try:
        async with ClientSession() as session:
            async with session.get(
                f"{base_url}/api/filetolink/status", timeout=10
            ) as resp:
                data = await resp.json()
    except (ClientError, TimeoutError, Exception) as e:
        await send_message(
            message,
            "<blockquote><b>◈ FILETOLINK STATUS</b></blockquote>\n"
            f"┃ <b>Web server:</b> unreachable\n┃ <code>{e}</code>",
        )
        return

    loads = data.get("loads") or {}
    lines = "\n".join(
        f"┃ <b>Bot {i}:</b> {n} active" for i, n in sorted(loads.items())
    ) or "┃ <i>no clients started yet</i>"
    await send_message(
        message,
        "<blockquote><b>◈ FILETOLINK STATUS</b></blockquote>\n"
        f"┃ <b>Stream bots:</b> {data.get('clients', 0)}\n"
        f"┃ <b>Cached files:</b> {data.get('cached', 0)}\n"
        f"┃ <b>Sessions:</b> {data.get('sessions', 0)}\n"
        f"{lines}",
    )


def _parse_batch(text):
    """'/link', '/link 5', '/link -i 5' -> count, or None if malformed."""
    parts = (text or "").split()
    if len(parts) < 2:
        return 1
    args = [p for p in parts[1:] if p != "-i"]
    if not args:
        return 1
    raw = args[0].strip()
    if not raw.isdigit():
        return None
    return int(raw)


@new_task
async def file_to_link(client, message):
    parts = (message.text or "").split()
    if len(parts) > 1 and parts[1].lower() in ("status", "stats", "health"):
        await _status_report(message)
        return

    if err := _preflight():
        await send_message(message, err)
        return

    reply = message.reply_to_message
    if not reply:
        await send_message(
            message,
            "<blockquote><b>◈ FileToLink</b></blockquote>\n"
            "┃ Reply to a file with <code>/link</code>\n"
            "┃ Batch: <code>/link 5</code> (this file + next 4)\n"
            "┃ Or just send a file to me in PM\n"
            "┃ Health: <code>/link status</code>",
        )
        return

    count = _parse_batch(message.text)
    if count is None or count < 1 or count > MAX_BATCH:
        await send_message(
            message, f"Batch count must be a number between 1 and {MAX_BATCH}."
        )
        return

    if count == 1:
        status = await send_message(message, "<i>◷ Generating link…</i>")
        await _reply_links(message, reply, status=status)
        return

    status = await send_message(
        message, f"<i>◷ Processing {count} files…</i>"
    )
    done = failed = 0
    for msg_id in range(reply.id, reply.id + count):
        try:
            msg = await client.get_messages(message.chat.id, msg_id)
            if not msg or msg.empty or not _get_media(msg):
                failed += 1
                continue
            if await _reply_links(message, msg, f"Batch File {done + 1}"):
                done += 1
            else:
                failed += 1
        except Exception as e:
            LOGGER.error(f"FileToLink batch {msg_id}: {e}")
            failed += 1
    await edit_message(
        status,
        "<blockquote><b>◈ BATCH COMPLETE</b></blockquote>\n"
        f"┃ <b>Generated:</b> {done}\n┃ <b>Skipped:</b> {failed}",
    )


def _busy_in_flow(message):
    """True when the user is mid-way through an interactive upload flow
    (thumbnail, rclone config, token…) — those handlers live in group -1
    and pyrogram runs every group, so without this the same file would be
    consumed twice."""
    try:
        from bot.modules import bot_settings, users_settings

        user_id = message.from_user.id if message.from_user else None
        return bool(
            (user_id and users_settings.handler_dict.get(user_id))
            or bot_settings.handler_dict.get(message.chat.id)
        )
    except Exception:
        return False


@new_task
async def auto_file_to_link(_, message):
    """A file sent straight to the bot in PM becomes links automatically."""
    from pyrogram import ContinuePropagation

    if (
        not Config.FILETOLINK_ENABLED
        or not Config.FILETOLINK_AUTO
        or not _get_media(message)
        or not message.from_user
        or _busy_in_flow(message)
    ):
        raise ContinuePropagation

    if not user_data.get(message.from_user.id, {}).get("AUTO_FILETOLINK", True):
        raise ContinuePropagation
    if _preflight():
        raise ContinuePropagation

    status = await send_message(message, "<i>◷ Generating link…</i>")
    await _reply_links(message, message, status=status)
