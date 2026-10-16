"""Official MCP SDK transport; tool semantics remain in application/domain layers."""
import asyncio
import json

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool

from .research import ResearchTools


async def serve(service, *, background_worker=False):
    tools = ResearchTools(service)
    with service.repository.transaction(service.campaign_id) as campaign:
        mode = campaign.execution_mode
    instructions = (tools.catalog["instructions"] + f" Campaign: {service.campaign_id}. Execution mode: {mode}. "
                    + ("An application worker is active. " if background_worker else
                       "An external application worker must process queued attempts; the tools never launch one. ")
                    + "Synthetic demo results are not trading evidence; diagnostic pilot results are not a ready Benchmark.")
    server = Server("quant-rsi-research", version="0.1.0", instructions=instructions)

    @server.list_tools()
    async def list_tools():
        return [Tool(**definition) for definition in tools.list_tools()]

    @server.call_tool(validate_input=False)
    async def call_tool(name, arguments):
        value = await asyncio.to_thread(tools.call, name, arguments)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(value, ensure_ascii=False, allow_nan=False))],
                              structuredContent=value, isError=not value["ok"])

    async def worker():
        while True:
            processed = await asyncio.to_thread(service.run_next)
            await asyncio.sleep(0.05 if processed else 0.2)

    task = asyncio.create_task(worker()) if background_worker else None
    try:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
