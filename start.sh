#!/bin/bash

# Exit immediately if a command fails
set -e

# 1. Boot the MCP server in the background on port 8000
# Pointing to the compiled .venv we built in the Dockerfile
/home/user/lean-lsp-mcp/.venv/bin/lean-lsp-mcp \
    --transport streamable-http \
    --port 8000 \
    --host 127.0.0.1 &

# 2. Boot Nginx in the foreground on the public port 7860
nginx -c /home/user/app/nginx.conf