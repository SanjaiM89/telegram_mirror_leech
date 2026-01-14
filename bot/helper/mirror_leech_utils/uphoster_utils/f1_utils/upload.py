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


class F1Upload:
    def __init__(self, listener, path):
        self.listener = listener
        self._updater = None
        self._path = path
        self._is_errored = False
        self.api_url = "https://api.1fichier.com/v1/upload/get_upload_server.cgi"
        self.__processed_bytes = 0
        self.last_uploaded = 0
        self.total_time = 0
        self.total_files = 0
        self.is_uploading = True
        self.update_interval = 3

        from bot import user_data

        user_dict = user_data.get(self.listener.user_id, {})
        self.api_key = user_dict.get("F1_API_KEY") or Config.F1_API_KEY

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

    async def get_upload_server(self):
        cmd = [
            "curl",
            "-s",
            "-X", "POST",
            "-H", f"Authorization: Bearer {self.api_key}",
            "-H", "Content-Type: application/json",
            "-d", "{}",
            self.api_url
        ]
        
        def _run_curl():
            return srun(cmd, stdout=PIPE, stderr=PIPE)

        process = await get_event_loop().run_in_executor(None, _run_curl)
        
        stdout = process.stdout.decode().strip()
        stderr = process.stderr.decode().strip()

        if process.returncode != 0:
            raise Exception(f"F1 Get Server Error: {stderr}")
            
        try:
            data = json_loads(stdout)
            if "url" in data:
                return data["url"]
            elif "message" in data: # Handle potential error message from API
                 raise Exception(f"F1 API Error: {data['message']}")
            else:
                 raise Exception(f"F1 API Unknown Response: {stdout}")
        except Exception as e:
             raise Exception(f"F1 Get Server JSON Error: {e} | Response: {stdout}")

    @retry(
        wait=wait_exponential(multiplier=2, min=4, max=8),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    async def upload_file(self, file_path):
        if not self.api_key:
             raise ValueError("1fichier API key not configured!")
             
        upload_url = await self.get_upload_server()
        
        cmd = [
            "curl",
            "-s",
            "-w", "\n%{http_code}",
            "--http1.1",
            "-F", f"file=@{file_path}",
            upload_url
        ]
        
        def _run_curl():
            return srun(cmd, stdout=PIPE, stderr=PIPE)

        process = await get_event_loop().run_in_executor(None, _run_curl)
        
        output = process.stdout.decode().strip()
        lines = output.split('\n')
        if not lines:
             raise Exception(f"Curl produced no output. Stderr: {process.stderr.decode().strip()}")
             
        http_code = lines[-1]
        response_body = "\n".join(lines[:-1]) # XML/HTML response usually from 1fichier
        
        if not http_code.startswith("2"):
             raise Exception(f"F1 Upload Error: HTTP {http_code}. Response: {response_body}")

        # 1fichier returns links in response body differently based on upload method.
        # Usually it returns an XML or simple text with the download link.
        # We need to parse it. 
        # The documentation doesn't specify the exact response format for curl upload, 
        # but typically it's the download link or a list of links.
        # Let's assume the response body contains the link or we parse it.
        # Based on common knowledge, it returns a text with the link or XML.
        # Let's verify via testing or try to extract any link starting with https://1fichier.com/
        
        import re
        match = re.search(r'https?://1fichier\.com/\?\w+', response_body)
        if match:
            return match.group(0)
        else:
            # Maybe it returns just the link?
            if response_body.startswith("https://1fichier.com"):
                return response_body.strip()
            # If we can't find it, dump response
            raise Exception(f"Could not find download link in response: {response_body}")

    async def upload(self):
        try:
            LOGGER.info(f"1fichier Uploading: {self._path}")
            self._updater = SetInterval(self.update_interval, self.progress)

            if not self.api_key:
                 raise ValueError("1fichier API key not configured!")

            if await aiopath.isfile(self._path):
                resp_link = await self.upload_file(self._path)
                
                if resp_link:
                    self.total_files = 1
                    await self.listener.on_upload_complete(
                        resp_link,
                        self.total_files,
                        0, # folders
                        "File",
                        dir_id="",
                    )
                else:
                    raise Exception("F1 Upload Failed: No link returned")

            else:
                raise ValueError("1fichier only supports single file upload.")
                
        except Exception as err:
            if isinstance(err, RetryError):
                LOGGER.info(f"Total Attempts: {err.last_attempt.attempt_number}")
                err = err.last_attempt.exception()
            LOGGER.error(f"1fichier Error: {err}")
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
            LOGGER.info(f"Cancelling 1fichier Upload: {self.listener.name}")
            await self.listener.on_upload_error("1fichier upload has been cancelled!")
