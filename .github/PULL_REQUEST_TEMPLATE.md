## Summary
Describe the change and reference the related Milestone/Feature ID (e.g., M1-M6, FA-01..FA-25).

## Checklist
- [ ] Ran `uv sync --extra dev --link-mode=copy` (when dependencies changed)
- [ ] Ran `$env:QT_QPA_PLATFORM='offscreen'; uv run --no-sync pytest -v` and all tests pass
- [ ] Zero Unicode emojis in source code, UI strings, scripts, workflows, or tests
- [ ] All SVG icons are 100% polygonal (`M/L/Z`, `<polygon>`, `<polyline>`, `<rect>`) with no `<circle>`, `<ellipse>`, or curve commands
- [ ] `Space Grotesk` weights limited to `300`, `400`, `500`, `700` (never `600`) and global `border-radius: 0px` preserved (`6px` only on `QWidget#TopBar`)
- [ ] Local file inputs validated through `core/path_guard.py`; no silent outbound network calls
- [ ] Updated relevant `docs/*.md` and `docs/HANDOFF_STATUS.md` if architecture or contracts changed
