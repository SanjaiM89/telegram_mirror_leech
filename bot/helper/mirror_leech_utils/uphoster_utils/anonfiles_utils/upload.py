from logging import getLogger
from os import path as ospath
from json import loads as json_loads
from asyncio import create_subprocess_exec, subprocess
from aiofiles.os import path as aiopath
from aiofiles.os import rename as aiorename
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bot.core.config_manager import Config
from bot.helper.ext_utils.bot_utils import SetInterval

LOGGER = getLogger(__name__)


class AnonFilesUpload:
    def __init__(self, listener, path):
        self.listener = listener
        self._updater = None
        self._path = path
        self._is_errored = False
        self.api_url = "https://api.anonfilesnew.com/upload"
        self.__processed_bytes = 0
        self.last_uploaded = 0
        self.total_time = 0
        self.total_files = 0
        self.is_uploading = True
        self.update_interval = 3

        from bot import user_data

        user_dict = user_data.get(self.listener.user_id, {})
        self.api_key = user_dict.get("ANONFILES_API") or Config.ANONFILES_API

    @property
    def speed(self):
        try:
            return self.__processed_bytes / self.total_time
        except Exception:
            return 0

    @property
    def processed_bytes(self):
        return self.__processed_bytes

    async def progress(self):
        # Placeholder for progress since we are using curl without progress parsing
        if self.is_uploading:
             self.total_time += self.update_interval

    @retry(
        wait=wait_exponential(multiplier=2, min=4, max=8),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    async def upload_file(self, file_path):
        if not self.api_key:
             raise ValueError("AnonFiles API key not configured!")
             
        url = f"{self.api_url}?key={self.api_key}"
        
        # Use curl for upload as it handles multipart/form-data and large files robustly
        # This bypasses aiohttp issues with chunked encoding or timeouts on some servers
        
        cmd = [
            "curl",
            "-s", 
            "--http1.1", # Force HTTP/1.1 to avoid HTTP/2 stream issues with some servers
            "-F", f"file=@{file_path}",
            "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            url
        ]

        process = await create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        decoded_stdout = stdout.decode().strip()
        decoded_stderr = stderr.decode().strip()

        if process.returncode != 0:
            raise Exception(f"Curl Error: {decoded_stderr}")
            
        if not decoded_stdout:
             raise Exception(f"AnonFiles returned empty response. Curl Stderr: {decoded_stderr}")

        try:
            return json_loads(decoded_stdout)
        except Exception as e:
             raise Exception(f"JSON Decode Error: {e} | Response: {decoded_stdout}")

    async def upload(self):
        try:
            LOGGER.info(f"AnonFiles Uploading: {self._path}")
            self._updater = SetInterval(self.update_interval, self.progress)

            if not self.api_key:
                 raise ValueError("AnonFiles API key not configured!")

            if await aiopath.isfile(self._path):
                # Replace spaces with underscores or similar if needed
                new_path = ospath.join(
                    ospath.dirname(self._path), ospath.basename(self._path).replace(" ", "_")
                )
                if self._path != new_path:
                    await aiorename(self._path, new_path)
                    self._path = new_path

                resp = await self.upload_file(self._path)
                
                if resp.get("status"):
                    link = resp["data"]["file"]["url"]["full"]
                    self.total_files = 1
                    await self.listener.on_upload_complete(
                        link,
                        self.total_files,
                        self.total_folders,
                        "File",
                        dir_id="",
                    )
                else:
                    error_msg = "Unknown Error"
                    if "error" in resp:
                         if "data" in resp and "file" in resp["data"] and "message" in resp["data"]["file"]:
                             error_msg = resp["data"]["file"]["message"]
                    raise Exception(f"AnonFiles Error: {error_msg}")

            else:
                raise ValueError("AnonFiles only supports single file upload.")
                
        except Exception as err:
            if isinstance(err, RetryError):
                LOGGER.info(f"Total Attempts: {err.last_attempt.attempt_number}")
                err = err.last_attempt.exception()
            LOGGER.error(f"AnonFiles Error: {err}")
            await self.listener.on_upload_error(str(err))
            self._is_errored = True
        finally:
            if self._updater:
                self._updater.cancel()
            if (self.listener.is_cancelled and not self._is_errored) or self._is_errored:
                return

    async def cancel_task(self):
        self.listener.is_cancelled = True
        if self.is_uploading:
            LOGGER.info(f"Cancelling AnonFiles Upload: {self.listener.name}")
            await self.listener.on_upload_error("AnonFiles upload has been cancelled!")
