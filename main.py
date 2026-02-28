import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse

from config import mcp

# Import tool modules to trigger @mcp.tool registration
import tools      # noqa: F401 — registers generate_file, modify_file, conduct_deep_research
import analysis   # noqa: F401 — registers analyze_file


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Health check endpoint for orchestrators and monitoring."""
    return JSONResponse({"status": "ok"})


app = mcp.http_app(path="/mcp", stateless_http=True)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
