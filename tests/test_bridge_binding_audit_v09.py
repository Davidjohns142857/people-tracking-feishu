from __future__ import annotations

from pathlib import Path

import pytest

from people_tracking_feishu.cli import (
    _authoritative_bridge_binding,
    _bridge_safe_master_records,
)
from people_tracking_feishu.state import PortableState


def _config(url: str, **master_values: object) -> dict:
    return {
        "field_mapping": {"person_key": "Person Key", "name": "Name"},
        "master_database": {
            "mode": "existing_base",
            "url": url,
            "authoritative_roster": True,
            **master_values,
        },
    }


def _batch(**values: object) -> dict:
    return {
        "source_complete": True,
        "base_token": "bas_expected",
        "table_id": "tbl_expected",
        "schema_fields": ["Person Key", "Name"],
        **values,
    }


def test_direct_url_only_authoritative_bridge_is_bound_to_opaque_ids() -> None:
    config = _config(
        "https://synthetic.feishu.cn/base/bas_expected?table=tbl_expected&view=viw_1"
    )
    master = config["master_database"]
    assert _authoritative_bridge_binding(
        _batch(), master=master, config=config
    ) == ("bas_expected", "tbl_expected")

    with pytest.raises(ValueError, match="base_token.*configured URL"):
        _authoritative_bridge_binding(
            _batch(base_token="bas_wrong"), master=master, config=config
        )
    with pytest.raises(ValueError, match="table_id.*configured URL"):
        _authoritative_bridge_binding(
            _batch(table_id="tbl_wrong"), master=master, config=config
        )


def test_indirect_url_requires_resolver_and_selected_table_proof() -> None:
    url = "https://synthetic.feishu.cn/wiki/wikcn_expected"
    config = _config(url, people_table_name="People")
    master = config["master_database"]
    with pytest.raises(ValueError, match="locator_proof"):
        _authoritative_bridge_binding(_batch(), master=master, config=config)

    proof = {
        "requested_url": url,
        "object_type": "bitable",
        "base_token": "bas_expected",
        "selected_table": {"table_id": "tbl_expected", "name": "People"},
    }
    assert _authoritative_bridge_binding(
        _batch(locator_proof=proof), master=master, config=config
    ) == ("bas_expected", "tbl_expected")
    with pytest.raises(ValueError, match="locator_proof"):
        _authoritative_bridge_binding(
            _batch(locator_proof={**proof, "requested_url": url + "-wrong"}),
            master=master,
            config=config,
        )


def _visible_person() -> dict:
    return {
        "entity_key": "person_existing",
        "field_values": {
            "name": "Human Name",
            "person_key": "person_existing",
            "record_type": "人员",
            "sync_status": "正常",
        },
    }


def test_openclaw_master_bridge_reuses_live_stable_key_without_local_link(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "state.sqlite3")
    try:
        safe = _bridge_safe_master_records(
            state,
            {"People": [_visible_person()], "Sources": []},
            authoritative_roster=False,
            base_token="bas_expected",
            people_table_id="tbl_expected",
            live_records={
                "People": [
                    {
                        "record_id": "rec_existing",
                        "fields": {
                            "Name": "Human Name",
                            "Person Key": "person_existing",
                            "Human Notes": "preserve me",
                        },
                    }
                ]
            },
            field_mapping={"People": {"name": "Name", "person_key": "Person Key"}},
        )
    finally:
        state.close()

    item = safe["People"][0]
    assert item["target_record_id"] == "rec_existing"
    assert item["write_contract"]["operation"] == "update"
    assert item["write_contract"]["expected_before_fields"]["fields"][
        "Human Notes"
    ] == "preserve me"
    assert "name" not in item["write_contract"]["machine_patch"]


def test_openclaw_master_bridge_rejects_duplicate_live_stable_keys(
    tmp_path: Path,
) -> None:
    state = PortableState(tmp_path / "duplicate.sqlite3")
    try:
        with pytest.raises(ValueError, match="duplicate stable key"):
            _bridge_safe_master_records(
                state,
                {"People": [_visible_person()], "Sources": []},
                authoritative_roster=False,
                base_token="bas_expected",
                people_table_id="tbl_expected",
                live_records={
                    "People": [
                        {
                            "record_id": "rec_one",
                            "fields": {"Person Key": "person_existing"},
                        },
                        {
                            "record_id": "rec_two",
                            "fields": {"Person Key": "person_existing"},
                        },
                    ]
                },
                field_mapping={"People": {"person_key": "Person Key"}},
            )
    finally:
        state.close()
