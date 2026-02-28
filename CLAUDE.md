# File Agent

## What This Is

An MCP (Model Context Protocol) tool server built with FastMCP that generates, modifies, and analyzes Excel/Word/CSV files via natural language. It uses an LLM to generate Python code (xlsxwriter/openpyxl for Excel, python-docx for Word, pandas for analysis), executes it in a sandboxed subprocess, and uploads the resulting file to Open WebUI or returns analysis results as text. Open WebUI connects to this server natively as an MCP tool server via Streamable HTTP transport.

## Architecture

```
Open WebUI  -->  MCP tool call: generate_file()  -->  LLM generates Python code
                 (Streamable HTTP at /mcp)             (xlsxwriter or python-docx)
                                                  -->  Code runs in isolated subprocess
                                                  -->  .xlsx/.docx uploaded to Open WebUI
                                                  -->  Download URL returned as text

Open WebUI  -->  MCP tool call: modify_file()    -->  Download existing file from Open WebUI
                 (Streamable HTTP at /mcp)        -->  LLM generates Python code (openpyxl or python-docx)
                                                  -->  Code runs in isolated subprocess
                                                  -->  Modified .xlsx/.docx uploaded to Open WebUI
                                                  -->  Download URL returned as text

Open WebUI  -->  MCP tool call: analyze_file()   -->  Download existing file from Open WebUI
                 (Streamable HTTP at /mcp)        -->  Agentic loop: LLM generates pandas code
                                                  -->  Code runs in isolated subprocess
                                                  -->  Output fed back to LLM for next round
                                                  -->  Checker sub-agent verifies results
                                                  -->  Analysis text returned (no file upload)
```

Single file app (`main.py`) with these key components:

- **`generate_code()`** — Calls LLM (OpenAI-compatible API) to produce raw Python code using the appropriate system prompt
- **`extract_code()`** — Strips markdown fences if LLM ignores formatting rules
- **`_llm_call()`** — Thin wrapper around `client.chat.completions.create()` with retry logic (2 retries, exponential backoff) for transient API failures. Used by all analysis LLM calls.
- **`_truncate_output()`** — Truncates subprocess output to `MAX_ANALYSIS_OUTPUT_CHARS` (30,000) before feeding to LLM context. Full output preserved in audit trail.
- **`_inspect_file()`** — Runs a deterministic (not LLM-generated) inspection script before analysis. Captures shape, dtypes, sample rows, null counts, sheet names (Excel), and header offset detection via openpyxl raw row inspection. Result injected into the first LLM message.
- **`_parse_checker_result()`** — Parses checker LLM response into `(status, content)` tuple. Handles missing markers gracefully.
- **`run_code_in_subprocess()`** — Executes LLM code in a subprocess with a stripped environment (no API keys), enforced timeout, and `file_path`/`input_file_path` reassignment stripping
- **`run_analysis_code()`** — Like `run_code_in_subprocess()` but returns captured stdout (with meaningful stderr warnings appended) instead of writing files; used by `analyze_file`
- **`upload_file()`** — Async upload to Open WebUI's `/api/v1/files/` endpoint via aiohttp
- **`download_file()`** — Async download from Open WebUI's `/api/v1/files/{id}/content` endpoint; detects file type from response headers
- **`generate_file()`** — MCP tool for creating new files; orchestrates a self-healing retry loop (up to 3 retries)
- **`modify_file()`** — MCP tool for modifying existing files; downloads the original, runs a self-healing retry loop, uploads the result
- **`analyze_file()`** — MCP tool for analyzing CSV/Excel files; runs data inspection, then agentic multi-round loop with checker sub-agent. Returns analysis results with QA section and full methodology audit trail (including timing).
- **`_run_checker()`** — Sub-agent that verifies analysis results; returns `(status, content)` where status is `'passed'`, `'failed'`, or `'unknown'`
- **`_run_analysis_redo()`** — Re-runs analysis with checker feedback when the checker flags issues. Records failed execution attempts in the audit trail instead of silently dropping them.
- **`FILE_TYPE_CONFIG`** — Dict mapping file types to their extension, system prompts (create and modify), and label

## MCP Tools

The server exposes three tools via `@mcp.tool`:

### `generate_file` — Create a new file from scratch

```
generate_file(instructions: str, file_type: str = "excel", filename_hint: str = "output") -> str
```

- `instructions` — Natural language description of the file to generate
- `file_type` — `"excel"` or `"docx"`
- `filename_hint` — Base name for the generated file (UUID suffix added automatically)
- Returns a plain string with status and download URL
- Raises `ToolError` for invalid file types or exhausted retries

### `modify_file` — Modify an existing file

```
modify_file(file_id: str, instructions: str, filename_hint: str = "modified") -> str
```

