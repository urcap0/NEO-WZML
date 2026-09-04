# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)
#
# Signed, expiring tokens shared by the bot process and the web process.
# Both derive the same key from BOT_TOKEN, so a link minted in chat can be
# verified by the web server without any shared state.

from base64 import urlsafe_b64decode, urlsafe_b64encode
from hashlib import sha256
from hmac import compare_digest, new as hmac_new
from time import time

from bot.core.config_manager import Config

TOKEN_PAGE_TTL_SECONDS = 15 * 60
PURPOSE_GOOGLE = "google-token"


def _key(purpose):
    return sha256(
        (Config.BOT_TOKEN or "neo-wzml").encode() + b"|" + purpose.encode()
    ).digest()


def _b64(raw):
    return urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text):
    pad = "=" * (-len(text) % 4)
    return urlsafe_b64decode(text + pad)


def make_signed_token(purpose, user_id, ttl=TOKEN_PAGE_TTL_SECONDS):
    """Token binding a purpose + user + expiry, as '<exp>.<sig>'."""
    exp = int(time()) + int(ttl)
    payload = f"{purpose}:{user_id}:{exp}".encode()
    sig = _b64(hmac_new(_key(purpose), payload, sha256).digest()[:18])
    return f"{exp}.{sig}"


def verify_signed_token(purpose, user_id, token):
    """True only for an untampered, unexpired token for this user."""
    try:
        exp_str, sig = str(token).split(".", 1)
        exp = int(exp_str)
    except (ValueError, AttributeError):
        return False
    if exp < time():
        return False
    payload = f"{purpose}:{user_id}:{exp}".encode()
    expected = _b64(hmac_new(_key(purpose), payload, sha256).digest()[:18])
    try:
        return compare_digest(expected, sig)
    except Exception:
        return False


def pack_state(purpose, user_id, token):
    """OAuth `state`: carries the identity through Google's redirect."""
    return _b64(f"{purpose}|{user_id}|{token}".encode())


def unpack_state(state):
    try:
        purpose, user_id, token = _unb64(str(state)).decode().split("|", 2)
        return purpose, int(user_id), token
    except Exception:
        return None, None, None
