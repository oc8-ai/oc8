# Creates a nanoclaw session folder's two SQLite DBs using NANOCLAW'S OWN schema
# code, so oc8 never forks it. Built from the pinned checkout (see PINNED.md).
#
# Built on oven/bun (not node) so the TypeScript schema module
# (src/db/schema.ts) can be imported directly, with no compile step -- bun's
# module loader strips types at import time and ships a built-in SQLite
# driver (bun:sqlite), so nothing here needs `npm install` or `tsc`.
#
# The entrypoint is `bun`, not `node` -- this image genuinely runs on bun, so
# session.py's SandboxSpec.command names `bun` directly rather than a `node`
# shim kept around just to match a stale literal.
FROM docker.io/oven/bun:1
ARG NANOCLAW_REF
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/nanocoai/nanoclaw.git /src && cd /src && git checkout "$NANOCLAW_REF"
WORKDIR /src
COPY provision.js /app/provision.js
