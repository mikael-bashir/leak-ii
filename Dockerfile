# 1. Start from a standard Ubuntu image
FROM ubuntu:22.04

# 2. Install basic requirements (Nginx is removed because we no longer need a proxy)
RUN apt-get update && apt-get install -y curl git build-essential && rm -rf /var/lib/apt/lists/*

# 3. Install Elan (The official Lean version manager)
ENV ELAN_HOME="/root/.elan"
ENV PATH="${ELAN_HOME}/bin:${PATH}"
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y

# 4. Install uv (Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# 5. Set up your project workspace
WORKDIR /workspace
COPY . .

# --- THE SOURCE CODE PATCH ---
# 6. Clone the actual source code of lean-lsp-mcp instead of using uvx
RUN git clone https://github.com/oOo0oOo/lean-lsp-mcp.git /opt/lean-lsp-mcp

# 7. Inject your security bypass setting into the server initialization
RUN sed -i 's/Server("lean-lsp-mcp")/Server("lean-lsp-mcp", transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))/g' /opt/lean-lsp-mcp/src/lean_lsp_mcp/server.py
RUN sed -i '1i from mcp.server.transport_security import TransportSecuritySettings' /opt/lean-lsp-mcp/src/lean_lsp_mcp/server.py

# 8. Install the modified package
WORKDIR /opt/lean-lsp-mcp
RUN uv pip install --system -e .

# 9. Return to your Lean project and set up the Hugging Face cache
WORKDIR /workspace
RUN mkdir -p /data/.lake
RUN ln -s /data/.lake /workspace/.lake
RUN lake build

# 10. Expose the port Hugging Face expects
EXPOSE 7860

# 11. Environment variables
ENV LEAN_PROJECT_PATH=/workspace
ENV LEAN_MCP_DISABLED_TOOLS="lean_build"

# 12. Boot your custom, patched server directly on 0.0.0.0
CMD ["python3", "-m", "lean_lsp_mcp.server", "--transport", "streamable-http", "--port", "7860", "--host", "0.0.0.0"]