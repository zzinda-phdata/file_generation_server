# OpenWebUI File Agent

An MCP (Model Context Protocol) tool server built with FastMCP that generates and modifies Excel (`.xlsx`) and Word (`.docx`) files from natural language instructions. It uses an LLM to write Python code, executes it in a sandboxed subprocess, and uploads the result to Open WebUI. Open WebUI connects to this server natively as an MCP tool server via Streamable HTTP transport.

## How It Works

**Creating a file:**
1. You ask for a file in natural language (e.g. *"Create a quarterly budget spreadsheet"*)
2. Open WebUI calls the `generate_file` MCP tool
3. The server asks an LLM to generate Python code using `xlsxwriter` (Excel) or `python-docx` (Word)
4. The code runs in an isolated subprocess (no access to API keys)
5. The resulting file is uploaded to Open WebUI
6. You get back a download URL

**Modifying a file:**
1. You ask to modify an existing file (e.g. *"Add a totals row to this spreadsheet"*)
2. Open WebUI calls the `modify_file` MCP tool with the file ID
3. The server downloads the original file, then asks an LLM to generate modification code using `openpyxl` (Excel) or `python-docx` (Word)
4. The code runs in an isolated subprocess
5. The modified file is uploaded to Open WebUI
6. You get back a download URL

If the generated code fails, the server automatically retries up to 3 times, feeding the error back to the LLM for self-correction.

## MCP Tools

### `generate_file`

Create a new Excel or Word file from scratch.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `instructions` | string | *(required)* | Natural language description of the file to generate |
| `file_type` | string | `"excel"` | `"excel"` for `.xlsx` or `"docx"` for `.docx` |
| `filename_hint` | string | `"output"` | Base filename (a UUID suffix is added automatically) |

### `modify_file`

Modify an existing Excel or Word file.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `file_id` | string | *(required)* | The Open WebUI file ID of the file to modify |
| `instructions` | string | *(required)* | Natural language description of the modifications to make |
| `filename_hint` | string | `"modified"` | Base filename (a UUID suffix is added automatically) |

### `analyze_file`

Analyze a CSV or Excel file using an agentic multi-round approach with a checker sub-agent.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `file_id` | string | *(required)* | The Open WebUI file ID of the file to analyze |
| `instructions` | string | *(required)* | Natural language description of the analysis to perform |

Returns natural language analysis results followed by a methodology section with all code and outputs for auditability. The tool runs up to 3 rounds of analysis and includes a checker sub-agent that verifies results.

## Prerequisites

- **Docker** and **Docker Compose** or **Podman**
- An **LLM API key** (OpenAI, or any OpenAI-compatible endpoint)
- A running **Open WebUI** instance with an API key

## Quick Start

```bash
# 1. Clone the repository and navigate to it
git clone <repo-url> && cd my_local_directory

# 2. Create your .env from the template
cp .sample_env .env

# 3. Fill in your API keys (see Configuration below)

# 4. Build and run
docker compose up --build
```

The server starts on **port 8000**. The MCP endpoint is at `http://localhost:8000/mcp`.

Verify it's running:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

## Open WebUI Setup

1. Go to **Admin Settings > Tools > MCP Servers**
2. Add server URL: `http://host.docker.internal:8000/mcp` (if both run in Docker) or `http://localhost:8000/mcp` (if File Agent runs on host)
3. The `generate_file` and `modify_file` tools should appear in the chat tool list

## Configuration

Copy `.sample_env` to `.env` and fill in the values:

```env
# LLM Configuration
OPENAI_API_KEY=sk-your-key-here
OPENAI_BASE_URL=https://api.openai.com/v1
MODEL_NAME=gpt-4o

# Subprocess
SCRIPT_TIMEOUT=90

# Open WebUI
INTERNAL_API_URL=http://host.docker.internal:8080
PUBLIC_DOMAIN=http://localhost:8080
OPENWEBUI_API_KEY=sk-your-openwebui-key-here
```

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | API key for the LLM provider |
| `OPENAI_BASE_URL` | LLM endpoint (works with OpenAI, Ollama, vLLM, etc.) |
| `MODEL_NAME` | Model to use for code generation |
| `SCRIPT_TIMEOUT` | Max seconds for generated code to execute (default: 90) |
| `INTERNAL_API_URL` | Open WebUI URL **from the service's perspective** (for file uploads) |
| `PUBLIC_DOMAIN` | Open WebUI URL **from the user's browser** (for download links) |
| `OPENWEBUI_API_KEY` | Open WebUI API key for file uploads |

## Development

```bash
pip install -r requirements.txt
python main.py
```

This starts a single Uvicorn worker on port 8000. For production, use Docker which runs Gunicorn with 4 Uvicorn workers.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `"No file found at file_path"` | LLM redefined `file_path` in generated code | Check debug logs; the regex stripping should handle this |
| `"Server disconnected"` / timeout | Gunicorn worker timeout too low | Increase `--timeout` in the Dockerfile CMD |
| `"Cannot connect to host localhost"` | Docker networking misconfiguration | Use `host.docker.internal` for `INTERNAL_API_URL` |
| Upload returns 401 | Invalid or missing Open WebUI API key | Regenerate key in Open WebUI Settings > Account > API Keys |
| Packages not found in subprocess | Wrong Python interpreter | Check the startup log `Python executable = ...` |
| `"Cannot determine file type"` | Unrecognized file in modify_file | Check debug logs for raw response headers |

## Sample Conversations and Outputs

### Excel Generation

![Sample Excel conversation](images/excel-conversation.png)

![Sample Excel output](images/excel-output.png)

![Sample Excel conversation](images/excel-conversation2a.png)

![Sample Excel conversation](images/excel-conversation2b.png)

![Sample Excel output](images/excel-output2.png)

### Word Document Generation

![Sample DocX conversation](images/docx-conversation.png)

![Sample DocX output](images/docx-output.png)
