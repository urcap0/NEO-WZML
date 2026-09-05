# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)

from uvloop import install

install()

from asyncio import sleep, to_thread
from hashlib import blake2b
from urllib.parse import quote, urlparse
from contextlib import asynccontextmanager
from logging import INFO, WARNING, FileHandler, StreamHandler, basicConfig, getLogger

from aioaria2 import Aria2HttpClient
from aiohttp.client_exceptions import ClientError
from aioqbt.client import create_client
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from aioqbt.exc import AQError

from web.nodes import (
    extract_file_ids,
    make_tree,
    make_mega_tree,
    make_terabox_tree,
    make_rclone_tree,
)
from web.mega_selection_store import (
    get_file_list as get_mega_file_list,
    update_selected_ids as set_mega_selected_ids,
)
from web.terabox_selection_store import (
    get_file_list as get_terabox_file_list,
    update_selected_ids as set_terabox_selected_ids,
)
from web.rclone_selection_store import (
    get_file_list as get_rclone_file_list,
    update_selected_ids as set_rclone_selected_ids,
)
from aiohttp import ClientSession

# the web server runs as its own gunicorn process, so it loads config
# itself (FileToLink needs BOT_TOKEN/HELPER_TOKENS and the bin chat)
from bot.core.config_manager import Config

try:
    Config.load()
except Exception:
    # missing/incomplete config only disables FileToLink; the rest of
    # the web UI (torrent/file selection) must still come up
    pass

getLogger("httpx").setLevel(WARNING)
getLogger("aiohttp").setLevel(WARNING)


def _derive_web_pin(token):
    digits = "".join(n for n in str(token) if n.isdigit())
    if len(digits) >= 4:
        return digits[:4]
    h = blake2b(str(token).encode("utf-8"), digest_size=4).hexdigest()
    return "".join(c for c in h if c.isdigit())[:4].zfill(4)

aria2 = None
qbittorrent = None
proxy_session: ClientSession | None = None
import os as _os
import secrets as _secrets

SERVICES = {
    "qbit": {
        "url": _os.environ.get("QBIT_WEB_URL", "http://localhost:8090"),
        "password": _os.environ.get("QBIT_WEB_PASSWORD") or _secrets.token_urlsafe(16),
    },
}


