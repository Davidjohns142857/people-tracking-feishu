from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from deploy.feishu import verify_release
from people_tracking_feishu.cli import _attach_bridge_write_contract
from people_tracking_feishu.config import ConfigError, normalize_answers, validate_config
from people_tracking_feishu.master import PEOPLE_FIELD_SPECS, sync_visible_master
from people_tracking_feishu.state import PortableState
from scripts import build_feishu_release, validate_portable_skill


ROOT = Path(__file__).parents[1]


def _valid_config() -> dict:
    return normalize_answers(
        {
            "sources": [{"kind": "local_file", "path": "/controlled/roster.json"}],
            "field_mapping": {"name": "姓名"},
            "master_database": {"mode": "local_or_api"},
        }
    )


@pytest.mark.parametrize(
    ("human_name", "machine_name"),
    [
        ("Shared Column", "shared column"),
        ("ＮＡＭＥ", "name"),
        (["Display Name", "Protected Alias"], " protected   alias "),
    ],
)
def test_config_rejects_human_machine_field_alias_collisions(
    human_name: object,
    machine_name: str,
) -> None:
    config = copy.deepcopy(_valid_config())
    config["field_mapping"]["name"] = human_name
    config["field_mapping"]["managed_by"] = machine_name

    with pytest.raises(ConfigError, match="field_mapping ownership collision"):
        validate_config(config, require_complete=True)


@pytest.mark.parametrize(
    "mapping",
    [
        {"person_key": "状态", "sync_status": "状态"},
        {"Sources": {"source_key": "来源键", "person_key": "来源键"}},
    ],
)
def test_config_rejects_same_owner_semantic_field_collisions(
    mapping: dict[str, object],
) -> None:
    config = copy.deepcopy(_valid_config())
    config["field_mapping"].update(mapping)

    with pytest.raises(ConfigError, match="semantic collision"):
        validate_config(config, require_complete=True)


class _NoWriteLark:
    def __init__(self) -> None:
        self.writes: list[list[str]] = []

    def list_base_records(self, **_: object) -> list[dict]:
        return []

    def list_base_fields(self, **_: object) -> list[dict]:
        return [
            {"field_name": "Name", "type": 1},
            {"field_name": "Person Key", "type": 1},
        ]

    def write(self, args: list[str], *, apply: bool):
        self.writes.append(args)
        raise AssertionError("an ownership collision must fail before a schema or row write")


@pytest.mark.parametrize("machine_name", ["NAME", "ＮＡＭＥ", "  Name  "])
def test_live_schema_resolution_rejects_physical_collision_before_any_write(
    tmp_path: Path,
    machine_name: str,
) -> None:
    state = PortableState(tmp_path / "state.sqlite3")
    lark = _NoWriteLark()
    try:
        with pytest.raises(ValueError, match="People field ownership collision"):
            sync_visible_master(
                state,
                lark,  # type: ignore[arg-type]
                base_token="base",
                people_table_id="people",
                apply=True,
                field_mapping={
                    "People": {
                        # The configured human alias does not itself collide;
                        # only resolution against the live `Name` column does.
                        "name": "姓名",
                        "person_key": "Person Key",
                        "managed_by": machine_name,
                    }
                },
            )
        assert lark.writes == []
    finally:
        state.close()


def test_live_schema_rejects_machine_machine_collision_before_write(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "machine-collision.sqlite3")
    lark = _NoWriteLark()
    try:
        with pytest.raises(ValueError, match="People field semantic collision"):
            sync_visible_master(
                state,
                lark,  # type: ignore[arg-type]
                base_token="base",
                people_table_id="people",
                apply=True,
                field_mapping={
                    "People": {
                        "name": "Name",
                        "person_key": "状态",
                        "sync_status": "状态",
                    }
                },
            )
        assert lark.writes == []
    finally:
        state.close()


def test_agent_bridge_rejects_normalized_cross_owner_candidates_before_emission() -> None:
    item = {"entity_key": "person:test", "fields": {"Name": "Test Person"}}

    with pytest.raises(ValueError, match="ownership collision before write contract"):
        _attach_bridge_write_contract(
            item,
            table="People",
            specs=PEOPLE_FIELD_SPECS,
            target_record_id="rec_test",
            values={"managed_by": "people-tracking-feishu"},
            live_fields={"Name": "Test Person"},
            field_mapping={"name": "Name", "managed_by": "ＮＡＭＥ"},
        )

    assert "write_contract" not in item


def test_agent_bridge_rejects_machine_machine_candidate_collision() -> None:
    item = {"entity_key": "person:test"}

    with pytest.raises(ValueError, match="semantic collision before write contract"):
        _attach_bridge_write_contract(
            item,
            table="People",
            specs=PEOPLE_FIELD_SPECS,
            target_record_id=None,
            values={
                "name": "Test Person",
                "person_key": "person_test",
                "sync_status": "有效",
            },
            live_fields=None,
            field_mapping={"person_key": "状态", "sync_status": "状态"},
        )

    assert "write_contract" not in item


