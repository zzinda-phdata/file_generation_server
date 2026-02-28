import os
import sys
import asyncio
import uuid
import logging
import re
import tempfile
from typing import Any
import aiohttp
import uvicorn

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse
from openai import AsyncOpenAI

# --- LOGGING ---
log = logging.getLogger("openwebui_fileagent")
log.setLevel(logging.DEBUG)
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
log.addHandler(_handler)
log.propagate = False

# --- CONFIGURATION ---
mcp = FastMCP("FileAgent")

# Temporary directory for generated files
STORAGE_DIR = tempfile.mkdtemp(prefix="owui_files_")
SCRIPT_TIMEOUT = int(os.getenv("SCRIPT_TIMEOUT", "90"))
# These fire at module import — once per gunicorn worker when running multi-worker
log.info(f"STORAGE_DIR = {STORAGE_DIR}")
log.info(f"SCRIPT_TIMEOUT = {SCRIPT_TIMEOUT}")
log.info(f"Python executable = {sys.executable}")

client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "sk-placeholder"),
    base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    timeout=120.0,
)
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o")

# --- SYSTEM PROMPTS ---
EXCEL_SYSTEM_PROMPT = """
You are an expert Python developer specialized in the 'xlsxwriter' library.
Your goal is to write a Python script that generates an Excel file based on user instructions.

RULES:
1. You must use 'xlsxwriter' to create the file.
2. A variable named `file_path` will be provided to your script at runtime. You MUST use this variable when opening the workbook:
   `workbook = xlsxwriter.Workbook(file_path)`
3. You must explicitly close the workbook at the end: `workbook.close()`
4. Do NOT output markdown formatting (like ```python). Output ONLY raw Python code.
5. If you use calculations, prefer Excel formulas (e.g., `write_formula`) over pre-calculated values unless asked otherwise.
6. Make the Excel file professional: use bold headers, currency formats, and colors where appropriate.
"""

DOCX_SYSTEM_PROMPT = """
You are an expert Python developer specialized in the 'python-docx' library.
Your goal is to write a Python script that generates a Word document based on user instructions.

RULES:
1. You must use 'python-docx' (import docx / from docx import Document) to create the file.
2. A variable named `file_path` will be provided to your script at runtime. You MUST save the document to this path:
   `document.save(file_path)`
3. Do NOT output markdown formatting (like ```python). Output ONLY raw Python code.
4. Make the document professional: use appropriate headings, styles, tables, and formatting where appropriate.
"""

EXCEL_MODIFY_SYSTEM_PROMPT = """
You are an expert Python developer specialized in the 'openpyxl' library.
Your goal is to write a Python script that reads an existing Excel file and modifies it based on user instructions.

RULES:
1. You must use 'openpyxl' to read and modify the file.
2. Two variables will be provided at runtime:
   - `input_file_path` — the path to the existing Excel file (read from this)
   - `file_path` — the path where the modified file must be saved (write to this)
3. Open the existing workbook like this:
   `from openpyxl import load_workbook`
   `wb = load_workbook(input_file_path)`
4. After making modifications, save to the output path:
   `wb.save(file_path)`
5. Do NOT overwrite `input_file_path`. Do NOT save to `input_file_path`.
6. Do NOT output markdown formatting (like ```python). Output ONLY raw Python code.
7. Do NOT redefine `input_file_path` or `file_path`.
8. If the user asks to add formatting, use openpyxl styles (Font, PatternFill, Alignment, Border, etc.).
9. If the user asks to add new data, append it without destroying existing data unless explicitly told to replace.
10. Preserve existing formatting and data unless the user explicitly asks to change or remove it.
"""

