import os
import uuid
import time
import asyncio

from fastmcp.exceptions import ToolError
from openai import APIError, APIConnectionError, RateLimitError

from config import (
    log, mcp, client, FILE_TYPE_CONFIG, STORAGE_DIR,
    DEEP_RESEARCH_MODEL, DEEP_RESEARCH_POLL_INTERVAL, DEEP_RESEARCH_TIMEOUT,
)
from helpers import generate_code, run_code_in_subprocess, upload_file, download_file


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
    if file_type not in FILE_TYPE_CONFIG or "system_prompt" not in FILE_TYPE_CONFIG.get(file_type, {}):
        supported = [k for k, v in FILE_TYPE_CONFIG.items() if "system_prompt" in v]
        raise ToolError(
            f"Unsupported file_type '{file_type}'. Must be one of: {', '.join(supported)}"
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

    if "modify_system_prompt" not in config:
        raise ToolError(
            f"File type '{config['label']}' is not supported for modification. "
            f"Use analyze_file instead for CSV data."
        )

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


@mcp.tool
async def conduct_deep_research(
    instructions: str,
) -> str:
    """Conduct deep research on a topic using web search and synthesis.

    Uses the OpenAI Responses API with the o4-mini-deep-research model to
    search and synthesize hundreds of web sources into a comprehensive
    research report. This can take several minutes to complete.

    Args:
        instructions: Natural language description of the research to conduct.
    """
    log.info(f"=== New deep research request: '{instructions[:100]}' ===")
    t0 = time.monotonic()

    # 1. Create background research task
    try:
        response = await client.responses.create(
            model=DEEP_RESEARCH_MODEL,
            input=instructions,
            background=True,
            tools=[{"type": "web_search_preview"}],
        )
    except (APIError, APIConnectionError, RateLimitError) as e:
        log.error(f"Deep research API call failed: {e}")
        raise ToolError(f"Failed to start deep research: {e}")

    response_id = response.id
    log.info(f"Deep research created: id={response_id}, status={response.status}")

    # 2. Poll for completion
    last_status = response.status
    while response.status in ("queued", "in_progress"):
        if time.monotonic() - t0 > DEEP_RESEARCH_TIMEOUT:
            log.error(f"Deep research timed out after {DEEP_RESEARCH_TIMEOUT}s (id={response_id})")
            raise ToolError(
                f"Deep research timed out after {DEEP_RESEARCH_TIMEOUT} seconds. "
                f"The research may still be running — try again later."
            )

        await asyncio.sleep(DEEP_RESEARCH_POLL_INTERVAL)

        try:
            response = await client.responses.retrieve(response_id)
        except (APIError, APIConnectionError, RateLimitError) as e:
            log.warning(f"Poll failed (will retry): {e}")
            continue

        if response.status != last_status:
            elapsed = time.monotonic() - t0
            log.info(f"Deep research status: {last_status} -> {response.status} ({elapsed:.0f}s elapsed)")
            last_status = response.status

    elapsed = time.monotonic() - t0
    log.info(f"Deep research finished: status={response.status}, elapsed={elapsed:.1f}s")

    # 3. Handle terminal status
    if response.status != "completed":
        # Extract error details from the response
        error_detail = ""
        try:
            # The API may include error info in various places
            if hasattr(response, "error") and response.error:
                error_detail = str(response.error)
            elif hasattr(response, "last_error") and response.last_error:
                error_detail = str(response.last_error)
            elif hasattr(response, "incomplete_details") and response.incomplete_details:
                error_detail = str(response.incomplete_details)
            # Also dump output items for clues
            if hasattr(response, "output") and response.output:
                for item in response.output:
                    log.info(f"Response output item: type={getattr(item, 'type', '?')}, content={str(item)[:300]}")
        except Exception:
            pass
        log.error(f"Deep research failed: status={response.status}, error={error_detail}, response_id={response_id}")
        log.debug(f"Full failed response: {response}")
        raise ToolError(
            f"Deep research ended with status '{response.status}' after {elapsed:.0f}s. "
            f"{f'Error: {error_detail}. ' if error_detail else ''}"
            f"Please try again."
        )

    # 4. Extract results
    report = response.output_text or ""
    if not report:
        raise ToolError("Deep research completed but returned no output.")

    # 5. Extract source citations from annotations
    sources = []
    try:
        for item in response.output:
            if hasattr(item, "content"):
                for block in item.content:
                    if hasattr(block, "annotations"):
                        for ann in block.annotations:
                            url = getattr(ann, "url", None)
                            title = getattr(ann, "title", None)
                            if url and url not in [s["url"] for s in sources]:
                                sources.append({"url": url, "title": title or url})
    except Exception:
        log.debug("Could not extract annotations from response", exc_info=True)

    # 6. Format output
    result = f"## Deep Research Report\n\n{report}"

    if sources:
        result += "\n\n---\n\n## Sources\n"
        for src in sources:
            result += f"- [{src['title']}]({src['url']})\n"

    result += f"\n---\n*Completed in {elapsed:.0f}s*"

    log.info(f"Deep research complete: {len(report)} chars, {len(sources)} sources, {elapsed:.1f}s")
    return result
