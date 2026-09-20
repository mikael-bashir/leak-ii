FROM ubuntu:22.04

RUN useradd -m -u 1000 user
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
RUN apt-get update && apt-get install -y \
    curl git build-essential python3 python3-pip python3-venv cmake tzdata zstd && \
    rm -rf /var/lib/apt/lists/*

USER user
ENV HOME=/home/user
ENV PATH="${HOME}/.local/bin:${HOME}/.elan/bin:${PATH}"
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf | sh -s -- -y
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
WORKDIR ${HOME}

# The environment every Pantograph worker loads is the Tengoku tree — one
# self-contained Lean 4 library seeded from Mathlib and other open libraries,
# plus every verified addition — pinned to its newest published build cache so
# nothing compiles. Changing TENGOKU_REFRESH re-clones on a rebuild instead of
# reusing a stale cached clone layer.
ARG TENGOKU_REFRESH=0
# Which tree this service follows, and whether it follows the per-merge top-ups (1) or only the
# nightly cache (0). Both are Space VARIABLES: Hugging Face passes them in as build args and as
# runtime env, so moving the service to another tree is a variable change plus a factory rebuild.
ARG TENGOKU_REPO=competemath/tengoku
ARG TENGOKU_TOPUPS=1
ENV TENGOKU_REPO=${TENGOKU_REPO}
ENV TENGOKU_TOPUPS=${TENGOKU_TOPUPS}
RUN echo "refresh ${TENGOKU_REFRESH}" >/dev/null && git clone --filter=blob:none https://github.com/${TENGOKU_REPO}.git tengoku
RUN --mount=type=secret,id=GH_TOKEN,env=GH_TOKEN,required=false cd tengoku && scripts/pin.sh \
 && rm -rf .lake/build/ir
ENV LEAN_PROJECT_PATH=${HOME}/tengoku
ENV TENGOKU_IMPORTS="Tengoku.All"

# Python app + PyPantograph, whose repl is built on the tree's toolchain
WORKDIR ${HOME}/app
COPY --chown=user . ${HOME}/app
RUN uv python install 3.11
RUN uv venv --python 3.11 ${HOME}/app/.venv
ENV PATH="${HOME}/app/.venv/bin:${PATH}"
RUN uv pip install fastmcp "mcp<2" asyncio nest_asyncio

WORKDIR ${HOME}/PyPantograph
RUN git clone --recurse-submodules https://github.com/competemath/PyPantograph.git .
RUN cp ${HOME}/tengoku/lean-toolchain ./src/lean-toolchain
RUN python3 build-pantograph.py
RUN uv pip install .

WORKDIR ${HOME}/app
EXPOSE 7860
CMD ["python3", "server.py"]
