# 1. Base Image
FROM ubuntu:22.04

# 2. CREATE THE GUEST USER
RUN useradd -m -u 1000 user

# 3. Install System Dependencies + Nginx
RUN apt-get update && apt-get install -y curl git build-essential python3 nginx && rm -rf /var/lib/apt/lists/*

# 4. Switch to the unprivileged user
USER user
ENV HOME=/home/user
ENV PATH="${HOME}/.local/bin:${HOME}/.elan/bin:${PATH}"

# 5. Install Lean & uv
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# 6. Setup Workspace
WORKDIR ${HOME}/app
COPY --chown=user . ${HOME}/app
RUN chmod +x ${HOME}/app/start.sh

# 7. Clone & Pre-build your patched fork
RUN git clone https://github.com/mikael-bashir/lean-lsp-mcp.git ${HOME}/lean-lsp-mcp
WORKDIR ${HOME}/lean-lsp-mcp
RUN uv sync

# 8. Build the Lean project natively
WORKDIR ${HOME}/app
RUN lake build

# 9. Environment Variables
EXPOSE 7860
ENV LEAN_PROJECT_PATH=${HOME}/app
ENV LEAN_MCP_DISABLED_TOOLS="lean_build"
ENV FORWARDED_ALLOW_IPS="*"

# 10. Boot the hardcoded Nginx proxy!
CMD ["./start.sh"]