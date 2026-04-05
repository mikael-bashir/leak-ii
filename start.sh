#!/bin/bash
# Start the MCP server quietly in the background on localhost
uvx lean-lsp-mcp --transport streamable-http --port 8000 --host 127.0.0.1 &

# Start Nginx in the foreground so the container stays alive
exec nginx -g "daemon off;"