def _make_oversized(path: Path, maximum: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.seek(maximum)
        handle.write(b"\0")


def test_builder_rejects_file_too_large_for_complete_content_scan(
    tmp_path: Path,
) -> None:
    target = tmp_path / "runtime" / "oversized.py"
    _make_oversized(target, build_feishu_release.MAX_AUDITED_FILE_BYTES)

    with pytest.raises(RuntimeError, match=r"oversized:runtime/oversized\.py"):
        build_feishu_release.audit(tmp_path)


def test_verifier_rejects_oversized_file_even_when_manifest_hash_matches(
    tmp_path: Path,
) -> None:
    version = "0.9.0-portable.1"
    version_path = tmp_path / "VERSION"
    version_path.write_text(version + "\n", encoding="utf-8")
    target = tmp_path / "runtime" / "oversized.bin"
    _make_oversized(target, verify_release.MAX_AUDITED_FILE_BYTES)
    files = {
        "VERSION": verify_release.sha256(version_path),
        "runtime/oversized.bin": verify_release.sha256(target),
    }
    (tmp_path / "release-manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": "people-tracking-feishu-release-v1",
                "release": f"people-tracking-feishu-{version}",
                "version": version,
                "release_audit_policy": {
                    "content_scan_is_mandatory": True,
                    "maximum_regular_file_bytes": verify_release.MAX_AUDITED_FILE_BYTES,
                    "oversized_file_action": "reject",
                },
                "files": files,
            }
        ),
        encoding="utf-8",
    )

    report = verify_release.verify(tmp_path)

    assert report["ok"] is False
    assert report["manifest_files_match"] is True
    assert report["scan"]["oversized"] == [
        "runtime/oversized.bin:8388609>8388608"
    ]


def test_verifier_rejects_unmanifested_oversized_bytecode_cache(
    tmp_path: Path,
) -> None:
    version = "0.9.0-portable.1"
    version_path = tmp_path / "VERSION"
    version_path.write_text(version + "\n", encoding="utf-8")
    target = tmp_path / "runtime/__pycache__/evil.cpython-312.pyc"
    _make_oversized(target, verify_release.MAX_AUDITED_FILE_BYTES)
    (tmp_path / "release-manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": "people-tracking-feishu-release-v1",
                "release": f"people-tracking-feishu-{version}",
                "version": version,
                "release_audit_policy": {
                    "content_scan_is_mandatory": True,
                    "maximum_regular_file_bytes": verify_release.MAX_AUDITED_FILE_BYTES,
                    "oversized_file_action": "reject",
                },
                "files": {"VERSION": verify_release.sha256(version_path)},
            }
        ),
        encoding="utf-8",
    )

    report = verify_release.verify(tmp_path)

    relative = "runtime/__pycache__/evil.cpython-312.pyc"
    assert report["ok"] is False
    assert relative in report["extra"]
    assert relative in report["scan"]["forbidden"]
    assert report["scan"]["oversized"] == [
        f"{relative}:8388609>8388608"
    ]


def test_source_validator_rejects_oversized_unscanned_file(tmp_path: Path) -> None:
    target = tmp_path / "runtime" / "oversized.txt"
    _make_oversized(target, validate_portable_skill.MAX_AUDITED_FILE_BYTES)

    assert validate_portable_skill.audit_files(tmp_path) == [
        "oversized:runtime/oversized.txt:8388609>8388608"
    ]


def test_release_manifest_declares_scan_and_checksum_trust_boundaries(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "people-tracking-feishu-0.9.0-portable.1"
    payload = build_feishu_release.manifest(staging, {}, 1786579200)

    assert payload["release_audit_policy"] == {
        "content_scan_is_mandatory": True,
        "maximum_regular_file_bytes": 8 * 1024 * 1024,
        "oversized_file_action": "reject",
    }
    assert payload["artifact_trust_policy"]["checksum_required_before_extraction"] is True
    assert payload["artifact_trust_policy"]["checksum_authenticates_publisher"] is False
    assert payload["artifact_trust_policy"]["independent_attestation_included"] is False


def test_release_docs_require_external_checksum_before_extraction() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    prompt = (ROOT / "deploy/feishu/INSTALL_PROMPT.md").read_text(encoding="utf-8")
    start = (ROOT / "deploy/feishu/AGENT_START_HERE.md").read_text(encoding="utf-8")
    handoff = json.loads(
        (ROOT / "deploy/feishu/agent-handoff.json").read_text(encoding="utf-8")
    )
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    report = (ROOT / "deploy/feishu/INSTALLATION_REPORT.md").read_text(encoding="utf-8")
    ci_readme = (ROOT / "ci-templates/github-actions/README.md").read_text(
        encoding="utf-8"
    )

    assert readme.index("sha256sum -c") < readme.index('unzip "${archive}"')
    assert readme.index('unzip "${archive}"') < readme.index("python3 verify_release.py")
    assert "不能独立证明发布者身份" in readme
    assert prompt.index("sha256sum -c") < prompt.index("checksum 成功后才解压")
    assert "不能独立认证发布者" in prompt
    assert start.index("sha256sum -c") < start.index("checksum 成功后才解压")
    assert start.index("checksum 成功后才解压") < start.index("python3 verify_release.py")
    assert "不能独立认证发布者" in start
    trust = handoff["artifact_trust_policy"]
    assert trust["checksum_required_before_extraction"] is True
    assert trust["package_code_must_not_execute_before_checksum"] is True
    assert trust["linux_checksum_command"][0] == "sha256sum"
    assert trust["macos_checksum_command"][:3] == ["shasum", "-a", "256"]
    assert trust["post_checksum_order"][:2] == ["extract_archive", "verify_release"]
    assert trust["checksum_authenticates_publisher"] is False
    assert trust["independent_attestation_included"] is False
    for document in (security, report, ci_readme):
        assert "不能独立认证发布者" in document
        assert "Artifact Attestation" in document