async def _load_db_config():
    """Mirror the bot's runtime config into this process.

    The bot treats MongoDB as the source of truth (see bot/core/startup.py),
    but this gunicorn process only loads config.py/env via Config.load().
    Pull the saved settings document over as well so FileToLink sees
    FILETOLINK_CHAT etc. that were set from /bsetting.

    Best-effort: the web UI must still come up when the DB is unreachable.
    """
    db_url = (Config.DATABASE_URL or "").strip()
    if not db_url or not Config.BOT_TOKEN:
        return
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        from pymongo.server_api import ServerApi

        client = AsyncIOMotorClient(
            db_url, server_api=ServerApi("1"), serverSelectionTimeoutMS=5000
        )
        bot_id = Config.BOT_TOKEN.split(":", 1)[0]
        if doc := await client.neowzml.settings.config.find_one({"_id": bot_id}):
            Config.load_dict(doc)
            LOGGER.info("Web server loaded config from MongoDB")
        await client.close()
    except Exception as e:
        LOGGER.warning(f"_load_db_config: MongoDB config skipped: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global aria2, qbittorrent, proxy_session
    await _load_db_config()
    aria2 = Aria2HttpClient("http://localhost:6800/jsonrpc")
    qbittorrent = await create_client(
        SERVICES["qbit"]["url"].rstrip("/") + "/api/v2/"
    )
    proxy_session = ClientSession(auto_decompress=True)
    yield
    await aria2.close()
    await qbittorrent.close()
    if proxy_session is not None:
        await proxy_session.close()
    from web.streamer import StreamClients

    await StreamClients.stop()


app = FastAPI(lifespan=lifespan)

from os import path as _ospath

from fastapi.staticfiles import StaticFiles

if _ospath.isdir("web/static"):
    app.mount("/static", StaticFiles(directory="web/static"), name="static")


templates = Jinja2Templates(directory="web/templates/")

basicConfig(
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
    datefmt="%d-%b-%y %I:%M:%S %p",
    handlers=[FileHandler("log.txt"), StreamHandler()],
    level=INFO,
)

LOGGER = getLogger(__name__)


async def re_verify(paused, resumed, hash_id):
    k = 0
    while True:
        res = await qbittorrent.torrents.files(hash_id)
        verify = True
        for i in res:
            if i.index in paused and i.priority != 0:
                verify = False
                break
            if i.index in resumed and i.priority == 0:
                verify = False
                break
        if verify:
            break
        LOGGER.info("Reverification Failed! Correcting stuff...")
        await sleep(0.5)
        if paused:
            try:
                await qbittorrent.torrents.file_prio(
                    hash=hash_id, id=paused, priority=0
                )
            except (ClientError, TimeoutError, Exception, AQError) as e:
                LOGGER.error(f"{e} Errored in reverification paused!")
        if resumed:
            try:
                await qbittorrent.torrents.file_prio(
                    hash=hash_id, id=resumed, priority=1
                )
            except (ClientError, TimeoutError, Exception, AQError) as e:
                LOGGER.error(f"{e} Errored in reverification resumed!")
        k += 1
        if k > 5:
            return False
    LOGGER.info(f"Verified! Hash: {hash_id}")
    return True


@app.get("/app/files", response_class=HTMLResponse)
async def files(request: Request):
    return templates.TemplateResponse(request, "page.html")


@app.api_route(
    "/app/files/torrent", methods=["GET", "POST"], response_class=HTMLResponse
)
async def handle_torrent(request: Request):
    params = request.query_params

    if not (gid := params.get("gid")):
        return JSONResponse(
            {
                "files": [],
                "engine": "",
                "error": "GID is missing",
                "message": "GID not specified",
            }
        )

    if not (pin := params.get("pin")):
        return JSONResponse(
            {
                "files": [],
                "engine": "",
                "error": "Pin is missing",
                "message": "PIN not specified",
            }
        )

    code = _derive_web_pin(gid)
    if len(code) < 4 or code != pin:
        return JSONResponse(
            {
                "files": [],
                "engine": "",
                "error": "Invalid pin",
                "message": "The PIN you entered is incorrect",
            }
        )

    if request.method == "POST":
        if not (mode := params.get("mode")):
            return JSONResponse(
                {
                    "files": [],
                    "engine": "",
                    "error": "Mode is not specified",
                    "message": "Mode is not specified",
                }
            )
        data = await request.json()
        if mode == "rename":
            if len(gid) > 20:
                await handle_rename(gid, data)
                content = {
                    "files": [],
                    "engine": "",
                    "error": "",
                    "message": "Rename successfully.",
                }
            else:
                content = {
                    "files": [],
                    "engine": "",
                    "error": "Rename failed.",
                    "message": "Cannot rename aria2c torrent file",
                }
        else:
            selected_files, unselected_files = extract_file_ids(data)
            if len(gid) > 20:
                await set_qbittorrent(gid, selected_files, unselected_files)
            else:
                selected_files = ",".join(selected_files)
                await set_aria2(gid, selected_files)
            content = {
                "files": [],
                "engine": "",
                "error": "",
                "message": "Your selection has been submitted successfully.",
            }
    else:
        try:
            if len(gid) > 20:
                res = await qbittorrent.torrents.files(gid)
                content = make_tree(res, "qbittorrent")
            else:
                res = await aria2.getFiles(gid)
                op = await aria2.getOption(gid)
                fpath = f"{op['dir']}/"
                content = make_tree(res, "aria2", fpath)
        except (ClientError, TimeoutError, Exception, AQError) as e:
            LOGGER.error(str(e))
            content = {
                "files": [],
                "engine": "",
                "error": "Error getting files",
                "message": str(e),
            }
    return JSONResponse(content)


@app.api_route(
    "/app/files/mega", methods=["GET", "POST"], response_class=HTMLResponse
)
async def handle_mega(request: Request):
    params = request.query_params
    gid_raw = params.get("gid", "")

    if not gid_raw:
        return JSONResponse({
            "files": [], "engine": "", "error": "GID is missing",
            "message": "GID not specified",
        })

    if not (pin := params.get("pin")):
        return JSONResponse({
            "files": [], "engine": "", "error": "Pin is missing",
            "message": "PIN not specified",
        })

    gid = gid_raw.replace("mega_", "", 1) if gid_raw.startswith("mega_") else gid_raw
    code = _derive_web_pin(gid_raw)
    if len(code) < 4 or code != pin:
        return JSONResponse({
            "files": [], "engine": "", "error": "Invalid pin",
            "message": "The PIN you entered is incorrect",
        })

    if request.method == "POST":
        data = await request.json()
        selected_files, _ = extract_file_ids(data)
        ok = await to_thread(set_mega_selected_ids, gid, set(selected_files))
        return JSONResponse({
            "files": [], "engine": "", "error": "" if ok else "GID not found",
            "message": "Selection submitted" if ok else "Task expired",
        })
    else:
        file_list = await to_thread(get_mega_file_list, gid)
        if file_list is None:
            return JSONResponse({
                "files": [], "engine": "", "error": "Not found",
                "message": "Task not found or expired",
            })
        content = make_mega_tree(file_list)
        return JSONResponse(content)


@app.api_route(
    "/app/files/terabox", methods=["GET", "POST"], response_class=HTMLResponse
)
async def handle_terabox(request: Request):
    params = request.query_params
    gid_raw = params.get("gid", "")

    if not gid_raw:
        return JSONResponse({
            "files": [], "engine": "", "error": "GID is missing",
            "message": "GID not specified",
        })

    if not (pin := params.get("pin")):
        return JSONResponse({
            "files": [], "engine": "", "error": "Pin is missing",
            "message": "PIN not specified",
        })

    gid = gid_raw.replace("terabox_", "", 1) if gid_raw.startswith("terabox_") else gid_raw
    code = _derive_web_pin(gid_raw)
    if len(code) < 4 or code != pin:
        return JSONResponse({
            "files": [], "engine": "", "error": "Invalid pin",
            "message": "The PIN you entered is incorrect",
        })

    if request.method == "POST":
        data = await request.json()
        selected_files, _ = extract_file_ids(data)
        ok = await to_thread(set_terabox_selected_ids, gid, set(selected_files))
        return JSONResponse({
            "files": [], "engine": "", "error": "" if ok else "GID not found",
            "message": "Selection submitted" if ok else "Task expired",
        })
    else:
        file_list = await to_thread(get_terabox_file_list, gid)
        if file_list is None:
            return JSONResponse({
                "files": [], "engine": "", "error": "Not found",
                "message": "Task not found or expired",
            })
        content = make_terabox_tree(file_list)
        return JSONResponse(content)


@app.api_route(
    "/app/files/rclone", methods=["GET", "POST"], response_class=HTMLResponse
)
async def handle_rclone(request: Request):
    params = request.query_params
    gid_raw = params.get("gid", "")

    if not gid_raw:
        return JSONResponse({
            "files": [], "engine": "", "error": "GID is missing",
            "message": "GID not specified",
        })

    if not (pin := params.get("pin")):
        return JSONResponse({
            "files": [], "engine": "", "error": "Pin is missing",
            "message": "PIN not specified",
        })

    gid = gid_raw.replace("rclone_", "", 1) if gid_raw.startswith("rclone_") else gid_raw
    code = _derive_web_pin(gid_raw)
    if len(code) < 4 or code != pin:
        return JSONResponse({
            "files": [], "engine": "", "error": "Invalid pin",
            "message": "The PIN you entered is incorrect",
        })

    if request.method == "POST":
        data = await request.json()
        selected_files, _ = extract_file_ids(data)
        ok = await to_thread(set_rclone_selected_ids, gid, set(selected_files))
        return JSONResponse({
            "files": [], "engine": "", "error": "" if ok else "GID not found",
            "message": "Selection submitted" if ok else "Task expired",
        })
    else:
        file_list = await to_thread(get_rclone_file_list, gid)
        if file_list is None:
            return JSONResponse({
                "files": [], "engine": "", "error": "Not found",
                "message": "Task not found or expired",
            })
        content = make_rclone_tree(file_list)
        return JSONResponse(content)


async def handle_rename(gid, data):
    try:
        _type = data["type"]
        del data["type"]
        if _type == "file":
            await qbittorrent.torrents.rename_file(hash=gid, **data)
        else:
            await qbittorrent.torrents.rename_folder(hash=gid, **data)
    except (ClientError, TimeoutError, Exception, AQError) as e:
        LOGGER.error(f"{e} Errored in renaming")


async def set_qbittorrent(gid, selected_files, unselected_files):
    if unselected_files:
        try:
            await qbittorrent.torrents.file_prio(
                hash=gid, id=unselected_files, priority=0
            )
        except (ClientError, TimeoutError, Exception, AQError) as e:
            LOGGER.error(f"{e} Errored in paused")
    if selected_files:
        try:
            await qbittorrent.torrents.file_prio(
                hash=gid, id=selected_files, priority=1
            )
        except (ClientError, TimeoutError, Exception, AQError) as e:
            LOGGER.error(f"{e} Errored in resumed")
    await sleep(0.5)
    if not await re_verify(unselected_files, selected_files, gid):
        LOGGER.error(f"Verification Failed! Hash: {gid}")


async def set_aria2(gid, selected_files):
    res = await aria2.changeOption(gid, {"select-file": selected_files})
    if res == "OK":
        LOGGER.info(f"Verified! Gid: {gid}")
    else:
        LOGGER.info(f"Verification Failed! Report! Gid: {gid}")


@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    return templates.TemplateResponse(request, "landing.html")


def rewrite_location(location: str, proxy_prefix: str) -> str:
    parsed = urlparse(location)
    if not parsed.netloc:
        return proxy_prefix + location
    if parsed.hostname in ["localhost", "127.0.0.1"]:
        return proxy_prefix + parsed.path
    return location


async def proxy_fetch(
    method: str, url: str, headers: dict, params: dict, body: bytes, proxy_prefix: str
):
    session = proxy_session or ClientSession(auto_decompress=True)
    async with session.request(
        method,
        url,
        headers=headers,
        params=params,
        data=body,
        allow_redirects=False,
    ) as upstream:
        if upstream.status in (301, 302, 303, 307, 308) and upstream.headers.get(
            "Location"
        ):
            loc = upstream.headers["Location"]
            new_loc = rewrite_location(loc, proxy_prefix)
            return HTMLResponse(
                status_code=upstream.status, headers={"Location": new_loc}
            )
        content = await upstream.read()
        media_type = upstream.headers.get("Content-Type", "text/html")
        resp_headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower() not in ["content-length", "content-encoding"]
        }
        return HTMLResponse(
            content=content,
            status_code=upstream.status,
            headers=resp_headers,
            media_type=media_type,
        )


