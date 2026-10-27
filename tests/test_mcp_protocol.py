"""End-to-end MCP protocol tests.

These tests spawn the MCP server as a subprocess and communicate over
stdio using the JSON-RPC protocol, exactly as an MCP client would.
"""

import asyncio
import json
import sys
import pytest


async def _run_mcp_session(messages):
    """Send a list of JSON-RPC messages to the MCP server and return responses."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "oss_agent.mcp_server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def send(msg):
        line = json.dumps(msg) + "\n"
        proc.stdin.write(line.encode())
        await proc.stdin.drain()

    async def recv():
        line = await proc.stdout.readline()
        if not line:
            return None
        return json.loads(line.decode())

    responses = []
    for msg in messages:
        await send(msg)
        resp = await recv()
        responses.append(resp)

    proc.stdin.close()
    await proc.wait()
    return responses


@pytest.mark.asyncio
async def test_mcp_server_initializes():
    """Test 1: The MCP server initializes correctly."""
    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
    }
    responses = await _run_mcp_session([init_msg])
    resp = responses[0]
    assert resp is not None
    assert resp.get("jsonrpc") == "2.0"
    assert resp.get("id") == 1
    result = resp.get("result", {})
    assert "protocolVersion" in result
    assert "capabilities" in result
    assert "serverInfo" in result


@pytest.mark.asyncio
async def test_mcp_tools_discovery():
    """Test 2: MCP clients can discover the registered tools."""
    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
    }
    list_msg = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }
    responses = await _run_mcp_session([init_msg, list_msg])
    tools_resp = responses[1]
    assert tools_resp is not None
    result = tools_resp.get("result", {})
    tools = result.get("tools", [])
    tool_names = [t["name"] for t in tools]
    expected = {
        "discover_repositories",
        "discover_issues",
        "analyze_issue",
        "get_capabilities",
        "get_state",
    }
    assert expected.issubset(set(tool_names)), f"Missing tools: {expected - set(tool_names)}"
    # Verify each tool has a description and input_schema
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool


@pytest.mark.asyncio
async def test_mcp_tool_input_validation():
    """Test 3: MCP tools validate their inputs."""
    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
    }

    # Call analyze_issue without required issue_url -> should return error
    bad_call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "analyze_issue", "arguments": {}},
    }
    responses = await _run_mcp_session([init_msg, bad_call])
    resp = responses[1]
    result = resp.get("result", {})
    # The tool handler should catch the missing argument and return an error
    assert result.get("isError") is True or "error" in str(result.get("content", ""))


@pytest.mark.asyncio
async def test_mcp_get_capabilities_tool():
    """Test 4: get_capabilities tool executes successfully."""
    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
    }
    caps_msg = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "get_capabilities", "arguments": {}},
    }
    responses = await _run_mcp_session([init_msg, caps_msg])
    resp = responses[1]
    result = resp.get("result", {})
    assert not result.get("isError", False)
    content = result.get("content", [])
    assert len(content) > 0
    text = content[0].get("text", "")
    data = json.loads(text)
    assert data.get("operation") == "get_capabilities"
    assert len(data.get("tools", [])) == 5


@pytest.mark.asyncio
async def test_mcp_unknown_tool_error():
    """Test 6: Errors are returned appropriately for unknown tools."""
    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
    }
    bad_msg = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "nonexistent_tool", "arguments": {}},
    }
    responses = await _run_mcp_session([init_msg, bad_msg])
    resp = responses[1]
    result = resp.get("result", {})
    assert result.get("isError") is True


@pytest.mark.asyncio
async def test_mcp_repeated_requests():
    """Test 10: The application handles repeated requests correctly."""
    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
    }
    caps_msg = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "get_capabilities", "arguments": {}},
    }
    # Send the same call twice
    messages = [init_msg, caps_msg, caps_msg]
    responses = await _run_mcp_session(messages)
    assert len(responses) == 3
    for resp in responses:
        result = resp.get("result", {})
        assert not result.get("isError", False)


@pytest.mark.asyncio
async def test_mcp_stdout_contains_only_protocol_messages():
    """Test 11: stdout contains only valid MCP protocol messages during stdio operation.

    We verify this indirectly: the server logs to stderr, and our
    stdout reads return valid JSON-RPC responses. If the server wrote
    logs to stdout, JSON parsing would fail.
    """
    init_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0.0"},
        },
    }
    list_msg = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }
    responses = await _run_mcp_session([init_msg, list_msg])
    # Every response parsed as valid JSON (otherwise _run_mcp_session would raise)
    assert all(r is not None for r in responses)
    assert all("jsonrpc" in r for r in responses)
