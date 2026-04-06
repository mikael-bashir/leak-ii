# 1. Standard Ubuntu
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y curl git build-essential && rm -rf /var/lib/apt/lists/*

# 2. Install Elan (Lean)
ENV ELAN_HOME="/root/.elan"
ENV PATH="${ELAN_HOME}/bin:${PATH}"
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y

# 3. Install uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# 4. Workspace & Cache setup
WORKDIR /workspace
COPY . .
RUN mkdir -p /data/.lake && ln -s /data/.lake /workspace/.lake && lake build

# 5. Environment
EXPOSE 7860
ENV LEAN_PROJECT_PATH=/workspace
ENV LEAN_MCP_DISABLED_TOOLS="lean_build"

# 6. The Clean Fix: Run with uvx, but force it to use the pre-bug version of the MCP SDK
CMD ["uvx", "--with", "mcp<1.24.0", "lean-lsp-mcp", "--transport", "streamable-http", "--port", "7860", "--host", "0.0.0.0"]