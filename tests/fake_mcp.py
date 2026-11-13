"""Tiny stdio MCP server over an in-memory SQLite database, for the test."""
import sqlite3

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("fake")
db = sqlite3.connect(":memory:", check_same_thread=False)
db.executescript("create table orders (id integer, region text, amount real);"
                 "insert into orders values (1,'north',100),(2,'north',50),(3,'south',70);")


@mcp.tool()
def query(sql: str) -> str:
    """Run a SQL query against the orders database."""
    return str(db.execute(sql).fetchall())


mcp.run()
