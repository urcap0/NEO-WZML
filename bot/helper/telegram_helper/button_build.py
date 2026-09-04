# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)

from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Telegram inline buttons can't be truly colored on standard pyrogram,
# so "button color" is an accent decoration applied to the label. Each
# style maps to (prefix, suffix) wrapped around the button text.
# "none" = plain (default look).
BUTTON_STYLES = {
    "none": ("", ""),
    "blue": ("🔵 ", ""),
    "red": ("🔴 ", ""),
    "green": ("🟢 ", ""),
    "pink": ("🩷 ", ""),
    "purple": ("🟣 ", ""),
    "cyan": ("🔷 ", ""),
    "orange": ("🟠 ", ""),
    "yellow": ("🟡 ", ""),
}


def _decorate(key):
    from bot.core.config_manager import Config

    prefix, suffix = BUTTON_STYLES.get(
        getattr(Config, "BUTTON_STYLE", "none") or "none", ("", "")
    )
    return f"{prefix}{key}{suffix}"


def _resolve(key, style):
    """Returns (label, native_style). native_style is None unless
    COLORED_BTNS is enabled — so plain pyroblack installs never receive
    the wzgram-only style kwarg.

    With COLORED_BTNS on, well-known labels get an automatic style when
    the call site passed none: Close/Cancel/Stop/Delete → DANGER,
    Yes/Confirm/Ok → SUCCESS, Back/Refresh/Next → PRIMARY."""
    from bot.core.config_manager import Config

    if style is not None and getattr(Config, "COLORED_BTNS", False):
        return str(key), style
    if getattr(Config, "COLORED_BTNS", False):
        auto = _AUTO_STYLE.get(str(key).strip().lower())
        if auto is not None:
            return str(key), auto
    return _decorate(key), None


_AUTO_STYLE = {
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
        kwargs = {"text": label, "url": link}
        if native_style is not None:
            kwargs["style"] = native_style
        if icon:
            kwargs["icon_custom_emoji_id"] = icon
        self.buttons[position if position in self.buttons else "default"].append(
            InlineKeyboardButton(**kwargs)
        )

    def data_button(self, key, data, position=None, style=None, premium_icon=False):
        label, native_style = _resolve(key, style)
        icon = _premium_icon() if premium_icon else None
        kwargs = {"text": label, "callback_data": data}
        if native_style is not None:
            kwargs["style"] = native_style
        if icon:
            kwargs["icon_custom_emoji_id"] = icon
        self.buttons[position if position in self.buttons else "default"].append(
            InlineKeyboardButton(**kwargs)
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
