# Releasing python-rako-2025

This document is the exact procedure for cutting a release. It exists so a
release never again requires hand-cranking: the workflow verifies everything
it needs and **fails loudly instead of silently publishing something wrong**.

## How it works, in one paragraph

`python_rako/__version__.py` is the single source of truth for the package
version (`__version__ = "X.Y.Z"`), read by hatchling
(`[tool.hatch.version]`) and imported by `python_rako.__init__`. The release
workflow (`.github/workflows/release.yml`) never writes to this file. When a
GitHub Release is published, the workflow checks that the release tag
matches `__version__.py` exactly (accepting `vX.Y.Z` or `X.Y.Z`), builds the
package, sanity-checks the built wheel by installing it into a throwaway
venv and importing it, and only then publishes to PyPI using **Trusted
Publishing** (OIDC — no API tokens stored anywhere). A `workflow_dispatch`
run does the same thing but publishes to **TestPyPI** instead, as a
dry run that never touches the real package.

## Maintainer release checklist

1. **Bump the version** (on a branch, or directly on `master` if you prefer
   — either way it must land on `master` before tagging):

   ```console
   $ python scripts/bump_version.py bump patch   # or: minor / major
   Bumped version to 0.5.1
   ```

   Or via `make`: `make bump-patch` / `make bump-minor` / `make bump-major`
   (these also `git add` and commit the version file for you).

2. **Update `CHANGELOG.md`**: move the relevant `Unreleased` entries under a
   new `## [X.Y.Z] - YYYY-MM-DD` heading.

3. **Commit and push**:

   ```console
   $ git add python_rako/__version__.py CHANGELOG.md
   $ git commit -m "Release 0.5.1"
   $ git push
   ```

4. **Tag and push the tag** — the tag name must match `__version__.py`
   exactly, with an optional leading `v` (`v0.5.1` and `0.5.1` are both
   accepted by the workflow's check, but pick one convention and stick to
   it; this project uses `vX.Y.Z`):

   ```console
   $ git tag v0.5.1
   $ git push origin v0.5.1
   ```

5. **Create the GitHub Release** pointing at that tag (via the GitHub UI —
   "Draft a new release" — or `gh release create v0.5.1 --generate-notes`).
   Publishing the release fires the `release: published` event and starts
   the release workflow automatically.

6. **Watch the workflow** in the Actions tab
   (<https://github.com/SimonLeigh/python-rako/actions/workflows/release.yml>):
   - `build` job: verifies the tag matches `__version__.py`, builds sdist +
     wheel, runs `twine check`, installs the wheel into a clean venv and
     imports it. If the tag/version mismatch or the smoke test fails, the
     workflow stops **before** anything is published.
   - `publish-pypi` job: publishes to the real PyPI via Trusted Publishing,
     with build attestations. Requires the `pypi` GitHub Environment
     approval if you've configured one as a manual gate (see below).

7. **Verify**: `pip install python-rako-2025==0.5.1` in a scratch venv, or
   check <https://pypi.org/project/python-rako-2025/>.

### Dry run before a real release (recommended)

Run the workflow manually from the Actions tab
("Release" -> "Run workflow", branch = `master`) or via
`gh workflow run release.yml`. This runs the `build` job and then
`publish-testpypi`, which publishes to **TestPyPI**
(<https://test.pypi.org/project/python-rako-2025/>) instead of the real
PyPI. It uses the exact same code path as a real release, so a clean dry
run is a strong signal the real release will also work. TestPyPI allows
re-publishing over an existing dry-run version freely, but it will reject a
version that's already there with a different sdist/wheel body — you may
need to bump the version again for a repeat dry run, or ignore the
duplicate-file error since it's harmless.

## One-time setup (already done unless PyPI/GitHub configuration changes)

These steps only need to be done once per project, or if the Trusted
Publisher / environment configuration is ever reset. If a release starts
failing at the "Publish to PyPI" / "Publish to TestPyPI" step with an
authentication error, re-check these.

### 1. PyPI Trusted Publisher (pypi.org)

On <https://pypi.org/manage/project/python-rako-2025/settings/publishing/>
(or, for a brand-new project name, the "pending publisher" flow at
<https://pypi.org/manage/account/publishing/>), add a Trusted Publisher
with:

| Field | Value |
|---|---|
| PyPI project name | `python-rako-2025` |
| Owner | `SimonLeigh` |
| Repository name | `python-rako` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

### 2. TestPyPI Trusted Publisher (test.pypi.org)

Same as above but on
<https://test.pypi.org/manage/project/python-rako-2025/settings/publishing/>
(or the pending-publisher flow at
<https://test.pypi.org/manage/account/publishing/> if the project doesn't
exist on TestPyPI yet), with:

| Field | Value |
|---|---|
| PyPI project name | `python-rako-2025` |
| Owner | `SimonLeigh` |
| Repository name | `python-rako` |
| Workflow name | `release.yml` |
| Environment name | `testpypi` |

### 3. GitHub Environments

In the repo's Settings -> Environments
(<https://github.com/SimonLeigh/python-rako/settings/environments>), create
two environments:

- **`pypi`** — used by the real-release job. Consider adding a required
  reviewer here for an extra manual gate before anything hits the real
  PyPI (optional but recommended).
- **`testpypi`** — used by the dry-run job. No protection rules needed.

No secrets need to be added to either environment — Trusted Publishing uses
GitHub's OIDC token (`id-token: write`, requested automatically by
`pypa/gh-action-pypi-publish`), not a stored API token. Any old
`PYPI_TOKEN` / `TEST_PYPI_TOKEN` repository secrets can be deleted once
this is confirmed working.

## Why not just fix the old workflow?

The previous workflow wrote the raw git tag into `__version__.py`
(`echo "$RELEASE_TAG" > python_rako/__version__.py`), producing a file
containing e.g. just `0.4.1` — not valid Python, and unreadable by
hatchling's `[tool.hatch.version]`. It also used long-lived API tokens
(`PYPI_TOKEN` / `TEST_PYPI_TOKEN` secrets) instead of Trusted Publishing,
and had no verification step before publishing, so a bad build could reach
PyPI with no warning. This workflow fixes all three: the version file is
never rewritten by CI, publishing uses short-lived OIDC credentials instead
of stored tokens, and nothing is published until the tag/version match has
been checked and the built wheel has been smoke-tested.
