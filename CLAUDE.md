# Excel Agent

## What This Is

A FastAPI service that takes natural language instructions, uses an LLM to generate Python/xlsxwriter code, executes it in a sandboxed subprocess, and uploads the resulting Excel file to Open WebUI.

## Architecture

```
User/Open WebUI  -->  POST /generate-excel  -->  LLM generates xlsxwriter code
                                              -->  Code runs in isolated subprocess
                                              -->  .xlsx uploaded to Open WebUI
                                              -->  Download URL returned
```

Single file app (`main.py`) with these key components:

- **`generate_code()`** — Calls LLM (OpenAI-compatible API) to produce raw Python code
- **`extract_code()`** — Strips markdown fences if LLM ignores formatting rules
- **`run_code_in_subprocess()`** — Executes LLM code in a subprocess with a stripped environment (no API keys), enforced timeout, and `file_path` reassignment stripping
- **`upload_file()`** — Async upload to Open WebUI's `/api/v1/files/` endpoint via aiohttp
- **`generate_excel()`** — The endpoint; orchestrates a self-healing retry loop (up to 3 retries)

## Key Design Decisions

- **Subprocess sandboxing**: LLM-generated code runs via `subprocess.run([sys.executable, script_path])` with `env={"PATH": ...}` only. This prevents the generated code from accessing API keys or server env vars.
- **`sys.executable`** is used instead of `"python"` so the subprocess uses the same interpreter (and installed packages) as the app.
- **`file_path` stripping**: LLMs often redefine `file_path` in generated code despite instructions not to. A regex strips `file_path = ...` lines before prepending the correct assignment.
- **Non-root Docker user**: The container runs as `appuser`, not root.
- **Gunicorn timeout**: Set to 300s to accommodate complex prompts that require multiple LLM round-trips.

## Running

### Local
```
python main.py
```
Starts uvicorn on port 8000.

### Docker
```
docker compose up --build
```
Uses Gunicorn with 4 Uvicorn workers. Environment variables loaded from `.env` via `env_file` in compose.yaml.

**Important**: After code changes, always use `--build` to rebuild the image.

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
| `requirements.txt` | Python dependencies |
| `.env` | Real config (gitignored) |
| `.sample_env` | Template for `.env` |
| `.gitignore` | Excludes `.env` |

## Common Issues

- **"No file found at file_path"**: The LLM redefined `file_path` in its generated code. The regex stripping in `run_code_in_subprocess` should handle this. If it recurs, check the debug logs for the generated code.
- **"Server disconnected"**: Gunicorn worker timeout. Increase `--timeout` in the Dockerfile CMD.
- **"Cannot connect to host localhost"**: Docker networking. Use `host.docker.internal` for `INTERNAL_API_URL`.
- **Packages not found in subprocess**: Ensure `sys.executable` points to the right Python. Check the startup log line `Python executable = ...`.
- **Logging not visible**: The logger uses a dedicated `StreamHandler` on stderr to avoid being overridden by Uvicorn's logging setup.
