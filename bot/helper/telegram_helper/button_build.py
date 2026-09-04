# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)

from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# wzgram (the pyrogram replacement this bot is pinned to — see
# requirements.txt) supports genuine colored inline buttons via
# InlineKeyboardButton(style=...). When Config.COLORED_BTNS is on, a
# call site that passes style=ButtonStyle.{PRIMARY,DANGER,SUCCESS} gets a
# real colored button and the old emoji-accent decoration is skipped (the
# two would look redundant stacked together). Any call that doesn't pass
# style, or when COLORED_BTNS is off, behaves exactly as before.
BUTTON_STYLES = {
    "none": ("", ""),
    "blue": ("🔵 ", ""),
    "red": ("🔴 ", ""),
    "green": ("🟢 ", ""),
    "purple": ("🟣 ", ""),
    "orange": ("🟠 ", ""),
    "yellow": ("🟡 ", ""),
    "diamond": ("🔹 ", ""),
    "star": ("✦ ", ""),
    "arrow": ("➤ ", ""),
    "bracket": ("『 ", " 』"),
}


def _decorate(key):
    from bot.core.config_manager import Config

    style = getattr(Config, "BUTTON_STYLE", "") or "none"
    prefix, suffix = BUTTON_STYLES.get(style, ("", ""))
    if not prefix and not suffix:
        return key
    # don't decorate labels that already start with an emoji/symbol accent
    text = str(key)
    # many theme labels already lead with their own emoji (☁️ Cloud,
    # 📨 Save, ⚡ Index…) — stacking a second accent on those looks broken
    first = text[:1]
    if not first or (not first.isalnum() and first not in "([<#/"):
        return text
    return f"{prefix}{text}{suffix}"


def _resolve(key, style):
    """Returns (label, native_style) for one button.

    Priority: explicit style= at the call site → built-in auto style for
    well-known labels (Close/Cancel/Stop/Delete → DANGER, Yes!/Confirm/Ok
    → SUCCESS, Back/Refresh/Next → PRIMARY) → PRIMARY fallback so EVERY
    button is colored. Native colors only render when COLORED_BTNS is on."""
    from bot.core.config_manager import Config

    if style is not None and getattr(Config, "COLORED_BTNS", False):
        return str(key), style
    if getattr(Config, "COLORED_BTNS", False):
        auto = _DEFAULT_AUTO_STYLES.get(str(key).strip().lower())
        return str(key), auto if auto is not None else ButtonStyle.PRIMARY
    return _decorate(key), ButtonStyle.DEFAULT


_DEFAULT_AUTO_STYLES = {
    "close": ButtonStyle.DANGER,
    "cancel": ButtonStyle.DANGER,
    "stop": ButtonStyle.DANGER,
    "delete": ButtonStyle.DANGER,
    "✕ delete": ButtonStyle.DANGER,
    "yes!": ButtonStyle.SUCCESS,
    "confirm": ButtonStyle.SUCCESS,
    "ok": ButtonStyle.SUCCESS,
    "start": ButtonStyle.SUCCESS,
    "back": ButtonStyle.PRIMARY,
    "refresh": ButtonStyle.PRIMARY,
    "next": ButtonStyle.PRIMARY,
    "previous": ButtonStyle.PRIMARY,
}


def _premium_icon():
    """Custom-emoji icon id for a button, or None. Telegram only accepts
    icon_custom_emoji_id from Premium-linked bots — see Config.IS_PREMIUM_BOT
    / PREMIUM_EMOJI_ID — so this stays None (plain button) unless both are
    set."""
    from bot.core.config_manager import Config

    if getattr(Config, "IS_PREMIUM_BOT", False) and getattr(
        Config, "PREMIUM_EMOJI_ID", ""
    ):
        return Config.PREMIUM_EMOJI_ID
    return None


class ButtonMaker:
    def __init__(self):
        self.buttons = {
            "default": [],
            "header": [],
            "f_body": [],
            "l_body": [],
            "footer": [],
        }

    def url_button(self, key, link, position=None, style=None, premium_icon=False):
        label, native_style = _resolve(key, style)
        icon = _premium_icon() if premium_icon else None
        self.buttons[position if position in self.buttons else "default"].append(
            InlineKeyboardButton(
                text=label, url=link, style=native_style, icon_custom_emoji_id=icon
            )
        )

    def data_button(self, key, data, position=None, style=None, premium_icon=False):
        label, native_style = _resolve(key, style)
        icon = _premium_icon() if premium_icon else None
        self.buttons[position if position in self.buttons else "default"].append(
            InlineKeyboardButton(
                text=label,
                callback_data=data,
                style=native_style,
                icon_custom_emoji_id=icon,
            )
        )

    def build_menu(self, b_cols=1, h_cols=8, fb_cols=2, lb_cols=2, f_cols=8):
        def chunk(lst, n):
            return [lst[i : i + n] for i in range(0, len(lst), n)]

        menu = chunk(self.buttons["default"], b_cols)
        menu = (
            chunk(self.buttons["header"], h_cols) if self.buttons["header"] else []
        ) + menu
        for key, cols in (("f_body", fb_cols), ("l_body", lb_cols), ("footer", f_cols)):
            if self.buttons[key]:
                menu += chunk(self.buttons[key], cols)
        return InlineKeyboardMarkup(menu)

    def reset(self):
        for key in self.buttons:
            self.buttons[key].clear()
