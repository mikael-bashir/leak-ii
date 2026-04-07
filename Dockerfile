# 1. Standard Ubuntu + Python
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y curl git build-essential python3 && rm -rf /var/lib/apt/lists/*

# 2. Install Elan (Lean)
ENV ELAN_HOME="/root/.elan"
ENV PATH="${ELAN_HOME}/bin:${PATH}"
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y

# 3. Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# 4. Copy your current Space files (lakefile, etc)
WORKDIR /workspace
COPY . .

# 5. Clone YOUR fork with the security fix
RUN git clone https://github.com/mikael-bashir/lean-lsp-mcp.git /opt/lean-lsp-mcp

# 6. Install your forked package globally
WORKDIR /opt/lean-lsp-mcp
RUN uv pip install --system -e .

# 7. Setup the Hugging Face Lean cache
WORKDIR /workspace
RUN mkdir -p /data/.lake && ln -s /data/.lake /workspace/.lake && lake build

EXPOSE 7860
ENV LEAN_PROJECT_PATH=/workspace
ENV LEAN_MCP_DISABLED_TOOLS="lean_build"

# 8. Boot the server cleanly!
CMD ["python3", "-m", "lean_lsp_mcp.server", "--transport", "streamable-http", "--port", "7860", "--host", "0.0.0.0"]