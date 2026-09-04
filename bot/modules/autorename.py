# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)
#
# /autorename — manage a per-user auto-rename template that is applied to
# every leeched/mirrored file (see filename_utils.format_filename).

from bot import user_data
from bot.helper.ext_utils.bot_utils import new_task, update_user_ldata
from bot.helper.ext_utils.db_handler import database
from bot.helper.ext_utils.autorename_utils import apply_autorename_template
from bot.helper.telegram_helper.message_utils import send_message

_HELP = (
    "<b>❖ Auto-Rename</b>\n"
    "Rebuilds every uploaded filename from a template.\n\n"
    "<b>Placeholders:</b> <code>{title}</code> <code>{season}</code> "
    "<code>{episode}</code> <code>{quality}</code> <code>{year}</code> "
    "(<code>{season_raw}</code>/<code>{episode_raw}</code> = unpadded)\n\n"
    "<b>Usage:</b>\n"
    "• <code>/autorename &lt;template&gt;</code> — set your template\n"
    "• <code>/autorename</code> — show current template\n"
    "• <code>/autorename off</code> — disable\n\n"
    "<b>Example:</b>\n"
    "<code>/autorename [MyGroup] {title} - S{season}E{episode} [{quality}]</code>"
)


@new_task
async def auto_rename(_, message):
    if message.from_user is None:
        await send_message(message, "Run <code>/autorename</code> from a user account.")
        return
    user_id = message.from_user.id
    text = message.text.split(maxsplit=1)
    arg = text[1].strip() if len(text) > 1 else ""

    if not arg:
        current = user_data.get(user_id, {}).get("AUTO_RENAME", "")
        if not current:
            await send_message(message, _HELP)
            return
        preview = apply_autorename_template(
            "The.Show.S01E04.1080p.WEB-DL.x265-Group.mkv", current
        )
        await send_message(
            message,
            f"<b>Your template:</b>\n<code>{current}</code>\n\n"
            f"<b>Sample:</b> <code>{preview}</code>\n\n"
            "Send <code>/autorename off</code> to disable.",
        )
        return

    if arg.lower() in ("off", "disable", "none", "clear"):
        update_user_ldata(user_id, "AUTO_RENAME", "")
        await database.update_user_data(user_id)
        await send_message(message, "❌ Auto-rename disabled.")
        return

    update_user_ldata(user_id, "AUTO_RENAME", arg)
    await database.update_user_data(user_id)
    preview = apply_autorename_template(
        "The.Show.S01E04.1080p.WEB-DL.x265-Group.mkv", arg
    )
    await send_message(
        message,
        f"✅ <b>Auto-rename set.</b>\n<b>Template:</b> <code>{arg}</code>\n"
        f"<b>Sample:</b> <code>{preview}</code>",
    )
