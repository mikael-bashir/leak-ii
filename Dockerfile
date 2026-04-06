# ... (Keep all your standard Ubuntu, Elan, and uv installations up top) ...

WORKDIR /workspace
COPY . .

# 1. Clone the actual source code of lean-lsp-mcp instead of using uvx
RUN git clone https://github.com/oOo0oOo/lean-lsp-mcp.git /opt/lean-lsp-mcp

# 2. Inject your exact setting into the server initialization
# This searches for the Server initialization and injects the disabled security setting
RUN sed -i 's/Server("lean-lsp-mcp")/Server("lean-lsp-mcp", transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))/g' /opt/lean-lsp-mcp/src/lean_lsp_mcp/server.py
# (We also need to make sure the object is imported in that file)
RUN sed -i '1i from mcp.server.transport_security import TransportSecuritySettings' /opt/lean-lsp-mcp/src/lean_lsp_mcp/server.py

# 3. Install the modified package natively
WORKDIR /opt/lean-lsp-mcp
RUN uv pip install --system -e .

WORKDIR /workspace
# --- HF BUCKET CACHE SETUP ---
RUN mkdir -p /data/.lake
RUN ln -s /data/.lake /workspace/.lake
RUN lake build

EXPOSE 7860
ENV LEAN_PROJECT_PATH=/workspace
ENV LEAN_MCP_DISABLED_TOOLS="lean_build"

# Boot the custom server directly on 0.0.0.0 (No Nginx proxy needed at all)
CMD ["python3", "-m", "lean_lsp_mcp.server", "--transport", "streamable-http", "--port", "7860", "--host", "0.0.0.0"]