import anyio
import os
import sys
import logging
from mcp.server.stdio import stdio_server
from mcp.client.sse import sse_client

# Diagnostic/info logs must go to stderr. Stdout is reserved exclusively for JSON-RPC messages.
logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
logger = logging.getLogger("mcp_client_cli")

async def relay(read_stream, write_stream):
    try:
        async for msg in read_stream:
            if isinstance(msg, Exception):
                logger.error(f"Error in stream: {msg}")
                continue
            await write_stream.send(msg)
    except anyio.ClosedResourceError:
        pass
    except Exception as e:
        logger.error(f"Relay exception: {e}")

async def main():
    url = os.environ.get("MCP_SERVER_URL")
    if not url:
        sys.exit("ERROR: MCP_SERVER_URL environment variable is required")

    token = os.environ.get("MCP_TOKEN")
    if token and token.startswith("$"):
        env_var_name = token[1:]
        token = os.environ.get(env_var_name)

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with sse_client(url, headers=headers) as (sse_read, sse_write):
        async with stdio_server() as (stdio_read, stdio_write):
            async with anyio.create_task_group() as tg:
                tg.start_soon(relay, stdio_read, sse_write)
                tg.start_soon(relay, sse_read, stdio_write)

if __name__ == "__main__":
    try:
        anyio.run(main)
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as e:
        logger.error(f"MCP Stdio-to-SSE bridge failed: {e}")
        sys.exit(1)