DOCX_MODIFY_SYSTEM_PROMPT = """
You are an expert Python developer specialized in the 'python-docx' library.
Your goal is to write a Python script that reads an existing Word document and modifies it based on user instructions.

RULES:
1. You must use 'python-docx' (import docx / from docx import Document) to read and modify the file.
2. Two variables will be provided at runtime:
   - `input_file_path` — the path to the existing Word document (read from this)
   - `file_path` — the path where the modified document must be saved (write to this)
3. Open the existing document like this:
   `from docx import Document`
   `document = Document(input_file_path)`
4. After making modifications, save to the output path:
   `document.save(file_path)`
5. Do NOT overwrite `input_file_path`. Do NOT save to `input_file_path`.
6. Do NOT output markdown formatting (like ```python). Output ONLY raw Python code.
7. Do NOT redefine `input_file_path` or `file_path`.
8. Preserve existing content and formatting unless the user explicitly asks to change or remove it.
"""

# --- FILE TYPE CONFIG ---
FILE_TYPE_CONFIG: dict[str, dict[str, Any]] = {
    "excel": {"ext": ".xlsx", "system_prompt": EXCEL_SYSTEM_PROMPT, "modify_system_prompt": EXCEL_MODIFY_SYSTEM_PROMPT, "label": "Excel"},
    "docx":  {"ext": ".docx", "system_prompt": DOCX_SYSTEM_PROMPT, "modify_system_prompt": DOCX_MODIFY_SYSTEM_PROMPT, "label": "Word"},
}

# --- HELPER FUNCTIONS ---

