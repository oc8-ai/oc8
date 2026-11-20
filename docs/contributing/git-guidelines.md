# Git Guidelines

## Commit messages

This repo follows [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): summary`, imperative mood, present tense. Real examples from
the history:

```
fix(plugins): ignore dotfiles in setup/guardrails, fix 2 stale comments
refactor(microsoft365): retrofit to the new connector/+mcp_bridge/+tests/ layout
docs(plugins): rewrite plugins/README.md for the new folder/manifest layout
test(plugins): widen the sys.modules eviction guard to cover backend/tests/ too
feat(plugins): auto-install plugin_depends recursively, never auto-enable
```

Common types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`. The scope
is usually a package or capa name (`capas`, `microsoft365`,
`frontend`) — pick whatever names the part of the codebase the commit
actually touches.

The summary line explains **why**, not just what changed — "fix(plugins):
ignore dotfiles in setup/guardrails" tells a reader what broke without
them opening the diff; "fix(plugins): update discovery.py" doesn't. For a
commit whose reasoning isn't obvious from the summary alone, use the
commit body to explain the reasoning, not to restate the diff.

## Atomic commits

One logical change per commit. A commit that mixes an unrelated
reformatting pass with a real fix makes the fix harder to review and
`git bisect` less useful later. If you're tempted to write "and also" in
a commit message, it's probably two commits.

## Before opening a pull request

```bash
cd backend
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy
uv run pytest
```

A PR that doesn't pass this locally won't pass CI either — running it
first saves a review round-trip.

## Never rewrite published history on a shared branch

Don't force-push, rebase, or amend a commit that's already been pushed to
a branch other people build on. If a commit needs fixing after the fact,
add a new commit that fixes it, rather than rewriting the one that's
wrong.

## Releases

Work lands on `dev`. Every change set bumps the version in
`backend/pyproject.toml` and `frontend/package.json` (and their lock files).
To release, open a pull request from `dev` into `main` titled
`Release X.Y.Z: merge dev into main` and merge it with a merge commit.

Its description becomes the release text, so write it for users. Start
with highlights, then upgrade notes (migrations, new settings, changed
defaults). On merge, the `Release` workflow tags `vX.Y.Z` and creates the
GitHub release from that description. It adds the list of merged pull
requests, then starts the agent image build for the tag. If `main`
already has a tag for its version, nothing is released.

## Next

- [Coding Guidelines](coding-guidelines.md) for lint/type/test conventions.
- [Core Architecture](architecture.md) for where a change belongs before
  you write it.
