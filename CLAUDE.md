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

Open WebUI  -->  MCP tool call: conduct_deep_research()  -->  OpenAI Responses API
                 (Streamable HTTP at /mcp)                -->  o4-mini-deep-research model
                                                          -->  Web search across hundreds of sources
                                                          -->  Background polling until complete
                                                          -->  Research report returned as text
```

Modular design with five Python files:

- **`main.py`** — Thin entry point. Imports tool modules to trigger `@mcp.tool` registration, adds health check route, exports ASGI app.
- **`config.py`** — All configuration: logging, `mcp` instance, `client` instance, constants, all 7 system prompts, `FILE_TYPE_CONFIG`.
- **`helpers.py`** — Shared utility functions used by tools and analysis modules.
- **`tools.py`** — `generate_file`, `modify_file`, `conduct_deep_research` MCP tools.
- **`analysis.py`** — `analyze_file` MCP tool and its sub-agents (`_inspect_file`, `_run_checker`, `_run_analysis_redo`).

Import dependency graph (no circular dependencies):
```
config.py  (no internal imports)
    ↑
helpers.py (imports from config)
    ↑
tools.py & analysis.py (import from config + helpers)
    ↑
main.py (imports config.mcp, then imports tools & analysis to trigger registration)
```

Key components:

- **`generate_code()`** *(helpers)* — Calls LLM to produce raw Python code using the appropriate system prompt
- **`extract_code()`** *(helpers)* — Strips markdown fences if LLM ignores formatting rules
- **`_llm_call()`** *(helpers)* — Wrapper around `client.chat.completions.create()` with retry logic (2 retries, exponential backoff) for transient API failures
- **`_truncate_output()`** *(helpers)* — Truncates subprocess output to `MAX_ANALYSIS_OUTPUT_CHARS` (30,000) before feeding to LLM context
- **`_parse_checker_result()`** *(helpers)* — Parses checker LLM response into `(status, content)` tuple
- **`run_code_in_subprocess()`** *(helpers)* — Executes LLM code in a subprocess with a stripped environment (no API keys), enforced timeout, and `file_path`/`input_file_path` reassignment stripping
- **`run_analysis_code()`** *(helpers)* — Like `run_code_in_subprocess()` but returns captured stdout (with meaningful stderr warnings appended)
- **`upload_file()`** *(helpers)* — Async upload to Open WebUI's `/api/v1/files/` endpoint
- **`download_file()`** *(helpers)* — Async download from Open WebUI's `/api/v1/files/{id}/content` endpoint; detects file type from response headers
- **`generate_file()`** *(tools)* — MCP tool for creating new files; self-healing retry loop (up to 3 retries)
- **`modify_file()`** *(tools)* — MCP tool for modifying existing files; downloads the original, runs a self-healing retry loop, uploads the result
- **`analyze_file()`** *(analysis)* — MCP tool for analyzing CSV/Excel files; runs data inspection, then agentic multi-round loop with checker sub-agent
- **`_inspect_file()`** *(analysis)* — Runs a deterministic inspection script before analysis (shape, dtypes, sample rows, null counts, sheet names, header offset detection)
- **`_run_checker()`** *(analysis)* — Sub-agent that verifies analysis results
- **`_run_analysis_redo()`** *(analysis)* — Re-runs analysis with checker feedback
- **`conduct_deep_research()`** *(tools)* — MCP tool for deep research via OpenAI Responses API with background polling
- **`FILE_TYPE_CONFIG`** *(config)* — Dict mapping file types to their extension, system prompts, and label

## MCP Tools

The server exposes four tools via `@mcp.tool`:

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

### `conduct_deep_research` — Research a topic using web search

```
conduct_deep_research(instructions: str) -> str
```

- `instructions` — Natural language description of the research to conduct
- Uses the OpenAI Responses API with `o4-mini-deep-research` model and web search
- Runs in background mode with polling (can take several minutes)
- Returns a research report with inline citations and a sources list
- Raises `ToolError` for API failures, timeouts (`DEEP_RESEARCH_TIMEOUT`, default 600s), or empty results
- **Note**: This tool requires the OpenAI API (not compatible with alternative providers like Ollama/vLLM)

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
- **Deep research via Responses API**: `conduct_deep_research` uses a completely different API surface (`client.responses.create()` with `background=True`) from the other tools. The initial API call returns immediately; the tool then polls `client.responses.retrieve()` every 5 seconds until completion. Status transitions are logged. Resilient to transient poll failures (logs warning and continues). Citations are extracted from response annotations.
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
| `DEEP_RESEARCH_MODEL` | Model for deep research tool | `o4-mini-deep-research` |
| `DEEP_RESEARCH_POLL_INTERVAL` | Seconds between status checks (hardcoded) | `5` |
| `DEEP_RESEARCH_TIMEOUT` | Max seconds to wait for deep research (hardcoded) | `600` |
| `INTERNAL_API_URL` | Open WebUI URL from inside the container | `http://localhost:8080` |
| `PUBLIC_DOMAIN` | Open WebUI URL from the browser (for download links) | `http://localhost:8080` |
| `OPENWEBUI_API_KEY` | Open WebUI API key for file uploads | — |

**Docker networking note**: `INTERNAL_API_URL` must use `host.docker.internal` instead of `localhost` when running in Docker on macOS/Windows, since `localhost` inside a container refers to the container itself.

## Files

| File | Purpose |
|---|---|
| `main.py` | Entry point — imports modules, health check, ASGI app export |
| `config.py` | Logging, constants, system prompts, LLM client, FastMCP instance, `FILE_TYPE_CONFIG` |
| `helpers.py` | Shared utilities — code generation, subprocess execution, file upload/download |
| `tools.py` | MCP tools: `generate_file`, `modify_file`, `conduct_deep_research` |
| `analysis.py` | MCP tool: `analyze_file` + sub-agents (`_inspect_file`, `_run_checker`, `_run_analysis_redo`) |
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
- **Deep research timeout**: `conduct_deep_research` can take several minutes. The tool polls in background mode so the HTTP client timeout (120s) is not an issue, but the gunicorn worker timeout (300s) may need to be increased to 600s+ for long research tasks. The tool's own timeout is `DEEP_RESEARCH_TIMEOUT` (default 600s).
- **Deep research with non-OpenAI providers**: `conduct_deep_research` uses the OpenAI Responses API which is only available at `api.openai.com`. If `OPENAI_BASE_URL` points to a different provider, this tool will fail. The other tools (generate_file, modify_file, analyze_file) work with any OpenAI-compatible provider.
- **"'AsyncOpenAI' object has no attribute 'responses'"**: The openai Python SDK must be >= 1.75 for the Responses API. Run `pip install --upgrade openai` or check `requirements.txt`.