def extract_code(llm_response: str) -> str:
    """Clean markdown code blocks if the LLM ignores instructions."""
    pattern = r"```(?:python)?(.*?)```"
    match = re.search(pattern, llm_response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return llm_response.strip()

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
_EXT_TO_FILE_TYPE = {".xlsx": "excel", ".xls": "excel", ".docx": "docx"}

# Content-Type to file type mapping
_CONTENT_TYPE_TO_FILE_TYPE = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "excel",
    "application/vnd.ms-excel": "excel",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
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


# --- MCP TOOL ---

@mcp.tool
async def generate_file(
    instructions: str,
    file_type: str = "excel",
    filename_hint: str = "output",
) -> str:
    """Generate an Excel or Word file from natural language instructions.

    Takes user instructions and a file_type, generates Python code to build
    the file, executes it in a sandbox, uploads to Open WebUI, and returns
    a download link. Self-corrects if code generation fails.

    Args:
        instructions: Natural language description of the file to generate.
        file_type: File type to generate: 'excel' or 'docx'.
        filename_hint: Base name for the generated file (UUID suffix added automatically).
    """

    # 0. Validate file_type
    if file_type not in FILE_TYPE_CONFIG:
        raise ToolError(
            f"Unsupported file_type '{file_type}'. Must be one of: {', '.join(FILE_TYPE_CONFIG)}"
        )
    config = FILE_TYPE_CONFIG[file_type]

    # 1. Setup paths
    filename = f"{filename_hint}_{uuid.uuid4().hex[:8]}{config['ext']}"
    file_path = os.path.join(STORAGE_DIR, filename)

    # 2. The Healing Loop
    max_attempts = 4
    current_code = ""
    last_error = ""

    log.info(f"=== New {config['label']} request: '{instructions}' | file: {filename} ===")
    log.info(f"Target path: {file_path}")

    for attempt in range(max_attempts):
        try:
            # A. Generate Code (Fresh or Fix)
            if attempt == 0:
                log.info(f"--- Attempt {attempt + 1}/{max_attempts}: fresh generation ---")
                current_code = await generate_code(instructions, config["system_prompt"])
            else:
                log.info(f"--- Attempt {attempt + 1}/{max_attempts}: self-correction (error: {last_error}) ---")
                current_code = await generate_code(instructions, config["system_prompt"], last_error, current_code)

            # B. Execute in isolated subprocess
            await run_code_in_subprocess(current_code, file_path)

            # C. Verify File Creation
            if not os.path.exists(file_path):
                raise Exception(
                    f"Code ran without error, but no file was found at '{file_path}'. "
                    "Did you forget to save/close the file?"
                )

            # D. Upload to Open WebUI
            log.info(f"Uploading {filename} to Open WebUI...")
            download_url = await upload_file(file_path, filename)

            log.info(f"Success on attempt {attempt + 1}: {download_url}")
            return (
                f"{config['label']} file generated successfully "
                f"(attempt {attempt + 1} of {max_attempts}).\n"
                f"Download: {download_url}"
            )

        except ToolError:
            raise
        except Exception as e:
            last_error = str(e)
            log.error(f"Attempt {attempt + 1} failed: {last_error}", exc_info=True)
            # Clean up partial output file before retrying
            try:
                os.remove(file_path)
            except OSError:
                pass

    # 3. Failure after all attempts
    log.error(f"All {max_attempts} attempts exhausted. Last error: {last_error}")
    raise ToolError(
        f"Failed to generate valid {config['label']} file after {max_attempts} attempts. "
        f"Last error: {last_error}"
    )


@mcp.tool
async def modify_file(
    file_id: str,
    instructions: str,
    filename_hint: str = "modified",
) -> str:
    """Modify an existing Excel or Word file using natural language instructions.

    Downloads the file from Open WebUI by file_id, uses an LLM to generate
    Python code that reads and modifies it, executes the code in a sandbox,
    uploads the result back to Open WebUI, and returns a download link.

    Args:
        file_id: The Open WebUI file ID of the file to modify.
        instructions: Natural language description of the modifications to make.
        filename_hint: Base name for the modified file (UUID suffix added automatically).
    """

    # 0. Download the original file and detect its type
    try:
        input_file_path, file_type = await download_file(file_id)
    except RuntimeError as e:
        raise ToolError(f"Failed to download file: {e}")

    config = FILE_TYPE_CONFIG[file_type]

    # 1. Setup output path
    filename = f"{filename_hint}_{uuid.uuid4().hex[:8]}{config['ext']}"
    file_path = os.path.join(STORAGE_DIR, filename)

    # 2. The Healing Loop
    max_attempts = 4
    current_code = ""
    last_error = ""

    log.info(f"=== New {config['label']} modify request: '{instructions}' | input: {input_file_path} | output: {filename} ===")

    try:
        for attempt in range(max_attempts):
            try:
                # A. Generate Code (Fresh or Fix)
                system_prompt = config["modify_system_prompt"]
                if attempt == 0:
                    log.info(f"--- Attempt {attempt + 1}/{max_attempts}: fresh generation ---")
                    current_code = await generate_code(
                        instructions, system_prompt,
                        input_file_path=input_file_path,
                    )
                else:
                    log.info(f"--- Attempt {attempt + 1}/{max_attempts}: self-correction (error: {last_error}) ---")
                    current_code = await generate_code(
                        instructions, system_prompt,
                        last_error, current_code,
                        input_file_path=input_file_path,
                    )

                # B. Execute in isolated subprocess
                await run_code_in_subprocess(current_code, file_path, input_file_path=input_file_path)

                # C. Verify File Creation
                if not os.path.exists(file_path):
                    raise Exception(
                        f"Code ran without error, but no file was found at '{file_path}'. "
                        "Did you forget to save the modified file?"
                    )

                # D. Upload to Open WebUI
                log.info(f"Uploading {filename} to Open WebUI...")
                download_url = await upload_file(file_path, filename)

                log.info(f"Success on attempt {attempt + 1}: {download_url}")
                return (
                    f"Modified {config['label']} file generated successfully "
                    f"(attempt {attempt + 1} of {max_attempts}).\n"
                    f"Download: {download_url}"
                )

            except ToolError:
                raise
            except Exception as e:
                last_error = str(e)
                log.error(f"Attempt {attempt + 1} failed: {last_error}", exc_info=True)
                # Clean up partial output file before retrying
                try:
                    os.remove(file_path)
                except OSError:
                    pass

        # 3. Failure after all attempts
        log.error(f"All {max_attempts} attempts exhausted. Last error: {last_error}")
        raise ToolError(
            f"Failed to modify {config['label']} file after {max_attempts} attempts. "
            f"Last error: {last_error}"
        )
    finally:
        # Clean up the downloaded input file
        try:
            os.remove(input_file_path)
        except OSError:
            pass


# --- HEALTH CHECK ---

@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Health check endpoint for orchestrators and monitoring."""
    return JSONResponse({"status": "ok"})

# --- ASGI APP ---

app = mcp.http_app(path="/mcp", stateless_http=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)