async def protected_proxy(
    service: str, path: str, request: Request, password: str = None
):
    service_info = SERVICES.get(service)
    if not service_info:
        raise HTTPException(status_code=404, detail="Service not found")
    if "password" in service_info and password != service_info["password"]:
        raise HTTPException(status_code=403, detail="Unauthorized access")
    base = service_info["url"]
    url = f"{base}/{path}" if path else base
    headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    body = await request.body()
    return await proxy_fetch(
        request.method, url, headers, dict(request.query_params), body, f"/{service}"
    )


@app.api_route("/qbit/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def qbittorrent_proxy(path: str = "", request: Request = None):
    password = request.query_params.get("pass") or request.cookies.get("qbit_pass")
    if not password:
        raise HTTPException(status_code=403, detail="Missing password")
    response = await protected_proxy("qbit", path, request, password)
    if "pass" in request.query_params:
        response.set_cookie("qbit_pass", password)
    return response


# ─────────────────────── Google token generator ───────────────────
#
# The user brings their own OAuth client (upload credentials.json or
# paste id/secret), so the bot host needs no credentials.json. An owner
# client, when configured, is offered as a one-click default.


def _token_page(request, **ctx):
    ctx.setdefault("title", "Google Token Generator")
    return templates.TemplateResponse(request, "token_generator.html", ctx)


def _token_auth_ok(user_id, token):
    from web.security import PURPOSE_GOOGLE, verify_signed_token

    return user_id.isdigit() and verify_signed_token(PURPOSE_GOOGLE, user_id, token)


@app.get("/app/token-generator", response_class=HTMLResponse)
async def token_generator_page(request: Request, user_id: str = "", token: str = ""):
    from web.token_gen import host_credentials, redirect_uri

    if not _token_auth_ok(user_id, token):
        return _token_page(
            request,
            state="error",
            message="This link is invalid or has expired. Run /tokengen again.",
        )
    if not Config.BASE_URL:
        return _token_page(
            request, state="error", message="BASE_URL is not configured."
        )

    host_id, _ = host_credentials()
    return _token_page(
        request,
        state="form",
        user_id=user_id,
        token=token,
        has_host_client=bool(host_id),
        redirect_uri=redirect_uri(),
    )


@app.post("/app/token-generator", response_class=HTMLResponse)
async def token_generator_start(
    request: Request,
    user_id: str = Form(""),
    token: str = Form(""),
    mode: str = Form("own"),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    credentials_file: UploadFile = File(None),
):
    from web.token_gen import (
        host_credentials,
        parse_client_json,
        redirect_uri,
        authorization_url,
        stash_client,
        validate_client,
    )

    if not _token_auth_ok(user_id, token):
        return _token_page(
            request,
            state="error",
            message="This link is invalid or has expired. Run /tokengen again.",
        )

    def back(err):
        host_id, _ = host_credentials()
        return _token_page(
            request,
            state="form",
            user_id=user_id,
            token=token,
            has_host_client=bool(host_id),
            redirect_uri=redirect_uri(),
            form_error=err,
        )

    try:
        if mode == "host":
            cid, secret = host_credentials()
            if not cid:
                return back("The owner hasn't configured a shared Google client.")
        elif credentials_file is not None and credentials_file.filename:
            raw = (await credentials_file.read(64 * 1024 + 1)).decode(
                "utf-8", "replace"
            )
            cid, secret = parse_client_json(raw)
        elif client_id or client_secret:
            cid, secret = validate_client(client_id, client_secret)
        else:
            return back(
                "Upload your credentials.json, or paste the client ID and secret."
            )
    except ValueError as e:
        return back(str(e))
    except Exception as e:
        LOGGER.error(f"TokenGen: bad client input: {e}")
        return back("Couldn't read that credentials file.")

    nonce = stash_client(user_id, cid, secret)
    return RedirectResponse(authorization_url(cid, nonce), status_code=303)


@app.get("/app/token-generator/callback", response_class=HTMLResponse)
async def token_generator_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
):
    from web.token_gen import exchange_code, store_token, take_client

    if error:
        return _token_page(request, state="error", message=f"Google returned: {error}")
    if not state:
        return _token_page(
            request, state="error", message="Google returned no state value."
        )

    # the nonce identifies both the pending client and its owner
    from web.token_gen import _PENDING, _sweep

    _sweep()
    entry = _PENDING.get(state)
    if not entry:
        return _token_page(
            request,
            state="error",
            message="This sign-in expired or was already used. Run /tokengen again.",
        )
    user_id = entry["user_id"]
    cid, secret = take_client(state, user_id)
    if not cid:
        return _token_page(
            request,
            state="error",
            message="This sign-in expired or was already used. Run /tokengen again.",
        )
    if not code:
        return _token_page(
            request, state="error", message="No authorization code returned."
        )

    try:
        token_bytes = await exchange_code(code, cid, secret)
        await store_token(user_id, token_bytes)
    except Exception as e:
        LOGGER.error(f"TokenGen: exchange failed for {user_id}: {e}")
        return _token_page(request, state="error", message=str(e))

    LOGGER.info(f"TokenGen: stored token.pickle for user {user_id}")
    return _token_page(request, state="done", user_id=user_id)


