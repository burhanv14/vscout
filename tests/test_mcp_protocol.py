"""End-to-end MCP protocol tests.

These tests use FastMCP's in-process test client to communicate with the MCP
server, exercising the same JSON-RPC tool protocol an IDE client would use.
"""

import json

from fastmcp.exceptions import ToolError

import pytest
from fastmcp import Client

from oss_agent.mcp_server import server


@pytest.mark.asyncio
async def test_mcp_server_initializes():
    """Test 1: The MCP server initializes correctly."""
    async with Client(server) as client:
        result = await client.call_tool("get_capabilities", {})
        assert result is not None
        assert not result.is_error


@pytest.mark.asyncio
async def test_mcp_tools_discovery():
    """Test 2: MCP clients can discover the registered tools."""
    async with Client(server) as client:
        tools = await client.list_tools()
        tool_names = [t.name for t in tools]
        expected = {
            "discover_repositories",
            "discover_issues",
            "analyze_issue",
            "get_capabilities",
            "get_state",
        }
        assert expected.issubset(
            set(tool_names)
        ), f"Missing tools: {expected - set(tool_names)}"
        for tool in tools:
            assert tool.name
            assert tool.description
            assert tool.input_schema


@pytest.mark.asyncio
async def test_mcp_tool_input_validation():
    """Test 3: MCP tools validate their inputs."""
    async with Client(server) as client:
        # Call analyze_issue without required issue_url -> should return error
        with pytest.raises(ToolError):
            await client.call_tool("analyze_issue", {})


@pytest.mark.asyncio
async def test_mcp_get_capabilities_tool():
    """Test 4: get_capabilities tool executes successfully."""
    async with Client(server) as client:
        result = await client.call_tool("get_capabilities", {})
        assert not result.is_error
        text = result.content[0].text
        data = json.loads(text)
        assert data.get("operation") == "get_capabilities"
        assert len(data.get("tools", [])) == 5


@pytest.mark.asyncio
async def test_mcp_unknown_tool_error():
    """Test 6: Errors are returned appropriately for unknown tools."""
    async with Client(server) as client:
        with pytest.raises(ToolError):
            await client.call_tool("nonexistent_tool", {})


@pytest.mark.asyncio
async def test_mcp_repeated_requests():
    """Test 10: The application handles repeated requests correctly."""
    async with Client(server) as client:
        # Call get_capabilities twice
        resp1 = await client.call_tool("get_capabilities", {})
        resp2 = await client.call_tool("get_capabilities", {})

        for resp in (resp1, resp2):
            assert not resp.is_error
