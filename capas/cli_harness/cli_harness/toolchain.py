"""The one line every coding-CLI runtime's task prompt gets, telling the
model what the shared image (Dockerfile.base) has preinstalled beyond the
CLI's own built-in shell tool. One shared constant rather than three copies:
all three runtime.py files build FROM the same image (Dockerfile.base), and
three independent copies would drift the moment one stopped matching what is
actually installed there."""

from __future__ import annotations

TOOLCHAIN_NOTE = (
    "This container also has Python 3.12, a headless Chromium via Playwright, "
    "Pillow, pandas, and a PDF library preinstalled -- use your shell tool to "
    "write and run a script for anything that needs them: rendering a "
    "JavaScript-heavy page, a screenshot, a generated PDF, an image resize or "
    "conversion, a data-file conversion."
)
