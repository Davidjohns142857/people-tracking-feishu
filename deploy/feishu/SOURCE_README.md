# People Tracking Feishu — bundled source

This directory is the source snapshot embedded in a verified portable release. The executable runtime is in the
release-level `runtime/` directory; installation starts with `verify_release.py` and `install_bundle.py`, not with an
editable global package install.

The snapshot contains no people roster, observation database, report, credential, Cookie, or runtime configuration.
See the release-level `AGENT_START_HERE.md`, `INSTALL_PROMPT.md`, `CHANGELOG.md`, and `release-manifest.json`.
