import os
import sys
import asyncio
import uuid
import re
import aiohttp

from openai import APIError, APIConnectionError, RateLimitError
from fastmcp.exceptions import ToolError

from config import (
    log, client, MODEL_NAME, STORAGE_DIR, SCRIPT_TIMEOUT,
    MAX_ANALYSIS_OUTPUT_CHARS, FILE_TYPE_CONFIG,
)


def extract_code(llm_response: str) -> str:
    """Clean markdown code blocks if the LLM ignores instructions."""
    pattern = r"```(?:python)?(.*?)```"
    match = re.search(pattern, llm_response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return llm_response.strip()


async def _llm_call(messages: list[dict], temperature: float = 0.01) -> str:
    """Call the LLM with retry logic for transient failures.

    Retries up to 2 times with exponential backoff on APIError,
    APIConnectionError, and RateLimitError.  Returns the response
    content string.
    """
    max_retries = 2
    for attempt in range(max_retries + 1):
        try:
            response = await client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=temperature,
            )
            return response.choices[0].message.content
        except (APIError, APIConnectionError, RateLimitError) as e:
            if attempt < max_retries:
                wait = [1, 3][attempt]
                log.warning(f"LLM call failed (attempt {attempt + 1}/{max_retries + 1}), retrying in {wait}s: {e}")
                await asyncio.sleep(wait)
            else:
                log.error(f"LLM call failed after {max_retries + 1} attempts: {e}")
                raise


