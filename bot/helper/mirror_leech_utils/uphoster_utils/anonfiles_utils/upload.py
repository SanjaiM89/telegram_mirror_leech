from logging import getLogger
from os import path as ospath
from json import loads as json_loads
from asyncio import get_event_loop
from subprocess import run as srun, PIPE
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
        self.total_folders = 0
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
        
        cmd = [
            "curl",
            "-s",
            "--http1.1",
            "-F", f"file=@{file_path}",
            "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            url
        ]
        
        def _run_curl():
            return srun(cmd, stdout=PIPE, stderr=PIPE)

        process = await get_event_loop().run_in_executor(None, _run_curl)
        
        stdout = process.stdout.decode().strip()
        stderr = process.stderr.decode().strip()

        if process.returncode != 0:
            raise Exception(f"Curl Error (Code {process.returncode}): {stderr}")
            
        if not stdout:
             # Check for common HTTP errors in stderr if available, or just generic message
             if "413 Payload Too Large" in stderr or "413 Request Entity Too Large" in stderr:
                 raise Exception("AnonFiles Error: File is too large for the server (HTTP 413). Check your account limits.")
             elif "502 Bad Gateway" in stderr:
                 raise Exception("AnonFiles Error: Server is down or overloaded (HTTP 502).")
             
             # If we used -s, stderr might be empty unless we use -S (show error) or if curl failed silently?
             # But if returncode is 0 and stdout is empty, it's usually a server issue.
             # We can try to re-run with -v if this happens? No, better to just report.
             
             # Actually, without -v, curl -s won't print headers to stderr.
             # So we can't parse 413 from stderr if we use -s.
             # We should use -v but filter the logs?
             # Or use -w "%{http_code}" to get the status code at the end.
             pass

        # Let's use -w to get status code reliably
        # We need to separate json output from status code.
        # curl -w "\n%{http_code}" ... 
        
        # New approach: Use write-out to get http code
        cmd = [
            "curl",
            "-s",
            "-w", "\n%{http_code}", 
            "--http1.1",
            "-F", f"file=@{file_path}",
            "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            url
        ]
        
        def _run_curl():
            return srun(cmd, stdout=PIPE, stderr=PIPE)

        process = await get_event_loop().run_in_executor(None, _run_curl)
        
        output = process.stdout.decode().strip()
        # The last line should be the http code
        lines = output.split('\n')
        if not lines:
             raise Exception(f"Curl produced no output. Stderr: {process.stderr.decode().strip()}")
             
        http_code = lines[-1]
        response_body = "\n".join(lines[:-1])
        
        if http_code == "413":
             raise Exception("AnonFiles Error: File is too large (HTTP 413). This is likely a 100MB Cloudflare limit on the server.")
        elif http_code == "502":
             raise Exception("AnonFiles Error: Bad Gateway (HTTP 502).")
        elif not http_code.startswith("2"):
             raise Exception(f"AnonFiles Error: HTTP {http_code}. Response: {response_body}")

        if not response_body:
             raise Exception(f"AnonFiles returned empty body with HTTP {http_code}.")

        try:
            return json_loads(response_body)
        except Exception as e:
             raise Exception(f"JSON Decode Error: {e} | Body: {response_body}")

    async def upload(self):
        try:
            LOGGER.info(f"AnonFiles Uploading: {self._path}")
            self._updater = SetInterval(self.update_interval, self.progress)

            if not self.api_key:
                 raise ValueError("AnonFiles API key not configured!")

            if await aiopath.isfile(self._path):
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
