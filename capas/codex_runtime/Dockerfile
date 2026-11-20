# The shared base (claude_code_runtime/Dockerfile.base), passed in so a
# build can point at the base it just built, e.g.
# `--build-arg BASE_IMAGE=registry.example/agent-runtime-base:<tag>`.
ARG BASE_IMAGE=oc8-agent-runtime-base:latest
FROM ${BASE_IMAGE}
RUN npm install -g @openai/codex
# LAST, after the global install -- see claude_code_runtime/Dockerfile.base
# for why this cannot live in the shared base. `node` is the image's own
# uid-1000 user.
USER node
