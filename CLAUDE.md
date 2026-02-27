# File Agent

## What This Is

An MCP (Model Context Protocol) tool server built with FastMCP that takes natural language instructions, uses an LLM to generate Python code (xlsxwriter for Excel, python-docx for Word), executes it in a sandboxed subprocess, and uploads the resulting file to Open WebUI. Open WebUI connects to this server natively as an MCP tool server via Streamable HTTP transport.

## Architecture

```
Open WebUI  -->  MCP tool call: generate_file()  -->  LLM generates Python code
                 (Streamable HTTP at /mcp)             (xlsxwriter or python-docx)
                                                  -->  Code runs in isolated subprocess
                                                  -->  .xlsx/.docx uploaded to Open WebUI
                                                  -->  Download URL returned as text
```

Single file app (`main.py`) with these key components:

- **`generate_code()`** — Calls LLM (OpenAI-compatible API) to produce raw Python code using the appropriate system prompt
- **`extract_code()`** — Strips markdown fences if LLM ignores formatting rules
- **`run_code_in_subprocess()`** — Executes LLM code in a subprocess with a stripped environment (no API keys), enforced timeout, and `file_path` reassignment stripping
- **`upload_file()`** — Async upload to Open WebUI's `/api/v1/files/` endpoint via aiohttp
- **`generate_file()`** — The MCP tool; orchestrates a self-healing retry loop (up to 3 retries) for any supported file type
- **`FILE_TYPE_CONFIG`** — Dict mapping file types to their extension, system prompt, and label

## MCP Tool

The server exposes one tool via `@mcp.tool`:

```
generate_file(instructions: str, file_type: str = "excel", filename_hint: str = "output") -> str
```

- `instructions` — Natural language description of the file to generate
- `file_type` — `"excel"` or `"docx"`
- `filename_hint` — Base name for the generated file (UUID suffix added automatically)
- Returns a plain string with status and download URL
- Raises `ToolError` for invalid file types or exhausted retries

## Supported File Types

| `file_type` | Library | Extension | System Prompt |
|---|---|---|---|
| `excel` | xlsxwriter | `.xlsx` | `EXCEL_SYSTEM_PROMPT` |
| `docx` | python-docx | `.docx` | `DOCX_SYSTEM_PROMPT` |

## Key Design Decisions

- **FastMCP with `stateless_http=True`**: Passed to `mcp.http_app()`. Each MCP request is independent — no session state. Safe with multiple gunicorn workers for concurrent multi-user support.
- **Single MCP tool**: `generate_file()` handles all file types via the `file_type` parameter and `FILE_TYPE_CONFIG` lookup. Adding a new file type requires only a new system prompt and config entry.
- **ASGI export**: `app = mcp.http_app(path="/mcp")` exports a Starlette ASGI app. Gunicorn/uvicorn serve it the same way they served the old FastAPI app.
- **Subprocess sandboxing**: LLM-generated code runs via `asyncio.create_subprocess_exec(sys.executable, script_path)` with `env={"PATH": ...}` only. This prevents the generated code from accessing API keys or server env vars.
- **`sys.executable`** is used instead of `"python"` so the subprocess uses the same interpreter (and installed packages) as the app.
- **`file_path` stripping**: LLMs often redefine `file_path` in generated code despite instructions not to. A regex strips `file_path = ...` lines before prepending the correct assignment.
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
3. The `generate_file` tool should appear in the chat tool list

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
| `requirements.txt` | Python dependencies (fastmcp, xlsxwriter, python-docx, etc.) |
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
