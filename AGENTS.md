# Agent instructions

Guidance for AI coding agents working in this repository.

- Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request —
  it covers the DCO sign-off, coding guidelines, and PR process.
- Before committing: `cd backend && python -m pytest && python -m ruff check src tests`
  and `cd frontend && npm run lint && npm run test:unit && npm run build`.
- Follow existing patterns in the module you're touching rather than
  introducing a new one — see [docs/contributing/coding-guidelines.md](docs/contributing/coding-guidelines.md).
- Read [DESIGN.md](DESIGN.md) before any visual or UI change. Fonts, colours,
  spacing, components, states and copy rules are defined there; don't
  deviate without the maintainers' agreement.
