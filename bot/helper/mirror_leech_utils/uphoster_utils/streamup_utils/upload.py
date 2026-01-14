from logging import getLogger
from os import path as ospath
from json import loads as json_loads
from asyncio import create_subprocess_exec, subprocess
import re
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


class StreamUpUpload:
    def __init__(self, listener, path):
        self.listener = listener
        self._updater = None
        self._path = path
        self._is_errored = False
        self.api_url = "https://api.streamup.cc/v1/upload"
        self.__processed_bytes = 0
        self.last_uploaded = 0
        self.total_time = 0
        self.total_files = 0
        self.total_folders = 0
        self.is_uploading = True
        self.update_interval = 3
        self.total_size = 0

        from bot import user_data

        user_dict = user_data.get(self.listener.user_id, {})
        self.api_key = user_dict.get("STREAMUP_API") or Config.STREAMUP_API

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
             raise ValueError("StreamUP API key not configured!")
             
        # StreamUP API: POST /v1/upload
        # Params: api_key, file
        
        cmd = [
            "curl",
            "--connect-timeout", "30",
            "--max-time", "600",
            "-w", "\n%{http_code}",
            "--http1.1",
            "-F", f"api_key={self.api_key}",
            "-F", f"file=@{file_path}",
            "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            self.api_url
        ]
        
        process = await create_subprocess_exec(
            *cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # Read stderr for progress
        stderr_output = []
        
        while True:
            chunk = await process.stderr.read(1024)
            if not chunk:
                break
            
            chunk_str = chunk.decode('utf-8', errors='replace')
            stderr_output.append(chunk_str)
            
            matches = re.findall(r'\r\s*(\d+)\s+([0-9.]+)([KMG]?)\s+(\d+)\s+([0-9.]+)([KMG]?)\s+(\d+)\s+([0-9.]+)([KMG]?)', chunk_str)
            if matches:
                last_match = matches[-1]
                up_size_raw = float(last_match[7])
                up_unit = last_match[8]
                
                multiplier = 1
                if up_unit == 'K': multiplier = 1024
                elif up_unit == 'M': multiplier = 1024 * 1024
                elif up_unit == 'G': multiplier = 1024 * 1024 * 1024
                
                current_bytes = int(up_size_raw * multiplier)
                
                if current_bytes > self.__processed_bytes:
                    self.__processed_bytes = current_bytes

        stdout_bytes = await process.stdout.read()
        stdout_str = stdout_bytes.decode().strip()
        stderr_str = "".join(stderr_output)
        
        await process.wait()
        
        output = stdout_str
        lines = output.split('\n')
        if not lines:
             raise Exception(f"Curl produced no output. Stderr: {stderr_str}")
             
        http_code = lines[-1]
        response_body = "\n".join(lines[:-1])
        
        # Handle network failure (HTTP 000 means curl couldn't connect)
        if http_code == "000":
            raise Exception("Network error: Unable to connect to StreamUP server. Check your internet connection or try again later.")
        
        if http_code == "200":
             try:
                 data = json_loads(response_body)
                 # Response: { "filecode": "https://streamup.cc/abcdef123", "video_id": 123, "title": "My Video" }
                 # Or Error: { "success": false, "message": "..." }
                 
                 if "filecode" in data:
                     return data["filecode"]
                 elif not data.get("success", True):
                     raise Exception(f"StreamUP API Error: {data.get('message', 'Unknown Error')}")
                 else:
                     # Maybe success is implicit if filecode exists?
                     # Let's assume filecode is the key.
                     if "filecode" in data: 
                         return data["filecode"]
                     # Handle edge case
                     raise Exception(f"StreamUP Unknown Response: {response_body}")
             except Exception as e:
                 raise Exception(f"StreamUP JSON Error: {e} | Response: {response_body}")
        
        elif http_code == "413":
             raise Exception("StreamUP Error: File too large (HTTP 413). Check account limits.")
        elif http_code == "500":
             raise Exception(f"StreamUP server error (HTTP 500): The service may be temporarily unavailable. Response: {response_body}")
        else:
             raise Exception(f"StreamUP Upload Error: HTTP {http_code}. Response: {response_body}")

    async def upload(self):
        try:
            LOGGER.info(f"StreamUP Uploading: {self._path}")
            self.total_size = await aiopath.getsize(self._path)
            self._updater = SetInterval(self.update_interval, self.progress)

            if not self.api_key:
                 raise ValueError("StreamUP API key not configured!")

            if await aiopath.isfile(self._path):
                new_path = ospath.join(
                    ospath.dirname(self._path), ospath.basename(self._path).replace(" ", "_")
                )
                if self._path != new_path:
                    await aiorename(self._path, new_path)
                    self._path = new_path

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
                    raise Exception("StreamUP Upload Failed: No link returned")

            else:
                raise ValueError("StreamUP only supports single file upload.")
                
        except Exception as err:
            if isinstance(err, RetryError):
                LOGGER.info(f"Total Attempts: {err.last_attempt.attempt_number}")
                err = err.last_attempt.exception()
            LOGGER.error(f"StreamUP Error: {err}")
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
            LOGGER.info(f"Cancelling StreamUP Upload: {self.listener.name}")
            await self.listener.on_upload_error("StreamUP upload has been cancelled!")