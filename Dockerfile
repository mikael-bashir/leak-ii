# Start from a standard Ubuntu image
FROM ubuntu:22.04

# Install basic requirements for Lean, Python, and our Nginx proxy
RUN apt-get update && apt-get install -y curl git build-essential nginx && rm -rf /var/lib/apt/lists/*

# Install Elan (The official Lean version manager)
ENV ELAN_HOME="/root/.elan"
ENV PATH="${ELAN_HOME}/bin:${PATH}"
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y

# Install uv (Python package manager required by lean-lsp-mcp)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Set up the workspace
WORKDIR /workspace

# Copy everything from your repository into the container
COPY . .

# --- PROXY CONFIGURATION ---
# Move our custom nginx config to the correct system folder
COPY nginx.conf /etc/nginx/sites-available/default
# Make sure the startup script is executable
RUN chmod +x /workspace/start.sh

# --- HF BUCKET CACHE SETUP ---
RUN mkdir -p /data/.lake
RUN ln -s /data/.lake /workspace/.lake
RUN lake build

# Hugging Face Spaces require web apps to listen on port 7860
EXPOSE 7860

# Secure the server and set the project path
ENV LEAN_PROJECT_PATH=/workspace
ENV LEAN_MCP_DISABLED_TOOLS="lean_build"

# Run our proxy startup script instead of directly calling uvx
CMD ["/workspace/start.sh"]