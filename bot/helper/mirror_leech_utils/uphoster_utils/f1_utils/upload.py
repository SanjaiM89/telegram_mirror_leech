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
            "-X", "GET",
            "-H", f"Authorization: Bearer {self.api_key}",
            "-H", "Content-Type: application/json",
            "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
            if "url" in data and "id" in data:
                return data
            elif "message" in data:
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
             
        server_data = await self.get_upload_server()
        upload_url = f"https://{server_data['url']}/upload.cgi?id={server_data['id']}"
        LOGGER.info(f"DEBUG: F1 Upload URL: {upload_url}")
        
        # 1. Upload File
        cmd = [
            "curl",
            "-s",
            "-w", "\n%{http_code}\n%{redirect_url}",
            "--http1.1",
            "-F", f"file[]=@{file_path}",
            "-H", f"Authorization: Bearer {self.api_key}",
            "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            upload_url
        ]
        
        def _run_curl():
            return srun(cmd, stdout=PIPE, stderr=PIPE)

        process = await get_event_loop().run_in_executor(None, _run_curl)
        
        output = process.stdout.decode().strip()
        lines = output.split('\n')
        if not lines:
             raise Exception(f"Curl produced no output. Stderr: {process.stderr.decode().strip()}")
             
        # Expected output format from -w:
        # [Response Body]
        # [HTTP Code]
        # [Redirect URL]
        
        if len(lines) < 2:
             raise Exception(f"Unexpected curl output: {output}")

        redirect_url = lines[-1]
        http_code = lines[-2]
        response_body = "\n".join(lines[:-2])
        
        if http_code == "302" and redirect_url:
             # Success! Now fetch the download link from the redirect URL
             # Redirect URL example: /end.pl?xid=...
             # We need to construct full URL: https://{server_host}{redirect_url}
             # Wait, redirect_url from curl might be absolute or relative. 
             # If relative, we prepend the server host.
             
             if not redirect_url.startswith("http"):
                 # Extract host from upload_url
                 from urllib.parse import urlparse
                 parsed_up = urlparse(upload_url)
                 base_url = f"{parsed_up.scheme}://{parsed_up.netloc}"
                 report_url = f"{base_url}{redirect_url}"
             else:
                 report_url = redirect_url
                 
             # Append &JSON=1 to get JSON report
             if "?" in report_url:
                 report_url += "&JSON=1"
             else:
                 report_url += "?JSON=1"
                 
             # 2. Get Report
             cmd_report = [
                "curl",
                "-s",
                "-X", "GET",
                "-H", f"Authorization: Bearer {self.api_key}",
                "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                report_url
             ]
             
             def _run_report_curl():
                 return srun(cmd_report, stdout=PIPE, stderr=PIPE)
                 
             process_rep = await get_event_loop().run_in_executor(None, _run_report_curl)
             
             if process_rep.returncode != 0:
                 raise Exception(f"F1 Report Error: {process_rep.stderr.decode().strip()}")
                 
             rep_stdout = process_rep.stdout.decode().strip()
             try:
                 rep_data = json_loads(rep_stdout)
                 # JSON return (pretty):
                 # {
                 #   "incoming" : 0,
                 #   "links" : [
                 #     {
                 #       "download" : "https://1fichier.com/?abcef",
                 #       "filename" : "Filename.ext",
                 #       ...
                 #     }
                 #   ]
                 # }
                 if "links" in rep_data and len(rep_data["links"]) > 0:
                     return rep_data["links"][0]["download"]
                 else:
                     raise Exception(f"No links found in report: {rep_stdout}")
             except Exception as e:
                 # Fallback: parse text/html if JSON fails (though we requested JSON)
                 import re
                 match = re.search(r'https?://1fichier\.com/\?\w+', rep_stdout)
                 if match:
                     return match.group(0)
                 raise Exception(f"F1 Report JSON Error: {e} | Response: {rep_stdout}")

        elif http_code.startswith("2"):
             # If 200 OK, maybe it returned the link directly?
             # But docs say 302 is success. 200 might be error page?
             # "200 Return program OK ... Html page with a clear error message"
             # So 200 is likely an ERROR if it's not a redirect to end.pl
             # But let's check body for link anyway
             import re
             match = re.search(r'https?://1fichier\.com/\?\w+', response_body)
             if match:
                 return match.group(0)
             raise Exception(f"F1 Upload returned 200 but no link found. Response: {response_body}")
        
        else:
             raise Exception(f"F1 Upload Error: HTTP {http_code}. Response: {response_body}")

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
