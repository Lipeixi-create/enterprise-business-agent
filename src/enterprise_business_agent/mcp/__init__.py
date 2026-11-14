"""MCP server transport for the existing read-only business tools.

MCP is a transport, not an execution path: every call is re-authorized by
ToolRegistry and executed through AnalysisGateway, SemanticQueryGuard and
SafeQueryExecutor. No MCP tool receives SQL or a database connection.
"""

from .server import MCP_PROTOCOL_VERSIONS, McpToolServer

__all__ = ["MCP_PROTOCOL_VERSIONS", "McpToolServer"]
