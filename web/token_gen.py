# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)
#
# Google Drive token.pickle generator.
#
# The OAuth client is supplied by the USER in the browser — either by
# uploading their credentials.json or pasting client id/secret — so the
# bot host needs no credentials.json of its own and every user ends up
# with a token bound to their own Google Cloud project. The client secret
# never travels through the redirect: it's parked server-side under a
# random nonce that is all the OAuth `state` carries.

from datetime import datetime, timedelta
from json import loads as json_loads
from logging import getLogger
from os import makedirs, path as ospath
from pickle import dumps as pickle_dumps
from secrets import token_urlsafe
from time import time
from urllib.parse import urlencode

from bot.core.config_manager import Config

LOGGER = getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
CREDENTIALS_FILE = "credentials.json"
PURPOSE = "google-token"

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

PENDING_TTL = 20 * 60
# nonce -> {client_id, client_secret, user_id, exp}; the web server is a
# single worker, and a lost entry only means "start /tokengen again"
_PENDING = {}


def redirect_uri():
    return f"{(Config.BASE_URL or '').rstrip('/')}/app/token-generator/callback"


def host_credentials():
    """Optional fallback: an owner-provided client, so users don't have to
    bring their own. Returns (client_id, client_secret) or (None, None)."""
    cid = (getattr(Config, "GOOGLE_CLIENT_ID", "") or "").strip()
    secret = (getattr(Config, "GOOGLE_CLIENT_SECRET", "") or "").strip()
    if cid and secret:
        return cid, secret

    raw = (getattr(Config, "GOOGLE_CREDENTIALS_JSON", "") or "").strip()
    if not raw and ospath.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE) as f:
                raw = f.read()
        except OSError:
            raw = ""
    if raw:
        try:
            return parse_client_json(raw)
        except ValueError:
            return None, None
    return None, None


def parse_client_json(raw):
    """Pull (client_id, client_secret) out of a credentials.json body."""
    if not raw or not raw.strip():
        raise ValueError("Choose a credentials.json file, or paste its contents.")
    if len(raw) > 64 * 1024:
        raise ValueError("That credentials.json is too large.")
    try:
        data = json_loads(raw)
    except ValueError:
        raise ValueError("That file isn't valid JSON.")
    if not isinstance(data, dict):
        raise ValueError("credentials.json must contain a JSON object.")
    section = data.get("web") or data.get("installed")
    if not isinstance(section, dict):
        raise ValueError(
            "credentials.json must hold a Google OAuth client "
            "(a 'web' or 'installed' section)."
        )
    return validate_client(section.get("client_id"), section.get("client_secret"))


def validate_client(client_id, client_secret):
    cid = (client_id or "").strip()
    secret = (client_secret or "").strip()
    if not cid or not secret:
        raise ValueError("Both the client ID and client secret are required.")
    if len(cid) > 512 or len(secret) > 512:
        raise ValueError("Those values are too long to be a Google client.")
    for value in (cid, secret):
        if "\r" in value or "\n" in value or " " in value:
            raise ValueError("The client ID/secret contain invalid characters.")
    if not cid.endswith(".apps.googleusercontent.com"):
        raise ValueError(
            "That doesn't look like a Google client ID — it should end in "
            ".apps.googleusercontent.com"
        )
    return cid, secret


def _sweep():
    now = time()
    for nonce in [n for n, v in _PENDING.items() if v["exp"] < now]:
        _PENDING.pop(nonce, None)


def stash_client(user_id, client_id, client_secret):
    """Park the client server-side; only the nonce goes in the URL."""
    _sweep()
    nonce = token_urlsafe(24)
    _PENDING[nonce] = {
        "user_id": int(user_id),
        "client_id": client_id,
        "client_secret": client_secret,
        "exp": time() + PENDING_TTL,
    }
    return nonce


def take_client(nonce, user_id):
    """One-shot retrieval — a code can only be exchanged once."""
    _sweep()
    entry = _PENDING.pop(nonce, None)
    if not entry or entry["user_id"] != int(user_id):
        return None, None
    return entry["client_id"], entry["client_secret"]


def authorization_url(client_id, state, login_hint=""):
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "select_account consent",  # always return a refresh_token
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{GOOGLE_AUTH_URI}?{urlencode(params)}"


async def exchange_code(code, client_id, client_secret):
    """Swap the authorization code for credentials; returns pickled bytes."""
    from aiohttp import ClientSession

    async with ClientSession() as session:
        async with session.post(
            GOOGLE_TOKEN_URI,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=30,
        ) as resp:
            payload = await resp.json(content_type=None)
            if resp.status != 200:
                raise ValueError(
                    payload.get("error_description")
                    or payload.get("error")
                    or f"Google rejected the request (HTTP {resp.status})."
                )

    if not payload.get("access_token"):
        raise ValueError("Google did not return an access token.")
    if not payload.get("refresh_token"):
        raise ValueError(
            "Google returned no refresh token. Remove this app at "
            "myaccount.google.com/permissions and try again."
        )

    from google.oauth2.credentials import Credentials

    expiry = None
    try:
        if expires_in := int(payload.get("expires_in") or 0):
            # google-auth stores a naive UTC datetime
            expiry = datetime.utcnow() + timedelta(seconds=expires_in)
    except (TypeError, ValueError):
        expiry = None

    creds = Credentials(
        token=payload["access_token"],
        refresh_token=payload["refresh_token"],
        token_uri=GOOGLE_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
        expiry=expiry,
    )
    return pickle_dumps(creds, protocol=4)


async def store_token(user_id, token_bytes):
    """Persist to disk and Mongo the same way user settings does."""
    makedirs("tokens", exist_ok=True)
    path = f"tokens/{user_id}.pickle"
    with open(path, "wb") as f:
        f.write(token_bytes)

    from web.mongo import users_collection

    coll = users_collection()
    if coll is None:
        return path
    await coll.update_one(
        {"_id": user_id}, {"$set": {"TOKEN_PICKLE": token_bytes}}, upsert=True
    )
    return path
