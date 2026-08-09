## Why

CloakBrowser 0.5.6 is available while the project is pinned to 0.5.5. Updating the pinned dependency keeps the downloader on the current patch release and removes the runtime upgrade notice.

## What Changes

- Update the exact CloakBrowser dependency pin from `0.5.5` to `0.5.6`.
- Regenerate `uv.lock` so installs and container builds resolve CloakBrowser 0.5.6 reproducibly.
- Verify the existing downloader tests and locked dependency installation still succeed.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

None. This patch-level dependency maintenance does not change the project's specified behavior, so this change opts out of spec deltas.

## Impact

- Dependency metadata: `pyproject.toml` and `uv.lock`.
- Container builds will install CloakBrowser 0.5.6 through the existing locked dependency workflow.
- No application API, configuration, or documented behavior changes are expected.
