import os
import sys
import subprocess
import uuid
import logging
import re
import tempfile
from typing import Optional
import aiohttp
import uvicorn

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import OpenAI

# --- LOGGING ---
log = logging.getLogger("openwebui_excel")
log.setLevel(logging.DEBUG)
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
log.addHandler(_handler)
log.propagate = False

# --- CONFIGURATION ---
app = FastAPI(title="OpenWebUI Excel Agent", version="1.0.0")

# Temporary directory for generated files
STORAGE_DIR = tempfile.mkdtemp(prefix="chs_excel_")
SCRIPT_TIMEOUT = int(os.getenv("SCRIPT_TIMEOUT", "90"))
log.info(f"STORAGE_DIR = {STORAGE_DIR}")
log.info(f"SCRIPT_TIMEOUT = {SCRIPT_TIMEOUT}")
log.info(f"Python executable = {sys.executable}")

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "sk-placeholder"), 
    base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1") 
)
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o")

# --- THE SYSTEM PROMPT ---
SYSTEM_PROMPT = """
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

# --- DATA MODELS ---
class InstructionRequest(BaseModel):
    instructions: str
    filename_hint: Optional[str] = "output"

class FileResponse(BaseModel):
    status: str
    download_url: str
    message: str
    attempts: int

# --- HELPER FUNCTIONS ---

def extract_code(llm_response: str) -> str:
    """Clean markdown code blocks if the LLM ignores instructions."""
    pattern = r"```python(.*?)```"
    match = re.search(pattern, llm_response, re.DOTALL)
    if match:
        return match.group(1).strip()
    return llm_response.strip()

def generate_code(instructions: str, file_path: str, error_context: str = None, previous_code: str = None) -> str:
    """Calls the LLM to generate or fix code."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(file_path)}]

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
    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        temperature=0.01
    )
    raw = response.choices[0].message.content
    code = extract_code(raw)
    log.debug(f"Generated code:\n{code}")
    return code

def run_code_in_subprocess(code: str, file_path: str) -> None:
    """Runs LLM-generated code in an isolated subprocess with no access to
    the parent process's environment variables (API keys, etc.)."""
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
        result = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=SCRIPT_TIMEOUT,
            env=safe_env,
        )
        log.info(f"Subprocess exited with code {result.returncode}")
        if result.stdout:
            log.info(f"[stdout] {result.stdout.strip()}")
        if result.stderr:
            log.warning(f"[stderr] {result.stderr.strip()}")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"Script exited with code {result.returncode}")

        log.info(f"Checking for output file at: {file_path}")
        log.info(f"File exists: {os.path.exists(file_path)}")
        if os.path.exists(file_path):
            log.info(f"File size: {os.path.getsize(file_path)} bytes")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Script timed out after {SCRIPT_TIMEOUT} seconds")
    finally:
        try:
            os.remove(script_path)
        except OSError:
            pass


async def upload_file(local_path: str, filename: str) -> str:
    """Uploads a generated file to Open WebUI and returns a download URL."""
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

# --- THE ENDPOINT ---

@app.post("/generate-excel", response_model=FileResponse, operation_id="generateExcel")
async def generate_excel(request: InstructionRequest):
    """
    Takes user instructions, generates Python code to build an Excel file,
    executes it, and returns a download link. Self-corrects if code fails.
    """
    
    # 1. Setup paths
    filename = f"{request.filename_hint}_{uuid.uuid4().hex[:8]}.xlsx"
    file_path = os.path.join(STORAGE_DIR, filename)
    
    # 2. The Healing Loop
    max_retries = 3
    current_code = ""
    last_error = ""
    
    log.info(f"=== New request: '{request.instructions}' | file: {filename} ===")
    log.info(f"Target path: {file_path}")

    for attempt in range(max_retries + 1):
        try:
            # A. Generate Code (Fresh or Fix)
            if attempt == 0:
                log.info(f"--- Attempt {attempt}: fresh generation ---")
                current_code = generate_code(request.instructions, file_path)
            else:
                log.info(f"--- Attempt {attempt}: self-correction (error: {last_error}) ---")
                current_code = generate_code(request.instructions, file_path, last_error, current_code)

            # B. Execute in isolated subprocess
            run_code_in_subprocess(current_code, file_path)

            # C. Verify File Creation
            if not os.path.exists(file_path):
                raise Exception(
                    f"Code ran without error, but no file was found at '{file_path}'. "
                    "Did you forget `workbook.close()`?"
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
                message="Excel file generated successfully.",
                attempts=attempt + 1
            )

        except Exception as e:
            last_error = str(e)
            log.error(f"Attempt {attempt} failed: {last_error}", exc_info=True)

    # 3. Failure after retries
    log.error(f"All {max_retries + 1} attempts exhausted. Last error: {last_error}")
    raise HTTPException(status_code=500, detail=f"Failed to generate valid Excel file after {max_retries} attempts. Last error: {last_error}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)