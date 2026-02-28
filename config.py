import os
import sys
import logging
import tempfile
from typing import Any

from fastmcp import FastMCP
from openai import AsyncOpenAI

# --- LOGGING ---
log = logging.getLogger("openwebui_fileagent")
log.setLevel(logging.DEBUG)
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
log.addHandler(_handler)
log.propagate = False

# --- MCP SERVER ---
mcp = FastMCP("FileAgent")

# --- CONSTANTS ---
STORAGE_DIR = tempfile.mkdtemp(prefix="owui_files_")
SCRIPT_TIMEOUT = int(os.getenv("SCRIPT_TIMEOUT", "90"))
MAX_ANALYSIS_OUTPUT_CHARS = 30_000

log.info(f"STORAGE_DIR = {STORAGE_DIR}")
log.info(f"SCRIPT_TIMEOUT = {SCRIPT_TIMEOUT}")
log.info(f"Python executable = {sys.executable}")

# --- LLM CLIENT ---
client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY", "sk-placeholder"),
    base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    timeout=120.0,
)
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4o")
DEEP_RESEARCH_MODEL = os.getenv("DEEP_RESEARCH_MODEL", "o4-mini-deep-research")
DEEP_RESEARCH_POLL_INTERVAL = 5   # seconds between status checks
DEEP_RESEARCH_TIMEOUT = 600       # max seconds to wait for completion

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

ANALYSIS_SYSTEM_PROMPT = """
You are an expert Python data analyst. Your goal is to write a Python script using pandas to analyze a data file and print the results.

RULES:
1. A variable named `input_file_path` will be provided at runtime. Use it to read the file.
2. Detect the file type from the extension:
   - `.csv`: use `pd.read_csv(input_file_path)`
   - `.xlsx` or `.xls`: use `pd.read_excel(input_file_path)`
3. Print ALL analysis results to stdout using `print()`. This is how your results are captured.
4. Do NOT redefine `input_file_path`.
5. Do NOT output markdown formatting (like ```python). Output ONLY raw Python code.
6. Do NOT create or save any files. Only print results.
7. Use pandas, and standard library modules only.
8. Format numerical output clearly (e.g., round floats, align columns where helpful).
9. You will receive a DATA INSPECTION section showing the file's structure, columns, types,
   and sample rows. Use this to write correct code on the first attempt. Pay attention to:
   - Whether the data starts on a row other than 1 (header offset — use skiprows if needed)
   - Column data types (especially object columns that should be numeric — use pd.to_numeric with errors='coerce')
   - Missing values (NaN counts per column)
   - For Excel files with multiple sheets: which sheet(s) contain the relevant data
"""

ANALYSIS_FOLLOWUP_SYSTEM_PROMPT = """
You are an expert Python data analyst continuing a multi-step analysis.

You will receive the code and output from previous analysis steps. Based on these results and the user's original instructions, decide what to do:

OPTION A — If the analysis is COMPLETE and fully addresses the user's instructions:
  Output ONLY the text: __ANALYSIS_COMPLETE__
  Do NOT output any code.

OPTION B — If more analysis is needed:
  Output ONLY raw Python code (no markdown fences) for the next analysis step.
  The variable `input_file_path` is already defined. Do NOT redefine it.
  Print all results to stdout using `print()`.
  Do NOT create or save any files.
  Use pandas, and standard library modules only.

Choose Option A when the previous output already contains enough information to fully answer the user's question. Do not run unnecessary extra steps.
"""

ANALYSIS_CHECKER_SYSTEM_PROMPT = """
You are a senior data analyst reviewing another analyst's work. You will receive:
- The original analysis instructions
- All code that was executed (each step)
- All output produced (each step)

Your job is to verify the analysis is correct and complete.

RULES:
1. Check that the analysis actually addresses the user's instructions.
2. Check for logical errors, misinterpretations, or incorrect calculations.
3. Check that conclusions are supported by the data shown in the output.

If the analysis is CORRECT and COMPLETE:
  Start your response with: __CHECK_PASSED__
  Then write a polished, natural language summary of the analysis results.
  This summary should be clear, well-organized, and directly answer the user's question.
  Do NOT include code in this summary.

If there are SIGNIFICANT issues (wrong answers, missing key parts of the instructions, logical errors):
  Start your response with: __CHECK_FAILED__
  Then describe the specific issues that need to be fixed.
  Be concrete: say what is wrong and what the correct approach should be.

Minor formatting issues or style preferences are NOT grounds for failure. Only fail for substantive errors.
"""

# --- FILE TYPE CONFIG ---
FILE_TYPE_CONFIG: dict[str, dict[str, Any]] = {
    "excel": {"ext": ".xlsx", "system_prompt": EXCEL_SYSTEM_PROMPT, "modify_system_prompt": EXCEL_MODIFY_SYSTEM_PROMPT, "label": "Excel"},
    "docx":  {"ext": ".docx", "system_prompt": DOCX_SYSTEM_PROMPT, "modify_system_prompt": DOCX_MODIFY_SYSTEM_PROMPT, "label": "Word"},
    "csv":   {"ext": ".csv", "label": "CSV"},
}
