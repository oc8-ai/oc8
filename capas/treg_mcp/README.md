# Treg

Connects a tenant's Treg account over MCP (see `plugin.toml`).

## Record links

Treg is a catalogue of third-party API endpoints and holds no records of its
own, so oc8 declares no `record_url` for it and shows no record links for
Treg tool results. The systems behind those endpoints own their records; link
them through that system's own capa instead.
