# Start from the official Lean 4 nightly image
FROM leanprover/lean4:nightly 

# Install uv (Python package manager required by lean-lsp-mcp)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Set up the workspace
WORKDIR /workspace

# Copy everything from your repository into the container
COPY . .

# --- HF BUCKET CACHE SETUP ---
# Create the /data directory (where your HF bucket mounts)
RUN mkdir -p /data/.lake
# Symlink the local project cache to the persistent bucket
RUN ln -s /data/.lake /workspace/.lake

# Pre-build the project so the LSP starts fast
RUN lake build

# Hugging Face Spaces require web apps to listen on port 7860
EXPOSE 7860

# Secure the server and set the project path
ENV LEAN_PROJECT_PATH=/workspace
ENV LEAN_MCP_DISABLED_TOOLS="lean_run_code,lean_build"

# Start the server using the streamable-http transport
CMD ["uvx", "lean-lsp-mcp", "--transport", "streamable-http", "--port", "7860", "--host", "0.0.0.0"]