# People Tracking Feishu — bundled source

This directory is the source snapshot embedded in a portable release. Externally verify the sibling ZIP with its
matching `.sha256` before extraction or execution. After extraction, the executable runtime is in the release-level
`runtime/` directory; the first package-internal step is `verify_release.py`, followed by `install_bundle.py`, not an
editable global package install. A checksum from the same GitHub Release checks integrity but does not independently
authenticate the publisher; this release does not include Sigstore or GitHub Artifact Attestation.

`full-source/` is an auditable repository source snapshot, not the installed import path. It retains the parent
People Intel project's optional Cognee integration; the portable installer neither installs those model extras nor
places this directory on `PYTHONPATH`. People-tracking change review is implemented only by the host Agent protocol
in `runtime/people_tracking_feishu/` and has no separately configured model client.

The snapshot contains no people roster, observation database, report, credential, Cookie, or runtime configuration.
See the release-level `AGENT_START_HERE.md`, `INSTALL_PROMPT.md`, `CHANGELOG.md`, and `release-manifest.json`.
