# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)
#
# /tokengen — hands the user a private, expiring link to the browser
# OAuth flow that produces their personal Google Drive token.pickle.

from urllib.parse import urlencode

from bot import user_data
from bot.core.config_manager import Config
from bot.helper.ext_utils.bot_utils import new_task
from bot.helper.telegram_helper.button_build import ButtonMaker
from bot.helper.telegram_helper.message_utils import send_message


@new_task
async def token_generator(_, message):
    user = message.from_user
    if user is None:
        await send_message(message, "Run <code>/tokengen</code> from a user account.")
        return

    if not Config.BASE_URL:
        await send_message(
            message,
            "<blockquote><b>✕ Token Generator Unavailable</b></blockquote>\n"
            "┃ <code>BASE_URL</code> is not configured.",
        )
        return
    if not Config.DATABASE_URL:
        await send_message(
            message,
            "<blockquote><b>✕ Token Generator Unavailable</b></blockquote>\n"
            "┃ <code>DATABASE_URL</code> is required to store your token.",
        )
        return

    from web.security import (
        PURPOSE_GOOGLE,
        TOKEN_PAGE_TTL_SECONDS,
        make_signed_token,
    )

    token = make_signed_token(PURPOSE_GOOGLE, user.id)
    query = urlencode({"user_id": user.id, "token": token})
    url = f"{Config.BASE_URL.rstrip('/')}/app/token-generator?{query}"

    has_token = bool(user_data.get(user.id, {}).get("TOKEN_PICKLE"))
    status = "replace your existing token" if has_token else "create your token"

    buttons = ButtonMaker()
    buttons.url_button("Open Token Generator", url)

    await send_message(
        message,
        "<blockquote><b>◈ GOOGLE TOKEN GENERATOR</b></blockquote>\n"
        f"┃ Open the page to {status}.\n"
        "┃ Bring your own Google OAuth client — upload\n"
        "┃ <code>credentials.json</code> or paste its ID and secret.\n"
        "┃ The token is stored privately for your Drive uploads.\n\n"
        f"✦ <i>Link is personal and expires in "
        f"{TOKEN_PAGE_TTL_SECONDS // 60} minutes.</i>",
        buttons.build_menu(1),
    )
