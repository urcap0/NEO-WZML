# This file is a part of NEO-WZML (github.com/irisXDR/NEO-WZML)

from asyncio import sleep, TimeoutError
from aiohttp.client_exceptions import ClientError
from aioaria2.exceptions import Aria2rpcException

from bot import LOGGER
from bot.core.torrent_manager import TorrentManager, aria2_name


class DirectListener:
    def __init__(self, path, listener, a2c_opt):
        self.listener = listener
        self._path = path
        self._a2c_opt = a2c_opt
        self._proc_bytes = 0
        self._failed = 0
        self.download_task = None
        self.name = self.listener.name

    async def _tell_status(self, gid, retries=3):
        last_exc = None
        for attempt in range(retries):
            try:
                return await TorrentManager.aria2.tellStatus(gid)
            except (Aria2rpcException, TimeoutError, ClientError) as e:
                last_exc = e
                LOGGER.warning(
                    f"Aria2 tellStatus failed for {gid} (attempt {attempt + 1}/{retries}): {e}"
                )
                if attempt < retries - 1:
                    await sleep(2)
        raise last_exc

    @property
    def processed_bytes(self):
        if self.download_task:
            return self._proc_bytes + int(
                self.download_task.get("completedLength", "0")
            )
        return self._proc_bytes

    @property
    def speed(self):
        return (
            int(self.download_task.get("downloadSpeed", "0"))
            if self.download_task
            else 0
        )

    async def download(self, contents):
        self.is_downloading = True
        if not contents:
            await self.listener.on_download_error(
                "There is nothing to download — no resolvable links were found."
            )
            return
        for content in contents:
            if self.listener.is_cancelled:
                break
            if content["path"]:
                self._a2c_opt["dir"] = f"{self._path}/{content['path']}"
            else:
                self._a2c_opt["dir"] = self._path
            filename = content["filename"]
            self._a2c_opt["out"] = filename
            try:
                gid = await TorrentManager.aria2.addUri(
                    uris=[content["url"]], options=self._a2c_opt, position=0
                )
            except (TimeoutError, ClientError, Exception) as e:
                self._failed += 1
                LOGGER.error(f"Unable to download {filename} due to: {e}")
                continue
            try:
                self.download_task = await self._tell_status(gid)
            except (Aria2rpcException, TimeoutError, ClientError) as e:
                self._failed += 1
                LOGGER.error(
                    f"Unable to fetch status for {filename} (gid {gid}) due to: {e}"
                )
                with suppress(Exception):
                    await TorrentManager.aria2_remove({"gid": gid})
                continue
            while True:
                if self.listener.is_cancelled:
                    if self.download_task:
                        await TorrentManager.aria2_remove(self.download_task)
                    break
                try:
                    self.download_task = await self._tell_status(gid)
                except (Aria2rpcException, TimeoutError, ClientError) as e:
                    LOGGER.error(
                        f"Status poll failed for {filename} (gid {gid}) due to: {e}"
                    )
                    await sleep(2)
                    continue
                if error_message := self.download_task.get("errorMessage"):
                    self._failed += 1
                    LOGGER.error(
                        f"Unable to download {aria2_name(self.download_task)} due to: {error_message}"
                    )
                    await TorrentManager.aria2_remove(self.download_task)
                    break
                elif self.download_task.get("status", "") == "complete":
                    self._proc_bytes += int(self.download_task.get("totalLength", "0"))
                    await TorrentManager.aria2_remove(self.download_task)
                    break
                await sleep(1)
            self.download_task = None
        if self.listener.is_cancelled:
            return
        if self._failed == len(contents):
            await self.listener.on_download_error("All files are failed to download!")
            return
        await self.listener.on_download_complete()
        return

    async def cancel_task(self):
        self.listener.is_cancelled = True
        LOGGER.info(f"Cancelling Download: {self.listener.name}")
        await self.listener.on_download_error("Download Cancelled by User!")
        if self.download_task:
            await TorrentManager.aria2_remove(self.download_task)
