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


def _auto_style_name(key):
    """Style name configured for this label. Values can be a native
    ButtonStyle name (danger/success/primary/default) or an accent name
    from BUTTON_STYLES (blue/red/green/...). Config.BTN_AUTO_STYLES
    overrides the built-in defaults per label."""
    from bot.core.config_manager import Config

    mapping = getattr(Config, "BTN_AUTO_STYLES", None)
    if not isinstance(mapping, dict):
        mapping = {}
    return mapping.get(str(key).strip().lower()) or _DEFAULT_AUTO_STYLES.get(
        str(key).strip().lower()
    )


def _resolve(key, style):
    """Returns (label, native_style) for one button.

    Priority: explicit style= at the call site → per-label auto style from
    Config.BTN_AUTO_STYLES (or built-in defaults) → global BUTTON_STYLE
    accent. Native colors (PRIMARY/DANGER/SUCCESS) only render when
    COLORED_BTNS is on; accent names decorate the label everywhere."""
    from bot.core.config_manager import Config

    if style is not None and getattr(Config, "COLORED_BTNS", False):
        return str(key), style

    auto = _auto_style_name(key)
    if auto:
        auto_l = str(auto).lower()
        if auto_l in ("danger", "success", "primary", "default"):
            native = getattr(ButtonStyle, auto_l.upper(), None)
            if native is not None and getattr(Config, "COLORED_BTNS", False):
                return str(key), native
        acc = BUTTON_STYLES.get(auto_l)
        if acc and (acc[0] or acc[1]):
            text = str(key)
            first = text[:1]
            if not first or (not first.isalnum() and first not in "([<#/"):
                return text, ButtonStyle.DEFAULT
            return f"{acc[0]}{text}{acc[1]}", ButtonStyle.DEFAULT

    return _decorate(key), ButtonStyle.DEFAULT


_DEFAULT_AUTO_STYLES = {
    "close": "danger",
    "cancel": "danger",
    "stop": "danger",
    "delete": "danger",
    "✕ delete": "danger",
    "yes!": "success",
    "confirm": "success",
    "ok": "success",
    "start": "success",
    "back": "primary",
    "refresh": "primary",
    "next": "primary",
    "previous": "primary",
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