def _truncate_output(text: str, max_chars: int = MAX_ANALYSIS_OUTPUT_CHARS) -> str:
    """Truncate text for LLM context, preserving start and noting truncation."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated, showing first {max_chars:,} of {len(text):,} chars]"


def _parse_checker_result(raw: str) -> tuple[str, str]:
    """Parse checker LLM response into (status, content).

    Status is 'passed', 'failed', or 'unknown'.
    Content is the response with the marker stripped.
    """
    stripped = raw.strip()
    if stripped.startswith("__CHECK_PASSED__"):
        return "passed", stripped[len("__CHECK_PASSED__"):].strip()
    elif stripped.startswith("__CHECK_FAILED__"):
        return "failed", stripped[len("__CHECK_FAILED__"):].strip()
    else:
        log.warning("Checker response missing expected marker, treating as passed")
        return "unknown", stripped


async def generate_code(instructions: str, system_prompt: str, error_context: str = None, previous_code: str = None, input_file_path: str = None) -> str:
    """Call the LLM to generate Python code, or to fix previously failed code.

    On the first attempt, sends user instructions directly.  On retries,
    includes the failed code and error message so the LLM can self-correct.
    When *input_file_path* is provided (modification flow), prompts reference
    both variables.
    """
    messages = [{"role": "system", "content": system_prompt}]

    if error_context:
        log.debug(f"Requesting code fix — error was: {error_context}")
        if input_file_path:
            path_reminder = (
                "Ensure you still read from `input_file_path` and "
                "save to `file_path`. Do NOT redefine these variables."
            )
        else:
            path_reminder = "Ensure you still use `file_path`."
        messages.append({
            "role": "user",
            "content": f"""
            The previous code you wrote failed.

            USER INSTRUCTIONS: {instructions}

            FAILED CODE:
            {previous_code}

            ERROR MESSAGE:
            {error_context}

            Please rewrite the code to fix this error. {path_reminder}
            """
        })
    else:
        log.debug(f"Requesting fresh code generation for: {instructions}")
        if input_file_path:
            messages.append({
                "role": "user",
                "content": (
                    f"Modify an existing file according to these instructions: {instructions}\n\n"
                    f"The existing file is available at the path stored in `input_file_path`. "
                    f"Save the modified file to the path stored in `file_path`."
                )
            })
        else:
            messages.append({"role": "user", "content": f"Create a python script for this request: {instructions}"})

    log.info(f"Calling LLM ({MODEL_NAME})...")
    response = await client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=0.01
    )
    raw = response.choices[0].message.content
    code = extract_code(raw)
    log.debug(f"Generated code:\n{code}")
    return code


async def run_code_in_subprocess(code: str, file_path: str, input_file_path: str = None) -> None:
    """Run LLM-generated code in an isolated subprocess.

    The subprocess inherits only PATH (no API keys or secrets).  Any
    ``file_path = ...`` assignments the LLM may have generated are stripped
    and replaced with the correct runtime path.  When *input_file_path* is
    provided (modification flow), it is also injected and protected.
    """
    # Strip any file_path reassignments the LLM may have generated
    cleaned = re.sub(r"(?m)^file_path\s*=\s*.+$", "", code)
    if input_file_path:
        cleaned = re.sub(r"(?m)^input_file_path\s*=\s*.+$", "", cleaned)
        script_content = (
            f"input_file_path = {input_file_path!r}\n"
            f"file_path = {file_path!r}\n"
            f"{cleaned}"
        )
    else:
        script_content = f"file_path = {file_path!r}\n{cleaned}"
    script_path = os.path.join(STORAGE_DIR, f"_script_{uuid.uuid4().hex[:8]}.py")
    log.info(f"Writing script to {script_path}")
    log.debug(f"Script content:\n{script_content}")
    try:
        with open(script_path, "w") as f:
            f.write(script_content)

        safe_env = {"PATH": os.getenv("PATH", "/usr/bin:/usr/local/bin")}
        log.info(f"Running subprocess: {sys.executable} {script_path}")
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=SCRIPT_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Script timed out after {SCRIPT_TIMEOUT} seconds")

        stdout_text = stdout.decode() if stdout else ""
        stderr_text = stderr.decode() if stderr else ""

        log.info(f"Subprocess exited with code {proc.returncode}")
        if stdout_text:
            log.info(f"[stdout] {stdout_text.strip()}")
        if stderr_text:
            log.warning(f"[stderr] {stderr_text.strip()}")
        if proc.returncode != 0:
            raise RuntimeError(stderr_text.strip() or f"Script exited with code {proc.returncode}")

        log.info(f"Checking for output file at: {file_path}")
        log.info(f"File exists: {os.path.exists(file_path)}")
        if os.path.exists(file_path):
            log.info(f"File size: {os.path.getsize(file_path)} bytes")
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass


async def run_analysis_code(code: str, input_file_path: str) -> str:
    """Run pandas analysis code in an isolated subprocess and return stdout.

    Similar to run_code_in_subprocess but returns captured stdout text
    instead of writing to an output file.  Raises RuntimeError on failure.
    """
    cleaned = re.sub(r"(?m)^input_file_path\s*=\s*.+$", "", code)
    script_content = f"input_file_path = {input_file_path!r}\n{cleaned}"
    script_path = os.path.join(STORAGE_DIR, f"_analysis_{uuid.uuid4().hex[:8]}.py")
    log.info(f"Writing analysis script to {script_path}")
    log.debug(f"Analysis script content:\n{script_content}")
    try:
        with open(script_path, "w") as f:
            f.write(script_content)

        safe_env = {"PATH": os.getenv("PATH", "/usr/bin:/usr/local/bin")}
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=safe_env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=SCRIPT_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"Analysis script timed out after {SCRIPT_TIMEOUT} seconds")

        stdout_text = stdout.decode() if stdout else ""
        stderr_text = stderr.decode() if stderr else ""

        log.info(f"Analysis subprocess exited with code {proc.returncode}")
        if stdout_text:
            log.debug(f"[analysis stdout] {stdout_text[:500]}")
        if proc.returncode != 0:
            raise RuntimeError(stderr_text.strip() or f"Script exited with code {proc.returncode}")

        # Append meaningful stderr warnings (filter out noise)
        if stderr_text:
            _noise = ("FutureWarning", "DeprecationWarning", "PendingDeprecationWarning")
            warning_lines = [
                line for line in stderr_text.strip().splitlines()
                if not any(noise in line for noise in _noise)
            ]
            if warning_lines:
                stdout_text += "\n--- Warnings ---\n" + "\n".join(warning_lines)

        return stdout_text
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass


async def upload_file(local_path: str, filename: str) -> str:
    """Upload a generated file to Open WebUI and return a public download URL.

    Raises on failure so the retry loop can catch and self-correct.
    """
    internal_api_url = os.getenv("INTERNAL_API_URL", "http://localhost:8080")
    public_domain = os.getenv("PUBLIC_DOMAIN", "http://localhost:8080")
    api_key = os.getenv("OPENWEBUI_API_KEY", "sk-placeholder")
    headers = {"Authorization": f"Bearer {api_key}"}
    upload_url = f"{internal_api_url.rstrip('/')}/api/v1/files/"

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=60)
    ) as session:
        with open(local_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("file", f, filename=filename)
            async with session.post(upload_url, headers=headers, data=form) as resp:
                status = resp.status
                text = await resp.text()
                try:
                    data = await resp.json()
                except Exception:
                    data = {}

    try:
        os.remove(local_path)
    except Exception:
        pass

    if status < 200 or status >= 300:
        raise RuntimeError(f"Upload failed (HTTP {status}): {text}")

    file_id = data.get("id") or data.get("uuid") or data.get("file_id")
    return f"{public_domain.rstrip('/')}/api/v1/files/{file_id}/content"


# Extension-to-file-type mapping (reverse of FILE_TYPE_CONFIG)
_EXT_TO_FILE_TYPE = {".xlsx": "excel", ".xls": "excel", ".docx": "docx", ".csv": "csv"}

# Content-Type to file type mapping
_CONTENT_TYPE_TO_FILE_TYPE = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "excel",
    "application/vnd.ms-excel": "excel",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/csv": "csv",
    "application/csv": "csv",
}


def _detect_file_type(resp: aiohttp.ClientResponse) -> str:
    """Detect file type from HTTP response headers.

    Checks Content-Disposition filename extension first, then Content-Type.
    Raises RuntimeError if the file type cannot be determined or is unsupported.
    """
    # 1. Try Content-Disposition header for filename
    cd = resp.headers.get("Content-Disposition", "")
    if cd:
        match = re.search(r'filename[*]?=["\']?([^"\';\s]+)', cd)
        if match:
            filename = match.group(1)
            ext = os.path.splitext(filename)[1].lower()
            if ext in _EXT_TO_FILE_TYPE:
                return _EXT_TO_FILE_TYPE[ext]
            log.warning(f"Content-Disposition filename '{filename}' has unrecognized extension '{ext}'")

    # 2. Try Content-Type header
    content_type = resp.content_type or ""
    base_type = content_type.split(";")[0].strip().lower()
    if base_type in _CONTENT_TYPE_TO_FILE_TYPE:
        return _CONTENT_TYPE_TO_FILE_TYPE[base_type]

    # 3. Cannot determine
    raise RuntimeError(
        f"Cannot determine file type from response headers. "
        f"Content-Disposition: '{cd}', Content-Type: '{content_type}'. "
        f"Supported types: {', '.join(FILE_TYPE_CONFIG.keys())}"
    )


async def download_file(file_id: str) -> tuple[str, str]:
    """Download a file from Open WebUI by file_id.

    Returns (local_path, file_type) where file_type is a key in FILE_TYPE_CONFIG.
    Raises RuntimeError on download failure or unrecognized file type.
    """
    internal_api_url = os.getenv("INTERNAL_API_URL", "http://localhost:8080")
    api_key = os.getenv("OPENWEBUI_API_KEY", "sk-placeholder")
    headers = {"Authorization": f"Bearer {api_key}"}
    download_url = f"{internal_api_url.rstrip('/')}/api/v1/files/{file_id}/content"

    log.info(f"Downloading file {file_id} from {download_url}")

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120)
    ) as session:
        async with session.get(download_url, headers=headers) as resp:
            if resp.status < 200 or resp.status >= 300:
                text = await resp.text()
                raise RuntimeError(
                    f"Failed to download file {file_id} (HTTP {resp.status}): {text}"
                )

            file_type = _detect_file_type(resp)

            local_filename = f"input_{file_id}_{uuid.uuid4().hex[:8]}{FILE_TYPE_CONFIG[file_type]['ext']}"
            local_path = os.path.join(STORAGE_DIR, local_filename)

            with open(local_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(8192):
                    f.write(chunk)

    log.info(f"Downloaded file to {local_path} (type: {file_type}, size: {os.path.getsize(local_path)} bytes)")
    return local_path, file_type