- `file_id` — The Open WebUI file ID of the file to modify
- `instructions` — Natural language description of the modifications to make
- `filename_hint` — Base name for the modified file (UUID suffix added automatically)
- File type is auto-detected from download response headers (Content-Disposition / Content-Type)
- Returns a plain string with status and download URL
- Raises `ToolError` for download failures, unrecognized file types, or exhausted retries

### `analyze_file` — Analyze a data file

```
analyze_file(file_id: str, instructions: str) -> str
```

- `file_id` — The Open WebUI file ID of the file to analyze
- `instructions` — Natural language description of the analysis to perform
- Supports CSV (`.csv`) and Excel (`.xlsx`) files
- Runs a deterministic data inspection first (shape, dtypes, sample rows, null counts, sheet names, header offset detection) and injects results into the LLM's first message
- Uses an agentic multi-round loop (up to 3 rounds) where the LLM generates pandas code, sees output, and decides what to analyze next
- A checker sub-agent verifies results; can request one redo if issues are found
- Returns: Analysis Results section, Quality Assurance section (checker status, round count, timing), and Methodology section (data inspection + all code and outputs with execution timing) for auditability
- Raises `ToolError` for download failures, unsupported file types, or failed analysis

## Supported File Types

| `file_type` | Create Library | Modify Library | Extension | Create Prompt | Modify Prompt |
|---|---|---|---|---|---|
| `excel` | xlsxwriter | openpyxl | `.xlsx` | `EXCEL_SYSTEM_PROMPT` | `EXCEL_MODIFY_SYSTEM_PROMPT` |
| `docx` | python-docx | python-docx | `.docx` | `DOCX_SYSTEM_PROMPT` | `DOCX_MODIFY_SYSTEM_PROMPT` |
| `csv` | — | — | `.csv` | — | — |

**Note**: xlsxwriter is write-only and cannot read existing files. The modification flow uses openpyxl, which can both read and write `.xlsx` files. CSV files are only supported by `analyze_file` (not `generate_file` or `modify_file`). Excel files can also be analyzed via `analyze_file`.

## Key Design Decisions

- **FastMCP with `stateless_http=True`**: Passed to `mcp.http_app()`. Each MCP request is independent — no session state. Safe with multiple gunicorn workers for concurrent multi-user support.
- **Three MCP tools**: `generate_file()` creates new files from scratch; `modify_file()` downloads and modifies existing files; `analyze_file()` reads CSV/Excel data and returns text analysis. The first two use `FILE_TYPE_CONFIG` for type-specific behavior. Adding a new generatable file type requires a create prompt, a modify prompt, and a config entry.
- **ASGI export**: `app = mcp.http_app(path="/mcp")` exports a Starlette ASGI app. Gunicorn/uvicorn serve it the same way they served the old FastAPI app.
- **Subprocess sandboxing**: LLM-generated code runs via `asyncio.create_subprocess_exec(sys.executable, script_path)` with `env={"PATH": ...}` only. This prevents the generated code from accessing API keys or server env vars.
- **`sys.executable`** is used instead of `"python"` so the subprocess uses the same interpreter (and installed packages) as the app.
- **`file_path` / `input_file_path` stripping**: LLMs often redefine path variables in generated code despite instructions not to. Regexes strip `file_path = ...` and `input_file_path = ...` lines before prepending the correct assignments.
- **Two-path subprocess design**: The modification flow injects both `input_file_path` (read-only original) and `file_path` (output) into the subprocess. This keeps the original intact for retry safety — only the output file is deleted between attempts.
- **Pre-analysis data inspection**: Before the agentic loop, `_inspect_file()` runs a deterministic script (not LLM-generated) that captures file metadata: shape, dtypes, sample rows, null counts, and for Excel files, sheet names and raw row inspection via openpyxl to detect header offsets (metadata rows above the actual data). This is injected into the first LLM message so it generates correct code on the first attempt. Included as "Step 0" in the methodology audit trail.
- **LLM call retry wrapper**: `_llm_call()` wraps all analysis LLM calls with retry logic (2 retries, exponential backoff at 1s/3s) for transient API failures (APIError, APIConnectionError, RateLimitError).
- **Output truncation**: `_truncate_output()` caps subprocess output to `MAX_ANALYSIS_OUTPUT_CHARS` (30,000 chars) before sending to LLM context. Full untruncated output is preserved in the `steps[]` list for the methodology audit trail.
- **Agentic analysis loop**: `analyze_file` uses a fundamentally different pattern from the other tools. Instead of a single-shot retry loop, it runs a multi-round conversation: generate code → run it → feed output back to LLM → LLM decides whether to continue or signal `__ANALYSIS_COMPLETE__`. Each round has its own error retry budget. Error fix prompts include the data inspection context for better self-correction.
- **Checker sub-agent**: After the analysis loop, a separate LLM call reviews the work using `ANALYSIS_CHECKER_SYSTEM_PROMPT`. Response is parsed by `_parse_checker_result()` into `(status, content)`. If status is `"failed"`, the analysis is redone once with the checker's feedback. Failed redo attempts are recorded in the audit trail (not silently dropped). The second result is accepted regardless.
- **QA section**: The analysis output includes a Quality Assurance section showing checker status (PASSED / PASSED after correction / ACCEPTED WITH CAVEATS), round count, execution timing, and a note if max rounds were reached without explicit completion.
- **Stderr warnings**: `run_analysis_code()` appends meaningful stderr warnings (filtering out FutureWarning/DeprecationWarning noise) to the returned output. This helps the LLM see and fix pandas warnings in subsequent rounds.
- **Analysis returns text, not files**: Unlike `generate_file` and `modify_file`, `analyze_file` captures subprocess stdout and returns it as text. It does not upload any files.
- **Non-root Docker user**: The container runs as `appuser`, not root.
- **Gunicorn timeout**: Set to 300s to accommodate complex prompts that require multiple LLM round-trips.

