# syntax=docker/dockerfile:1
# Build context: frontend workspace (`./frontend` from the repository root).
FROM docker.io/oven/bun:1 AS build
WORKDIR /app
COPY package.json bun.lock bunfig.toml ./
RUN bun install --frozen-lockfile
COPY . .
# Relative API base -> the same image runs on any server, no rebuild, no CORS.
ARG VITE_API_URL=/api/v1
# node-server preset: the repo default is cloudflare-module, which a container
# cannot run. Both env names are set because the effective one differs across
# TanStack Start / nitro versions; the build asserts the result below.
ENV VITE_API_URL=$VITE_API_URL \
    SERVER_PRESET=node-server \
    NITRO_PRESET=node-server
RUN bun run build && \
    test -f .output/server/index.mjs && \
    grep -q '"preset": "node-server"' .output/nitro.json

FROM docker.io/library/node:22-slim AS runtime
WORKDIR /app
COPY --from=build /app/.output ./.output
ENV PORT=3000
EXPOSE 3000
CMD ["node", ".output/server/index.mjs"]
