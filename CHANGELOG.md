# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Release pipeline: the release workflow no longer overwrites
  `python_rako/__version__.py` with the raw git tag (which produced an
  invalid Python file). It now verifies the release tag matches
  `__version__.py` and fails loudly on mismatch instead of publishing a
  broken build.
- Release workflow now publishes to PyPI using **Trusted Publishing**
  (OIDC) instead of long-lived API tokens, with build attestations, a
  pre-publish smoke test (build the wheel, install it into a clean venv,
  import it), and a `workflow_dispatch`-triggered TestPyPI dry run.
- CI now also builds the sdist/wheel and runs `twine check` on every pull
  request, so packaging breakage is caught before release day, not during
  it.

<!--
When cutting a release, move the entries above into a new section here,
e.g.:

## [0.5.0] - 2026-09-01

### Fixed

- ...
-->

[Unreleased]: https://github.com/SimonLeigh/python-rako/commits/master

<!--
Note for the first tagged release under this process: no git tags exist in
this repository yet (0.4.1 was released without ever tagging the commit).
Once v0.5.0 (or whichever version comes first) is tagged, switch the link
above to a compare link, e.g.:
  [Unreleased]: https://github.com/SimonLeigh/python-rako/compare/v0.5.0...HEAD
  [0.5.0]: https://github.com/SimonLeigh/python-rako/releases/tag/v0.5.0
-->