# ─────────────────────────── FileToLink ───────────────────────────


async def _stream_response(message_id: int, sig: str, request: Request, as_attachment):
    from fastapi.responses import StreamingResponse

    from web.streamer import (
        ByteStreamer,
        NoStreamClients,
        StreamClients,
        bin_chat,
        range_params,
        verify,
    )

    chat_id = bin_chat()
    if not chat_id:
        raise HTTPException(status_code=503, detail="FileToLink is not configured")

    if not verify(chat_id, message_id, sig):
        raise HTTPException(status_code=403, detail="Invalid or expired link")

    await StreamClients.start()
    try:
        index, client = StreamClients.pick()
    except NoStreamClients:
        raise HTTPException(
            status_code=503, detail="No streaming client available — check bot tokens"
        )
    streamer = ByteStreamer(client, index)

    try:
        file_id, file_size, file_name, mime_type = await streamer.get_properties(
            chat_id, message_id
        )
    except FileNotFoundError as e:
        StreamClients.release(index)
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        StreamClients.release(index)
        LOGGER.error(f"FileToLink properties error: {e}")
        raise HTTPException(status_code=502, detail="Unable to read file")

    if not file_size:
        StreamClients.release(index)
        raise HTTPException(status_code=404, detail="Empty file")

    range_header = request.headers.get("Range")
    start, end = 0, file_size - 1
    partial = False
    if range_header:
        try:
            raw_range = range_header.replace("bytes=", "").strip().split("-", 1)
            first, last = raw_range[0].strip(), (
                raw_range[1].strip() if len(raw_range) > 1 else ""
            )
            if not first:
                # suffix form "bytes=-N" — the LAST n bytes, not the whole file
                if not last:
                    raise ValueError("empty range")
                start = max(0, file_size - int(last))
                end = file_size - 1
            else:
                start = int(first)
                end = int(last) if last else end
            partial = True
        except ValueError:
            StreamClients.release(index)
            raise HTTPException(status_code=416, detail="Malformed Range header")
    if start < 0 or end >= file_size or start > end:
        StreamClients.release(index)
        return JSONResponse(
            {"error": "Requested range not satisfiable"},
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    offset, first_cut, last_cut, part_count = range_params(start, end, file_size)
    disposition = "attachment" if as_attachment else "inline"
    safe_name = quote(file_name)
    headers = {
        "Content-Type": mime_type,
        "Content-Length": str(end - start + 1),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'{disposition}; filename="{safe_name}"',
        "Cache-Control": "public, max-age=3600",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    # StreamingResponse always drains its iterator, so a HEAD would pull
    # the whole file from Telegram just to discard it — answer with
    # headers only.
    if request.method == "HEAD":
        StreamClients.release(index)
        return Response(
            status_code=206 if partial else 200,
            headers=headers,
            media_type=mime_type,
        )

    async def body():
        try:
            async for chunk in streamer.yield_file(
                file_id, offset, first_cut, last_cut, part_count
            ):
                yield chunk
        except Exception as e:
            # headers already promised Content-Length; log loudly so a
            # truncated transfer isn't mistaken for a clean one
            LOGGER.error(
                f"FileToLink stream aborted for {message_id} "
                f"(bytes {start}-{end}): {e}"
            )
            streamer.invalidate(chat_id, message_id)
        finally:
            StreamClients.release(index)

    return StreamingResponse(
        body(),
        status_code=206 if partial else 200,
        headers=headers,
        media_type=mime_type,
    )


@app.get("/api/filetolink/status")
async def filetolink_status():
    from web.streamer import ByteStreamer, StreamClients

    return JSONResponse(
        {
            "clients": len(StreamClients.loads()),
            "loads": StreamClients.loads(),
            "cached": len(ByteStreamer._props_cache),
            "sessions": len(ByteStreamer._sessions),
        }
    )


@app.head("/stream/{message_id}/{sig}")
@app.get("/stream/{message_id}/{sig}")
async def stream_media(message_id: int, sig: str, request: Request):
    return await _stream_response(message_id, sig, request, as_attachment=False)


@app.head("/dl/{message_id}/{sig}")
@app.get("/dl/{message_id}/{sig}")
async def download_media(message_id: int, sig: str, request: Request):
    return await _stream_response(message_id, sig, request, as_attachment=True)


@app.get("/watch/{message_id}/{sig}", response_class=HTMLResponse)
async def watch_media(message_id: int, sig: str, request: Request):
    from web.streamer import (
        ByteStreamer,
        NoStreamClients,
        StreamClients,
        bin_chat,
        verify,
    )

    chat_id = bin_chat()
    if not chat_id:
        raise HTTPException(status_code=503, detail="FileToLink is not configured")
    if not verify(chat_id, message_id, sig):
        raise HTTPException(status_code=403, detail="Invalid or expired link")

    await StreamClients.start()
    try:
        index, client = StreamClients.pick()
    except NoStreamClients:
        raise HTTPException(status_code=503, detail="No streaming client available")
    streamer = ByteStreamer(client, index)
    try:
        _, file_size, file_name, mime_type = await streamer.get_properties(
            chat_id, message_id
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        StreamClients.release(index)

    return templates.TemplateResponse(
        request,
        "player.html",
        {
            "file_name": file_name,
            "file_size": file_size,
            "mime_type": mime_type,
            "stream_url": f"/stream/{message_id}/{sig}",
            "download_url": f"/dl/{message_id}/{sig}",
            "is_video": mime_type.startswith("video/"),
            "is_audio": mime_type.startswith("audio/"),
        },
    )


@app.exception_handler(Exception)
async def page_not_found(_, exc):
    LOGGER.error("Unhandled web exception: %s: %s", type(exc).__name__, exc, exc_info=True)
    return HTMLResponse(
        "<h1>404: Task not found! Mostly wrong input.</h1>",
        status_code=404,
    )
