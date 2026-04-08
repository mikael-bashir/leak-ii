# 1. Base Image
FROM ubuntu:22.04

# 2. CREATE THE GUEST USER
RUN useradd -m -u 1000 user

# 3. Install System Dependencies + Network Debugging Tools
RUN apt-get update && apt-get install -y \
    curl git build-essential python3 socat lsof net-tools \
    && rm -rf /var/lib/apt/lists/*

# 4. Switch to the unprivileged user
USER user
ENV HOME=/home/user
ENV PATH="${HOME}/.local/bin:${HOME}/.elan/bin:${PATH}"

# 5. Install Lean & uv (Verbose mode)
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y
RUN curl -vLsSf https://astral.sh/uv/install.sh | sh

# 6. Setup Workspace
WORKDIR ${HOME}/app
COPY --chown=user . ${HOME}/app

# 7. Clone & Pre-build your patched fork (Verbose sync)
RUN git clone https://github.com/mikael-bashir/lean-lsp-mcp.git ${HOME}/lean-lsp-mcp
WORKDIR ${HOME}/lean-lsp-mcp
RUN uv sync -v

# 8. Build the Lean project natively (Verbose build)
WORKDIR ${HOME}/app
RUN lake build -v

# 9. Environment Variables
EXPOSE 7860
ENV LEAN_PROJECT_PATH=${HOME}/app
ENV LEAN_MCP_DISABLED_TOOLS="lean_build"
ENV FORWARDED_ALLOW_IPS="*"

# --- DIAGNOSTIC ENVIRONMENT VARIABLES ---
# 1. Force Python to print logs instantly to the console instead of buffering them
ENV PYTHONUNBUFFERED=1
# 2. Force Uvicorn to print the raw ASGI scope (including RAW HTTP HEADERS)
ENV UVICORN_LOG_LEVEL="trace"
# 3. Set the Lean server itself to debug mode
ENV LEAN_LOG_LEVEL="DEBUG"

# 10. Create the Diagnostic Boot Wrapper
# This script dumps the exact state of the Hugging Face container right before boot
RUN echo '#!/bin/bash\n\
echo "=========================================="\n\
echo "      STARTING DIAGNOSTIC BOOTUP          "\n\
echo "=========================================="\n\
echo "[DEBUG] Current User: $(whoami)"\n\
echo "[DEBUG] Current Directory: $(pwd)"\n\
echo "[DEBUG] --- RAW HUGGING FACE ENVIRONMENT ---"\n\
env | sort\n\
echo "=========================================="\n\
echo "[DEBUG] Booting python server..."\n\
exec /home/user/lean-lsp-mcp/.venv/bin/lean-lsp-mcp --transport streamable-http --port 7860 --host 0.0.0.0\n\
' > /home/user/app/debug_start.sh

RUN chmod +x /home/user/app/debug_start.sh

# 11. Boot the glass box!
CMD ["/home/user/app/debug_start.sh"]