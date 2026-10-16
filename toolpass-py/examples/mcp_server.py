"""An MCP server (the official ``mcp`` SDK, v2) whose tools toolpass secures.

The tools are plain functions under ``@secured_tool()``; the server needs no
adapter. What toolpass checks each call against is the user of the current
``Session``, which comes from the host or the agent, never from a tool
argument: the model can make those up.

Over Streamable HTTP the agent names the user in the ``X-Toolpass-User``
header of each request, and ``session_from_header`` opens the session::

    pip install mcp
    python examples/mcp_server.py -http 8000
    # the agent's MCP client sends X-Toolpass-User: dana@example.com

Over stdio the host starts one server process per user and names the user
in ``AGENT_USER``::

    AGENT_USER=dana@example.com python examples/mcp_server.py

The engine runs in this process on the ``fake`` integration, so the example
needs no toolpass server; swap in ``Toolpass.from_config("toolpass.yaml")``.
"""

from __future__ import annotations

import os
import sys

from mcp.server.mcpserver import MCPServer

import toolpass
from toolpass import Session, Toolpass, permission_check, secured_tool, session_from_header

tp = Toolpass(connections=[{"id": "demo", "integration": "fake", "users": "dana@example.com", "admins": "admin@example.com"}])
# The SDK reports an exception as only "Error executing tool", so hand the
# refusal to the model as the tool's result instead.
toolpass.configure(on_refuse=str)

mcp = MCPServer("toolpass-demo", instructions="write_thing is refused unless the user holds thing.write; a refusal says why.")


@mcp.tool()
@secured_tool(effect="write", authorize=permission_check(tp, "demo", "thing.write", "thing:{thing_id}", fresh=True))
def write_thing(thing_id: str, content: str) -> str:
    """Write content to a thing in the demo system. Requires thing.write."""
    # The action itself, run with the server's own credential. Replace with a
    # real call (delete a Jira issue, scale a deployment, ...).
    return f"wrote {len(content)} bytes to thing:{thing_id}"


@mcp.tool()
@secured_tool(effect="read")
def read_thing(thing_id: str) -> str:
    """Read a thing in the demo system. Anyone may read."""
    return f"thing:{thing_id} says hello"


def main(argv: list[str]) -> None:
    if argv[:1] == ["-http"]:
        import uvicorn

        # One agent reaches this server: on a network others reach, add
        # key=os.environ["TOOLPASS_AGENT_KEY"] and have the agent send it in X-Toolpass-Key.
        uvicorn.run(session_from_header(mcp.streamable_http_app()), host="127.0.0.1", port=int(argv[1]) if len(argv) > 1 else 8000)
        return
    user = os.environ.get("AGENT_USER")
    if not user:
        sys.exit("AGENT_USER missing: set it to the email of the user this server acts for, or run with -http")
    with Session(user).active():
        mcp.run()


if __name__ == "__main__":
    main(sys.argv[1:])
