## 1. Update Dependency

- [x] 1.1 Change the `cloakbrowser` pin in `pyproject.toml` from `0.5.5` to `0.5.6`.
- [x] 1.2 Regenerate `uv.lock` and confirm it resolves CloakBrowser 0.5.6.

## 2. Verify Upgrade

- [x] 2.1 Run `uv sync --locked` to verify the locked environment installs successfully.
- [x] 2.2 Run `uv run python -m unittest discover -v` and confirm the existing test suite passes.
