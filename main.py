import os
import sys
import asyncio
import uuid
import logging
import re
import tempfile
from typing import Any, Optional
import aiohttp
import uvicorn

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from openai import AsyncOpenAI

# --- LOGGING ---
log = logging.getLogger("openwebui_fileagent")
log.setLevel(logging.DEBUG)
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
log.addHandler(_handler)
log.propagate = False

# --- CONFIGURATION ---
app = FastAPI(title="OpenWebUI File Agent", version="2.0.0")

# Temporary directory for generated files
STORAGE_DIR = tempfile.mkdtemp(prefix="owui_files_")
SCRIPT_TIMEOUT = int(os.getenv("SCRIPT_TIMEOUT", "90"))
log.info(f"STORAGE_DIR = {STORAGE_DIR}")
log.info(f"SCRIPT_TIMEOUT = {SCRIPT_TIMEOUT}")
log.info(f"Python executable = {sys.executable}")

client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "sk-placeholder"),
    base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
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

# --- FILE TYPE CONFIG ---
FILE_TYPE_CONFIG: dict[str, dict[str, Any]] = {
    "excel": {"ext": ".xlsx", "system_prompt": EXCEL_SYSTEM_PROMPT, "label": "Excel"},
    "docx":  {"ext": ".docx", "system_prompt": DOCX_SYSTEM_PROMPT, "label": "Word"},
}

# --- DATA MODELS ---
class InstructionRequest(BaseModel):
    """Incoming request to generate a file from natural language instructions."""
    instructions: str = Field(description="Natural language description of the file to generate")
    filename_hint: Optional[str] = Field("output", description="Base name for the generated file (UUID suffix added automatically)")
    file_type: Optional[str] = Field("excel", description="File type to generate: 'excel' or 'docx'")

class FileResponse(BaseModel):
    """Response returned after file generation and upload."""
    status: str = Field(description="'success' or HTTP error status")
    download_url: str = Field(description="URL to download the generated file from Open WebUI")
    message: str = Field(description="Human-readable result message")
    attempts: int = Field(description="Number of generation attempts used (1 = first try)")

# --- HELPER FUNCTIONS ---

def extract_code(llm_response: str) -> str:
    """Clean markdown code blocks if the LLM ignores instructions."""
    pattern = r"```python(.*?)```"
    match = re.search(pattern, llm_response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return llm_response.strip()

async def generate_code(instructions: str, file_path: str, system_prompt: str, error_context: str = None, previous_code: str = None) -> str:
    """Call the LLM to generate Python code, or to fix previously failed code.

    On the first attempt, sends user instructions directly.  On retries,
    includes the failed code and error message so the LLM can self-correct.
    """
    messages = [{"role": "system", "content": system_prompt}]

    if error_context:
        log.debug(f"Requesting code fix — error was: {error_context}")
        messages.append({
            "role": "user",
            "content": f"""
            The previous code you wrote failed.

            USER INSTRUCTIONS: {instructions}

            FAILED CODE:
            {previous_code}

            ERROR MESSAGE:
            {error_context}

            Please rewrite the code to fix this error. Ensure you still use `file_path`.
            """
        })
    else:
        log.debug(f"Requesting fresh code generation for: {instructions}")
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

async def run_code_in_subprocess(code: str, file_path: str) -> None:
    """Run LLM-generated code in an isolated subprocess.

    The subprocess inherits only PATH (no API keys or secrets).  Any
    ``file_path = ...`` assignments the LLM may have generated are stripped
    and replaced with the correct runtime path.
    """
    # Strip any file_path reassignments the LLM may have generated
    cleaned = re.sub(r"(?m)^file_path\s*=\s*.+$", "", code)
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

    Returns a URL string on success, or an error string prefixed with
    "Upload Error" or "System Error" on failure.
    """
    internal_api_url = os.getenv("INTERNAL_API_URL", "http://localhost:8080")
    public_domain = os.getenv("PUBLIC_DOMAIN", "http://localhost:8080")
    api_key = os.getenv("OPENWEBUI_API_KEY", "sk-placeholder")
    headers = {"Authorization": f"Bearer {api_key}"}
    upload_url = f"{internal_api_url.rstrip('/')}/api/v1/files/"

    try:
        form = aiohttp.FormData()
        form.add_field("file", open(local_path, "rb"), filename=filename)

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            async with session.post(upload_url, headers=headers, data=form) as resp:
                status = resp.status
                text = await resp.text()
                try:
                    data = await resp.json()
                except:
                    data = {}

        try:
            os.remove(local_path)
        except:
            pass

        if status < 200 or status >= 300:
            return f"Upload Error {status}: {text}"
        file_id = data.get("id") or data.get("uuid") or data.get("file_id")

        download_url = f"{public_domain.rstrip('/')}/api/v1/files/{file_id}/content"

        return download_url
    except Exception as e:
        return f"System Error: {str(e)}"

# --- THE ENDPOINTS ---

@app.post("/generate-file", response_model=FileResponse, operation_id="generateFile")
async def generate_file(request: InstructionRequest) -> FileResponse:
    """
    Takes user instructions and a file_type ("excel" or "docx"), generates Python
    code to build the file, executes it, and returns a download link.
    Self-corrects if code fails.
    """

    # 0. Validate file_type
    file_type = request.file_type
    if file_type not in FILE_TYPE_CONFIG:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file_type '{file_type}'. Must be one of: {', '.join(FILE_TYPE_CONFIG)}"
        )
    config = FILE_TYPE_CONFIG[file_type]

    # 1. Setup paths
    filename = f"{request.filename_hint}_{uuid.uuid4().hex[:8]}{config['ext']}"
    file_path = os.path.join(STORAGE_DIR, filename)

    # 2. The Healing Loop
    max_retries = 3
    current_code = ""
    last_error = ""

    log.info(f"=== New {config['label']} request: '{request.instructions}' | file: {filename} ===")
    log.info(f"Target path: {file_path}")

    for attempt in range(max_retries + 1):
        try:
            # A. Generate Code (Fresh or Fix)
            if attempt == 0:
                log.info(f"--- Attempt {attempt}: fresh generation ---")
                current_code = await generate_code(request.instructions, file_path, config["system_prompt"])
            else:
                log.info(f"--- Attempt {attempt}: self-correction (error: {last_error}) ---")
                current_code = await generate_code(request.instructions, file_path, config["system_prompt"], last_error, current_code)

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
            if download_url.startswith(("Upload Error", "System Error")):
                log.error(f"Upload failed: {download_url}")
                raise Exception(download_url)

            log.info(f"Success on attempt {attempt + 1}: {download_url}")
            return FileResponse(
                status="success",
                download_url=download_url,
                message=f"{config['label']} file generated successfully.",
                attempts=attempt + 1
            )

        except Exception as e:
            last_error = str(e)
            log.error(f"Attempt {attempt} failed: {last_error}", exc_info=True)

    # 3. Failure after retries
    log.error(f"All {max_retries + 1} attempts exhausted. Last error: {last_error}")
    raise HTTPException(status_code=500, detail=f"Failed to generate valid {config['label']} file after {max_retries} attempts. Last error: {last_error}")

@app.get("/health", operation_id="healthCheck")
async def health() -> dict[str, str]:
    """Health check endpoint for orchestrators and monitoring."""
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)