## Running

### Local
```
python main.py
```
Starts uvicorn on port 8000. MCP endpoint at `http://localhost:8000/mcp`.

### Docker
```
docker compose up --build
```
Uses Gunicorn with 4 Uvicorn workers. Environment variables loaded from `.env` via `env_file` in compose.yaml.

**Important**: After code changes, always use `--build` to rebuild the image.

## Open WebUI Configuration

1. Go to **Admin Settings > Tools > MCP Servers**
2. Add server URL: `http://host.docker.internal:8000/mcp` (if both run in Docker) or `http://localhost:8000/mcp` (if File Agent runs on host)
3. The `generate_file`, `modify_file`, and `analyze_file` tools should appear in the chat tool list

## Verification

Health check:
```
curl http://localhost:8000/health
```

MCP handshake:
```
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test"}},"id":1}'
```

## Environment Variables

Configured in `.env` (gitignored). See `.sample_env` for the template.

| Variable | Purpose | Default |
|---|---|---|
| `OPENAI_API_KEY` | LLM API key | — |
| `OPENAI_BASE_URL` | LLM endpoint (OpenAI, Ollama, vLLM, etc.) | `https://api.openai.com/v1` |
| `MODEL_NAME` | Model to use for code generation | `gpt-4o` |
| `SCRIPT_TIMEOUT` | Max seconds for subprocess execution | `90` |
| `INTERNAL_API_URL` | Open WebUI URL from inside the container | `http://localhost:8080` |
| `PUBLIC_DOMAIN` | Open WebUI URL from the browser (for download links) | `http://localhost:8080` |
| `OPENWEBUI_API_KEY` | Open WebUI API key for file uploads | — |

**Docker networking note**: `INTERNAL_API_URL` must use `host.docker.internal` instead of `localhost` when running in Docker on macOS/Windows, since `localhost` inside a container refers to the container itself.

## Files

| File | Purpose |
|---|---|
| `main.py` | Entire application |
| `Dockerfile` | Python 3.11-slim, non-root user, Gunicorn |
| `compose.yaml` | Docker Compose config, loads `.env` |
| `requirements.txt` | Python dependencies (fastmcp, xlsxwriter, openpyxl, python-docx, pandas, etc.) |
| `.env` | Real config (gitignored) |
| `.sample_env` | Template for `.env` |
| `.gitignore` | Excludes `.env` |
| `.dockerignore` | Excludes `.git`, `.env`, etc. from Docker build |

## Common Issues

- **"No file found at file_path"**: The LLM redefined `file_path` in its generated code. The regex stripping in `run_code_in_subprocess` should handle this. If it recurs, check the debug logs for the generated code.
- **"Server disconnected"**: Gunicorn worker timeout. Increase `--timeout` in the Dockerfile CMD.
- **"Cannot connect to host localhost"**: Docker networking. Use `host.docker.internal` for `INTERNAL_API_URL`.
- **Packages not found in subprocess**: Ensure `sys.executable` points to the right Python. Check the startup log line `Python executable = ...`.
- **Logging not visible**: The logger uses a dedicated `StreamHandler` on stderr to avoid being overridden by Uvicorn's logging setup.
- **MCP tool not appearing in Open WebUI**: Verify the MCP handshake works (see Verification section). Check that the server URL in Open WebUI settings matches the actual endpoint.
- **"Cannot determine file type" in modify_file/analyze_file**: Open WebUI's response headers didn't include a recognizable filename extension or Content-Type. Check the debug logs for the raw headers.
- **Old `.xls` format not supported for modification**: openpyxl only supports `.xlsx` (Office Open XML). Legacy `.xls` files (Excel 97-2003) are not supported.
- **"not supported for analysis"**: `analyze_file` only supports CSV and Excel files. Word documents cannot be analyzed.
- **Analysis timeout**: Multi-round analysis involves up to 3 subprocess runs + 5+ LLM calls. Ensure gunicorn timeout (300s) is sufficient. Increase if complex analyses time out.
