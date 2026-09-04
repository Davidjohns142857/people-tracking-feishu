from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import platform
import secrets
import shutil
import stat
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from people_intel.light_cli import _public_urlopen

from . import __version__
from .config import (
    QUESTIONNAIRE,
    ConfigError,
    RuntimePaths,
    authoritative_roster_source_ref,
    load_config,
    mark_state,
    normalize_answers,
    secure_write_json,
    utc_now,
    validate_config,
)
from .ingest import (
    canonical_records,
    reconcile_incremental_records,
    records_from_file,
    records_from_payload,
)
from .lark import LarkCli, LarkCliError, PINNED_LARK_CLI, first_value
from .master import (
    PEOPLE_FIELD_SPECS,
    SOURCE_FIELD_SPECS,
    create_master_base,
    master_schema,
    sync_visible_master,
    visible_records,
)
from .reporting import markdown_http_url
from .sandbox import algorithm_e2e, feishu_e2e
from .scheduler import openclaw_cron_plan
from .state import PortableState, stable_hash
from .tracking import (
    apply_agent_review_results,
    deliver_digest,
    deliver_developer_digest,
    export_agent_review_requests,
    scan_due,
)
ENABLE_PHRASE = "确认启用"
SANDBOX_PHRASE = "确认运行私有沙箱E2E"
_MASTER_LIVE_PRE_READ_MAX_AGE = timedelta(hours=1)


def _nonnegative_finite_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return parsed


def _json_file(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ValueError("input must be a regular non-symlink file")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("input exceeds the 20 MiB safety limit")
    return json.loads(path.read_text(encoding="utf-8"))


def _bridge_refs_hash(refs: list[str]) -> str:
    encoded = json.dumps(sorted(refs), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bridge_request_hash(*, purpose: str, content: Any) -> str:
    encoded = json.dumps(
        {"purpose": purpose, "content": content},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bridge_target_record_ids(content: Any) -> dict[str, str]:
    if not isinstance(content, dict):
        return {}
    records = content.get("records")
    if not isinstance(records, dict):
        return {}
    targets: dict[str, str] = {}
    for table, items in records.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            entity_key = str(item.get("entity_key") or "").strip()
            record_id = str(item.get("target_record_id") or "").strip()
            if entity_key and record_id:
                targets[f"{table}:{entity_key}"] = record_id
    return targets


def _bridge_contract_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bridge_write_contracts(content: Any) -> dict[str, dict[str, Any]]:
    """Extract the immutable, locally generated write plan from a bridge file."""

    if not isinstance(content, dict) or not isinstance(content.get("records"), dict):
        return {}
    contracts: dict[str, dict[str, Any]] = {}
    for table, items in content["records"].items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            entity_key = str(item.get("entity_key") or "").strip()
            contract = item.get("write_contract")
            if not entity_key or not isinstance(contract, dict):
                continue
            contracts[f"{table}:{entity_key}"] = json.loads(
                json.dumps(contract, ensure_ascii=False, allow_nan=False)
            )
    return contracts


def _new_bridge_request(
    paths: RuntimePaths,
    *,
    purpose: str,
    expected_refs: list[str],
    request_content: Any,
    state: PortableState | None = None,
) -> dict[str, Any]:
    refs = sorted(str(ref) for ref in expected_refs)
    if not refs or len(refs) != len(set(refs)):
        raise ValueError("bridge request must contain unique expected refs")
    request_hash = _bridge_request_hash(purpose=purpose, content=request_content)
    owned = state is None
    active = state or PortableState(paths.database)
    try:
        existing = active.get_meta(f"bridge_request:{purpose}")
        if (
            isinstance(existing, dict)
            and existing.get("status") == "pending"
            and existing.get("expected_refs") == refs
            and existing.get("expected_refs_hash") == _bridge_refs_hash(refs)
            and existing.get("request_hash") == request_hash
        ):
            return existing
        request = {
            "schema_version": "people-tracking-bridge-v1",
            "purpose": purpose,
            "bridge_nonce": secrets.token_hex(16),
            "expected_refs": refs,
            "expected_refs_hash": _bridge_refs_hash(refs),
            "request_hash": request_hash,
            "expected_record_ids": _bridge_target_record_ids(request_content),
            "expected_writes": _bridge_write_contracts(request_content),
            "created_at": utc_now(),
            "status": "pending",
        }
        active.set_meta(f"bridge_request:{purpose}", request)
        return request
    finally:
        if owned:
            active.close()


def _bridge_public_fields(request: dict[str, Any]) -> dict[str, Any]:
    return {
        key: request[key]
        for key in (
            "schema_version",
            "purpose",
            "bridge_nonce",
            "expected_refs",
            "expected_refs_hash",
            "request_hash",
        )
    }


def _validate_bridge_result(
    paths: RuntimePaths,
    payload: Any,
    *,
    purpose: str,
    completed_refs: list[str],
    state: PortableState | None = None,
    consume: bool = True,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("all_ok") is not True:
        raise ValueError("bridge result must explicitly contain all_ok=true")
    owned = state is None
    active = state or PortableState(paths.database)
    try:
        request = active.get_meta(f"bridge_request:{purpose}")
        if not isinstance(request, dict) or request.get("status") != "pending":
            raise ValueError(f"no pending {purpose} bridge request exists")
        expected = list(request.get("expected_refs") or [])
        echoed = payload.get("expected_refs")
        if payload.get("bridge_nonce") != request.get("bridge_nonce"):
            raise ValueError("bridge result nonce does not match the pending request")
        if echoed != expected:
            raise ValueError("bridge result expected_refs do not match the pending request")
        if payload.get("expected_refs_hash") != request.get("expected_refs_hash"):
            raise ValueError("bridge result expected_refs_hash does not match the pending request")
        if payload.get("request_hash") != request.get("request_hash"):
            raise ValueError("bridge result request_hash does not match the pending request")
        actual = [str(ref) for ref in completed_refs]
        if len(actual) != len(set(actual)):
            raise ValueError("bridge result contains duplicate refs")
        missing = sorted(set(expected) - set(actual))
        unknown = sorted(set(actual) - set(expected))
        if missing or unknown:
            raise ValueError(
                "bridge result coverage mismatch: "
                f"missing={missing or []}, unknown={unknown or []}"
            )
        consumed = {
            **request,
            "status": "consumed",
            "consumed_at": utc_now(),
        }
        if consume:
            active.set_meta(f"bridge_request:{purpose}", consumed)
        return consumed
    finally:
        if owned:
            active.close()


def _field_name_set(values: Any) -> set[str]:
    if not isinstance(values, list):
        return set()
    names: set[str] = set()
    for value in values:
        if isinstance(value, str):
            name = value.strip()
        elif isinstance(value, dict):
            name = str(value.get("field_name") or value.get("name") or "").strip()
        else:
            name = ""
        if not name or name in names:
            return set()
        names.add(name)
    return names


def _validated_master_bridge_contract(
    paths: RuntimePaths,
    payload: Any,
    *,
    config: dict[str, Any],
    state: PortableState,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate full schema and entity coverage before consuming a write bridge."""

    if not isinstance(payload, dict):
        raise ValueError("master bridge result must be an object")
    completed_refs = payload.get("completed_refs")
    if not isinstance(completed_refs, list):
        raise ValueError("master bridge results must contain completed_refs[]")
    consumed = _validate_bridge_result(
        paths,
        payload,
        purpose="master_sync",
        completed_refs=[str(ref) for ref in completed_refs],
        state=state,
        consume=False,
    )
    if payload.get("schema_version") != consumed.get("schema_version"):
        raise ValueError("master bridge schema_version does not match the request")
    if payload.get("purpose") != "master_sync":
        raise ValueError("master bridge purpose must be master_sync")
    expected = list(consumed.get("expected_refs") or [])
    expected_tables = {
        ref.removeprefix("table:") for ref in expected if ref.startswith("table:")
    }
    if not expected_tables or "People" not in expected_tables:
        raise ValueError("master bridge request has no People table contract")

    schema_fields = payload.get("schema_fields")
    if not isinstance(schema_fields, dict) or set(schema_fields) != expected_tables:
        raise ValueError(
            "master bridge schema_fields must exactly cover requested tables"
        )
    names_by_table = {
        table: _field_name_set(schema_fields.get(table)) for table in expected_tables
    }
    if any(not names for names in names_by_table.values()):
        raise ValueError("master bridge schema_fields contain an empty or duplicate field list")
    configured_people_key = str(
        (config.get("field_mapping") or {}).get("person_key") or ""
    ).strip()
    if not names_by_table["People"].intersection(
        {configured_people_key, "Person Key", "人员编号", "人员 Key"} - {""}
    ):
        raise ValueError("master bridge People schema has no stable person key field")
    if "Sources" in expected_tables and not names_by_table["Sources"].intersection(
        {"Source Key", "来源编号", "来源 Key"}
    ):
        raise ValueError("master bridge Sources schema has no stable source key field")

    table_ids = payload.get("table_ids")
    if (
        not isinstance(table_ids, dict)
        or set(table_ids) != expected_tables
        or any(
        not str(table_ids.get(table) or "").strip() for table in expected_tables
        )
    ):
        raise ValueError("master bridge table_ids must exactly cover requested tables")
    base_token = str(payload.get("base_token") or "").strip()
    if not base_token:
        raise ValueError("master bridge results require base_token")
    master = config.get("master_database") or {}
    configured_base = str(master.get("base_token") or "").strip()
    configured_people_table = str(master.get("people_table_id") or "").strip()
    configured_sources_table = str(master.get("sources_table_id") or "").strip()
    if configured_base and configured_base != base_token:
        raise ValueError("master bridge base_token does not match configured Base")
    if configured_people_table and configured_people_table != str(table_ids["People"]):
        raise ValueError("master bridge People table does not match configured table")
    if (
        "Sources" in expected_tables
        and configured_sources_table
        and configured_sources_table != str(table_ids["Sources"])
    ):
        raise ValueError("master bridge Sources table does not match configured table")
    result_url = str(payload.get("base_url") or "").strip()
    configured_url = str(master.get("url") or "").strip()
    if not result_url or (
        configured_url and _base_locator(result_url) != _base_locator(configured_url)
    ):
        raise ValueError("master bridge base_url does not match configured Base")

    outcomes = payload.get("outcomes")
    if not isinstance(outcomes, list) or any(not isinstance(item, dict) for item in outcomes):
        raise ValueError("master bridge results must contain outcomes[]")
    expected_entities = {ref for ref in expected if not ref.startswith("table:")}
    if payload.get("write_protocol") != "machine-fields-verified-v1":
        raise ValueError("master bridge result has no verified machine-field protocol")
    if payload.get("preflight_all_ok") is not True:
        raise ValueError("master bridge must preflight every row before any write")
    expected_record_ids = {
        str(ref): str(record_id)
        for ref, record_id in (consumed.get("expected_record_ids") or {}).items()
    }
    expected_writes = consumed.get("expected_writes")
    if not isinstance(expected_writes, dict) or set(expected_writes) != expected_entities:
        raise ValueError(
            "pending master bridge request has no complete machine write contract"
        )
    actual_entities: set[str] = set()
    record_ids: set[tuple[str, str]] = set()
    links: list[dict[str, str]] = []
    for outcome in outcomes:
        table = str(outcome.get("table") or "").strip()
        entity_key = str(outcome.get("entity_key") or "").strip()
        record_id = str(outcome.get("record_id") or "").strip()
        if outcome.get("ok") is not True or table not in expected_tables:
            raise ValueError("every master bridge outcome must state ok=true for a requested table")
        if not entity_key or not record_id:
            raise ValueError("every master bridge outcome needs entity_key and record_id")
        ref = f"{table}:{entity_key}"
        if ref in actual_entities:
            raise ValueError(f"master bridge contains duplicate outcome: {ref}")
        if ref not in expected_entities:
            raise ValueError(f"master bridge contains an unknown outcome: {ref}")
        if (table, record_id) in record_ids:
            raise ValueError(f"master bridge reuses record_id in {table}: {record_id}")
        if expected_record_ids.get(ref) not in (None, record_id):
            raise ValueError(
                f"master bridge outcome changed the required target record_id for {ref}"
            )
        _validate_master_write_outcome(
            ref=ref,
            outcome=outcome,
            expected=expected_writes[ref],
            record_id=record_id,
        )
        actual_entities.add(ref)
        record_ids.add((table, record_id))
        links.append(
            {
                "entity_kind": "person" if table == "People" else "source",
                "entity_key": entity_key,
                "base_token": base_token,
                "table_id": str(table_ids[table]),
                "record_id": record_id,
            }
        )
    if actual_entities != expected_entities:
        raise ValueError(
            "master bridge outcome coverage mismatch: "
            f"missing={sorted(expected_entities - actual_entities)}, "
            f"unknown={sorted(actual_entities - expected_entities)}"
        )
    return consumed, links


def _validate_master_write_outcome(
    *,
    ref: str,
    outcome: dict[str, Any],
    expected: Any,
    record_id: str,
) -> None:
    if not isinstance(expected, dict):
        raise ValueError(f"master bridge write contract is invalid for {ref}")
    contract = dict(expected)
    contract_hash = str(contract.pop("contract_hash", ""))
    if not contract_hash or contract_hash != _bridge_contract_hash(contract):
        raise ValueError(f"master bridge write contract hash is invalid for {ref}")
    if contract.get("protocol") != "machine-fields-verified-v1":
        raise ValueError(f"master bridge write protocol is invalid for {ref}")
    table, entity_key = ref.split(":", 1)
    if contract.get("table") != table or contract.get("entity_key") != entity_key:
        raise ValueError(f"master bridge write contract identity changed for {ref}")
    specs = PEOPLE_FIELD_SPECS if table == "People" else SOURCE_FIELD_SPECS
    allowed_machine = {
        semantic for semantic, spec in specs.items() if spec.get("owner") == "machine"
    }
    allowed_human = {
        semantic for semantic, spec in specs.items() if spec.get("owner") == "human"
    }
    machine_allowlist = contract.get("machine_field_allowlist")
    human_allowlist = contract.get("human_field_allowlist")
    if (
        not isinstance(machine_allowlist, list)
        or len(machine_allowlist) != len(set(machine_allowlist))
        or set(machine_allowlist) != allowed_machine
        or not isinstance(human_allowlist, list)
        or len(human_allowlist) != len(set(human_allowlist))
        or set(human_allowlist) != allowed_human
    ):
        raise ValueError(f"master bridge field ownership allowlist is invalid for {ref}")
    machine_patch = contract.get("machine_patch")
    if (
        not isinstance(machine_patch, dict)
        or not set(machine_patch).issubset(allowed_machine)
        or contract.get("machine_patch_hash") != _bridge_contract_hash(machine_patch)
    ):
        raise ValueError(f"master bridge machine patch is invalid for {ref}")
    machine_candidates = contract.get("machine_field_candidates")
    human_candidates = contract.get("human_field_candidates")
    if (
        not isinstance(machine_candidates, dict)
        or set(machine_candidates) != set(machine_patch)
        or any(
            not isinstance(values, list)
            or not values
            or len(values) != len(set(values))
            for values in machine_candidates.values()
        )
        or not isinstance(human_candidates, dict)
        or set(human_candidates) != allowed_human
        or any(
            not isinstance(values, list)
            or not values
            or len(values) != len(set(values))
            for values in human_candidates.values()
        )
    ):
        raise ValueError(f"master bridge field candidates are invalid for {ref}")
    machine_candidate_ids = {
        _bridge_field_identity(field_name)
        for values in machine_candidates.values()
        for field_name in values
    }
    human_candidate_ids = {
        _bridge_field_identity(field_name)
        for values in human_candidates.values()
        for field_name in values
    }
    if machine_candidate_ids & human_candidate_ids:
        raise ValueError(f"master bridge field candidates cross ownership for {ref}")
    if outcome.get("operation") != contract.get("operation"):
        raise ValueError(f"master bridge operation changed for {ref}")
    expected_target = str(contract.get("target_record_id") or "")
    actual_target = str(outcome.get("target_record_id") or "")
    if actual_target != expected_target or (expected_target and record_id != expected_target):
        raise ValueError(
            f"master bridge outcome changed the required target record_id for {ref}"
        )
    if outcome.get("contract_hash") != contract_hash:
        raise ValueError(f"master bridge outcome contract hash changed for {ref}")
    if outcome.get("machine_patch_hash") != contract.get("machine_patch_hash"):
        raise ValueError(f"master bridge outcome machine patch hash changed for {ref}")
    if outcome.get("expected_before_hash") != contract.get("expected_before_hash"):
        raise ValueError(f"master bridge outcome before hash changed for {ref}")
    if outcome.get("preflight_ok") is not True:
        raise ValueError(f"master bridge row was not preflighted before write: {ref}")

    before_read = outcome.get("before_read")
    expected_before = contract.get("expected_before_fields")
    if (
        not isinstance(before_read, dict)
        or before_read != expected_before
        or _bridge_contract_hash(before_read) != contract.get("expected_before_hash")
    ):
        raise ValueError(f"master bridge before-read mismatch for {ref}")
    applied_patch = outcome.get("applied_patch")
    if not isinstance(applied_patch, dict) or applied_patch != machine_patch:
        raise ValueError(f"master bridge applied patch differs from machine patch for {ref}")
    applied_mapping = outcome.get("applied_field_mapping")
    applied_identities = (
        [_bridge_field_identity(value) for value in applied_mapping.values()]
        if isinstance(applied_mapping, dict)
        else []
    )
    if (
        not isinstance(applied_mapping, dict)
        or set(applied_mapping) != set(machine_patch)
        or len(set(applied_identities)) != len(applied_mapping)
        or any(
            not isinstance(field_name, str)
            or field_name not in machine_candidates[semantic]
            for semantic, field_name in applied_mapping.items()
        )
    ):
        raise ValueError(f"master bridge applied field mapping is invalid for {ref}")
    known_human_names = {
        _bridge_field_identity(field_name)
        for candidates in human_candidates.values()
        if isinstance(candidates, list)
        for field_name in candidates
    }
    if set(applied_identities) & known_human_names:
        raise ValueError(f"master bridge machine patch targets a human-owned field for {ref}")
    if outcome.get("human_patch") not in (None, {}):
        raise ValueError(f"master bridge attempted to patch human-owned fields for {ref}")
    create_human_fields = contract.get("create_human_fields")
    if not isinstance(create_human_fields, dict) or not set(create_human_fields).issubset(
        allowed_human
    ):
        raise ValueError(f"master bridge create fields are invalid for {ref}")
    if contract.get("operation") == "create":
        if outcome.get("created_human_fields") != create_human_fields:
            raise ValueError(f"master bridge create fields changed for {ref}")
        created_mapping = outcome.get("created_field_mapping")
        created_identities = (
            [_bridge_field_identity(value) for value in created_mapping.values()]
            if isinstance(created_mapping, dict)
            else []
        )
        if (
            not isinstance(created_mapping, dict)
            or set(created_mapping) != set(create_human_fields)
            or len(set(created_identities)) != len(created_mapping)
            or any(
                not isinstance(field_name, str)
                or field_name not in human_candidates[semantic]
                for semantic, field_name in created_mapping.items()
            )
        ):
            raise ValueError(f"master bridge create field mapping is invalid for {ref}")
        if set(created_identities) & set(applied_identities):
            raise ValueError(f"master bridge create mappings overlap for {ref}")
    else:
        if outcome.get("created_human_fields") not in (None, {}):
            raise ValueError(f"master bridge updated human-owned fields for {ref}")
        if outcome.get("created_field_mapping") not in (None, {}):
            raise ValueError(f"master bridge mapped human fields during an update for {ref}")
        created_mapping = {}
    post_read = outcome.get("post_read")
    if not isinstance(post_read, dict):
        raise ValueError(f"master bridge post-read is missing for {ref}")
    if str(post_read.get("record_id") or "") != record_id:
        raise ValueError(f"master bridge post-read record_id changed for {ref}")
    post_fields = post_read.get("fields")
    if not isinstance(post_fields, dict):
        raise ValueError(f"master bridge post-read fields are missing for {ref}")
    for semantic, desired in machine_patch.items():
        if post_fields.get(applied_mapping[semantic]) != desired:
            raise ValueError(
                f"master bridge post-read does not confirm the machine patch for {ref}"
            )
    if contract.get("operation") == "create":
        for semantic, desired in create_human_fields.items():
            if post_fields.get(created_mapping[semantic]) != desired:
                raise ValueError(f"master bridge create readback changed for {ref}")
        permitted_created = set(applied_mapping.values()) | set(created_mapping.values())
        if set(post_fields) - permitted_created:
            raise ValueError(f"master bridge created unexpected fields for {ref}")
    else:
        before_fields = (expected_before or {}).get("fields")
        if not isinstance(before_fields, dict):
            raise ValueError(f"master bridge expected before fields are invalid for {ref}")
        changed_fields = {
            field_name
            for field_name in set(before_fields) | set(post_fields)
            if before_fields.get(field_name) != post_fields.get(field_name)
        }
        if changed_fields - set(applied_mapping.values()):
            raise ValueError(f"master bridge changed human-owned fields for {ref}")
    if outcome.get("post_read_hash") != _bridge_contract_hash(post_read):
        raise ValueError(f"master bridge post-read hash is invalid for {ref}")
    if outcome.get("human_fields_unchanged") is not True:
        raise ValueError(f"master bridge did not attest human-field preservation for {ref}")


def _consume_master_bridge(
    state: PortableState,
    *,
    request: dict[str, Any],
    links: list[dict[str, str]],
) -> None:
    now = utc_now()
    try:
        state.db.execute("BEGIN IMMEDIATE")
        for link in links:
            state.db.execute(
                """INSERT INTO feishu_record_links(
                     entity_kind,entity_key,base_token,table_id,record_id,updated_at
                   ) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(entity_kind,entity_key,base_token,table_id) DO UPDATE SET
                     record_id=excluded.record_id,updated_at=excluded.updated_at""",
                (
                    link["entity_kind"],
                    link["entity_key"],
                    link["base_token"],
                    link["table_id"],
                    link["record_id"],
                    now,
                ),
            )
        state.db.execute(
            """INSERT INTO portable_meta(key,value_json,updated_at) VALUES(?,?,?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                                              updated_at=excluded.updated_at""",
            (
                "bridge_request:master_sync",
                json.dumps(request, ensure_ascii=False),
                now,
            ),
        )
        state.db.commit()
    except Exception:
        state.db.rollback()
        raise


def _bridge_actions(actions: list[dict[str, Any]], request: dict[str, Any]) -> list[dict[str, Any]]:
    fields = _bridge_public_fields(request)
    return [{**action, **fields} for action in actions]


def _master_expected_refs(
    records: dict[str, list[dict[str, Any]]], *, include_sources: bool = True
) -> list[str]:
    tables = ("People", "Sources") if include_sources else ("People",)
    refs = [f"table:{table}" for table in tables]
    for table in tables:
        refs.extend(f"{table}:{item['entity_key']}" for item in records.get(table, []))
    return refs


def _master_bridge_result_contract(*, include_sources: bool) -> dict[str, Any]:
    tables = ["People"] + (["Sources"] if include_sources else [])
    return {
        "write_protocol": "machine-fields-verified-v1",
        "preflight_all_ok": (
            "true only after every target row was read and every expected_before_hash "
            "matched; if one row differs, write nothing and return all_ok=false"
        ),
        "all_ok": True,
        "completed_refs": "exact bridge_request.expected_refs",
        "base_token": "actual Base token",
        "base_url": "actual HTTPS Feishu/Lark Base URL",
        "table_ids": {table: f"actual {table} table ID" for table in tables},
        "schema_fields": {
            table: "complete live field list after writes" for table in tables
        },
        "outcomes": [
            {
                "table": "People or Sources",
                "entity_key": "exact requested entity key",
                "record_id": (
                    "actual stable Feishu record_id; must equal target_record_id when supplied"
                ),
                "operation": "exact write_contract.operation",
                "target_record_id": "exact write_contract.target_record_id or null",
                "contract_hash": "exact write_contract.contract_hash",
                "machine_patch_hash": "exact write_contract.machine_patch_hash",
                "expected_before_hash": "exact write_contract.expected_before_hash",
                "preflight_ok": True,
                "before_read": "exact write_contract.expected_before_fields",
                "applied_patch": "exact write_contract.machine_patch; semantic keys only",
                "applied_field_mapping": (
                    "semantic machine key -> one exact live field named in "
                    "write_contract.machine_field_candidates"
                ),
                "human_patch": {},
                "created_human_fields": (
                    "exact write_contract.create_human_fields for create; otherwise {}"
                ),
                "created_field_mapping": (
                    "semantic human key -> permitted live field for create; otherwise {}"
                ),
                "post_read": {
                    "record_id": "same as outcome.record_id",
                    "fields": (
                        "complete live row after write; every difference from before_read.fields "
                        "must be one exact machine field in applied_field_mapping"
                    ),
                },
                "post_read_hash": "sha256 of canonical post_read JSON",
                "human_fields_unchanged": True,
                "ok": True,
            }
        ],
    }


def _authoritative_master(config: dict[str, Any]) -> dict[str, Any] | None:
    master = config.get("master_database") or {}
    if (
        isinstance(master, dict)
        and master.get("mode") == "existing_base"
        and master.get("authoritative_roster", True) is True
    ):
        return dict(master)
    return None


def _base_locator(url: Any) -> str:
    """Compare Base URLs without a view-scoped query or fragment."""

    value = str(url or "").strip()
    return value.split("?", 1)[0].split("#", 1)[0].rstrip("/").casefold()


def _direct_base_url_binding(url: Any) -> tuple[str | None, str | None]:
    """Extract an independently checkable Base/table identity from a direct URL.

    Wiki links need an explicit resolver proof from the Feishu bridge, but an
    ordinary ``/base/<token>?table=<id>`` URL already contains both opaque IDs.
    Binding bridge results to those values prevents an accidental read of a
    different Base from silently becoming the authoritative roster.
    """

    parsed = urlparse(str(url or "").strip())
    segments = [segment for segment in parsed.path.split("/") if segment]
    base_token: str | None = None
    for marker in ("base", "bitable"):
        if marker in segments:
            index = segments.index(marker)
            if index + 1 < len(segments):
                base_token = segments[index + 1]
                break
    query = dict(parse_qsl(parsed.query))
    table = str(query.get("table") or query.get("table_id") or "").strip()
    return base_token, table or None


def _configured_source_is_master(source: dict[str, Any], master: dict[str, Any]) -> bool:
    if (
        source.get("kind") != "feishu_base"
        or not _base_locator(source.get("url"))
        or _base_locator(source.get("url")) != _base_locator(master.get("url"))
    ):
        return False
    source_query = dict(parse_qsl(urlparse(str(source.get("url") or "")).query))
    master_query = dict(parse_qsl(urlparse(str(master.get("url") or "")).query))
    source_table = str(
        source.get("table_id")
        or source.get("table_name")
        or source_query.get("table")
        or source_query.get("table_id")
        or ""
    ).strip()
    master_table = str(
        master.get("people_table_id")
        or master.get("people_table_name")
        or master_query.get("table")
        or master_query.get("table_id")
        or ""
    ).strip()
    if source_table and master_table and source_table != master_table:
        # A display name ("People") and an opaque table ID ("tbl_...") may
        # designate the same table.  Only two distinct opaque IDs prove that
        # these are separate tables in the same Base.
        return not (
            source_table.casefold().startswith("tbl")
            and master_table.casefold().startswith("tbl")
        )
    return True


def _raw_base_record_id(record: dict[str, Any]) -> str:
    return str(record.get("record_id") or record.get("id") or record.get("row_id") or "").strip()


def _authoritative_record_links(
    records: list[dict[str, Any]],
    canonical: list[dict[str, Any]],
) -> dict[str, str]:
    """Validate and retain the authoritative Base row identity."""

    if len(records) != len(canonical):
        raise ValueError("authoritative roster canonicalization changed record coverage")
    links: dict[str, str] = {}
    seen_remote_ids: set[str] = set()
    for raw, normalized in zip(records, canonical, strict=True):
        record_id = _raw_base_record_id(raw)
        if not record_id:
            raise ValueError("every authoritative roster row must preserve record_id")
        if record_id in seen_remote_ids:
            raise ValueError(f"authoritative roster contains duplicate record_id: {record_id}")
        seen_remote_ids.add(record_id)
        source_record_id = str(normalized.get("source_record_id") or "").strip()
        if not source_record_id:
            raise ValueError("authoritative roster row has no canonical source_record_id")
        if source_record_id in links:
            raise ValueError(
                f"authoritative roster contains duplicate source_record_id: {source_record_id}"
            )
        links[source_record_id] = record_id
    return links


def _authoritative_bridge_binding(
    batch: dict[str, Any],
    *,
    master: dict[str, Any],
    config: dict[str, Any],
) -> tuple[str, str]:
    if batch.get("source_complete") is not True and batch.get("complete") is not True:
        raise ValueError("authoritative roster bridge must explicitly be source_complete=true")
    if batch.get("page_cursor") or batch.get("next_page_token"):
        raise ValueError("authoritative roster bridge cannot contain a page cursor")
    base_token = str(batch.get("base_token") or "").strip()
    table_id = str(batch.get("table_id") or batch.get("people_table_id") or "").strip()
    if not base_token or not table_id:
        raise ValueError("authoritative roster bridge needs base_token and table_id")
    if master.get("base_token") and str(master["base_token"]) != base_token:
        raise ValueError("authoritative roster bridge base_token does not match config")
    configured_table = str(master.get("people_table_id") or "").strip()
    if configured_table and configured_table != table_id:
        raise ValueError("authoritative roster bridge table_id does not match config")
    configured_url = str(master.get("url") or "").strip()
    direct_base, url_table = _direct_base_url_binding(configured_url)
    if direct_base and direct_base != base_token:
        raise ValueError(
            "authoritative roster bridge base_token does not match the configured URL"
        )
    if url_table and url_table.casefold().startswith("tbl") and url_table != table_id:
        raise ValueError(
            "authoritative roster bridge table_id does not match the configured URL"
        )

    locator_proof = batch.get("locator_proof")
    if not direct_base:
        if not isinstance(locator_proof, dict):
            raise ValueError(
                "indirect authoritative Base URL requires locator_proof from the resolver"
            )
        if (
            _base_locator(locator_proof.get("requested_url"))
            != _base_locator(configured_url)
            or str(locator_proof.get("object_type") or "").casefold()
            not in {"base", "bitable"}
            or str(locator_proof.get("base_token") or "") != base_token
        ):
            raise ValueError(
                "authoritative roster locator_proof does not bind the configured URL"
            )

    if not configured_table and not (
        url_table and url_table.casefold().startswith("tbl")
    ):
        selected = batch.get("selected_table")
        if not isinstance(selected, dict) and isinstance(locator_proof, dict):
            selected = locator_proof.get("selected_table")
        expected_name = str(
            master.get("people_table_name")
            or (url_table if url_table and not url_table.casefold().startswith("tbl") else "")
            or ""
        ).strip()
        if (
            not isinstance(selected, dict)
            or str(selected.get("table_id") or selected.get("id") or "") != table_id
            or (
                expected_name
                and str(selected.get("name") or "").strip().casefold()
                != expected_name.casefold()
            )
        ):
            raise ValueError(
                "authoritative roster bridge cannot bind the selected People table"
            )
    schema_fields = batch.get("schema_fields")
    names = _field_name_set(schema_fields)
    configured_key = str((config.get("field_mapping") or {}).get("person_key") or "")
    if not names or not names.intersection(
        {configured_key, "Person Key", "人员编号", "人员 Key"} - {""}
    ):
        raise ValueError(
            "authoritative roster bridge needs the complete schema_fields including person key"
        )
    return base_token, table_id


def _master_pre_read_from_bridge(
    payload: dict[str, Any], master: dict[str, Any] | None
) -> dict[str, list[dict[str, Any]]]:
    """Validate an optional full live snapshot used to bind safe update hashes."""

    pre_read = payload.get("master_pre_read")
    if pre_read is None:
        return {}
    if not isinstance(pre_read, dict) or pre_read.get("source_complete") is not True:
        raise ValueError("master_pre_read must be a complete object")
    if pre_read.get("page_cursor") or pre_read.get("next_page_token"):
        raise ValueError("master_pre_read cannot contain a page cursor")
    base_token = str(pre_read.get("base_token") or "").strip()
    table_ids = pre_read.get("table_ids")
    schema_fields = pre_read.get("schema_fields")
    records = pre_read.get("records")
    if (
        not base_token
        or not isinstance(table_ids, dict)
        or not isinstance(schema_fields, dict)
        or not isinstance(records, dict)
        or set(table_ids) != set(schema_fields)
        or set(table_ids) != set(records)
    ):
        raise ValueError("master_pre_read table/schema/record coverage is incomplete")
    configured = master or {}
    if configured.get("base_token") and str(configured["base_token"]) != base_token:
        raise ValueError("master_pre_read base_token does not match configured Base")
    expected_ids = {
        "People": configured.get("people_table_id"),
        "Sources": configured.get("sources_table_id"),
    }
    output: dict[str, list[dict[str, Any]]] = {}
    for table, rows in records.items():
        if table not in {"People", "Sources"} or not isinstance(rows, list):
            raise ValueError("master_pre_read contains an unsupported table")
        table_id = str(table_ids.get(table) or "").strip()
        if not table_id or (
            expected_ids.get(table) and str(expected_ids[table]) != table_id
        ):
            raise ValueError(f"master_pre_read {table} table_id does not match config")
        if not _field_name_set(schema_fields.get(table)):
            raise ValueError(f"master_pre_read {table} schema_fields are incomplete")
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"master_pre_read {table} records must be objects")
            record_id = _raw_base_record_id(row)
            if not record_id or record_id in seen:
                raise ValueError(f"master_pre_read {table} record_id coverage is invalid")
            seen.add(record_id)
        output[table] = rows
    return output


def _store_master_live_pre_read(
    state: PortableState,
    *,
    base_token: str,
    people_table_id: str,
    sources_table_id: str | None,
    records: dict[str, list[dict[str, Any]]],
) -> None:
    state.set_meta(
        "master_live_pre_read",
        {
            "base_token": base_token,
            "table_ids": {
                "People": people_table_id,
                **({"Sources": sources_table_id} if sources_table_id else {}),
            },
            "records": records,
            "captured_at": utc_now(),
            "snapshot_hash": _bridge_contract_hash(records),
        },
    )


def _load_master_live_pre_read(
    state: PortableState,
    *,
    base_token: str,
    people_table_id: str,
    sources_table_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    snapshot = state.get_meta("master_live_pre_read")
    if not isinstance(snapshot, dict) or snapshot.get("base_token") != base_token:
        return {}
    expected_ids = {
        "People": people_table_id,
        **({"Sources": sources_table_id} if sources_table_id else {}),
    }
    if snapshot.get("table_ids") != expected_ids:
        return {}
    try:
        captured = datetime.fromisoformat(
            str(snapshot.get("captured_at") or "").replace("Z", "+00:00")
        )
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        captured = captured.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return {}
    now = datetime.now(timezone.utc)
    if (
        captured > now + timedelta(minutes=5)
        or now - captured > _MASTER_LIVE_PRE_READ_MAX_AGE
    ):
        return {}
    records = snapshot.get("records")
    if (
        not isinstance(records, dict)
        or snapshot.get("snapshot_hash") != _bridge_contract_hash(records)
    ):
        return {}
    return records


def _reconcile_roster_batch(
    state: PortableState,
    source_ref: str,
    records: list[dict[str, Any]],
    *,
    source_complete: bool,
    page_cursor: str | None,
    authoritative: bool,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "source_complete": source_complete,
        "page_cursor": page_cursor,
    }
    parameters = inspect.signature(state.reconcile_source_records).parameters.values()
    if any(
        parameter.name == "authoritative"
        or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    ):
        kwargs["authoritative"] = authoritative
    return state.reconcile_source_records(source_ref, records, **kwargs)


def _link_authoritative_roster_rows(
    state: PortableState,
    *,
    result: dict[str, Any],
    record_links: dict[str, str],
    base_token: str,
    table_id: str,
) -> None:
    """Bind projections/review rows back to their user-owned People rows."""

    for outcome in result.get("records") or []:
        source_record_id = str(outcome.get("source_record_id") or "")
        record_id = record_links.get(source_record_id)
        # A removed row is absent from the live Base and intentionally keeps its
        # historical link.  All live outcomes must retain their original row ID.
        if not record_id:
            if outcome.get("outcome") != "removed":
                raise ValueError(
                    f"authoritative reconciliation lost Base row {source_record_id}"
                )
            continue
        if outcome.get("outcome") == "pending_review":
            issues = outcome.get("issues") or []
            if not issues:
                raise ValueError("pending authoritative roster row has no review issue")
            for issue in issues:
                issue_id = str((issue or {}).get("issue_id") or "").strip()
                if not issue_id:
                    raise ValueError("pending authoritative roster issue has no issue_id")
                state.set_feishu_record_link(
                    entity_kind="person",
                    entity_key=f"review:{issue_id}",
                    base_token=base_token,
                    table_id=table_id,
                    record_id=record_id,
                )
            continue
        person_key = str(outcome.get("person_key") or "").strip()
        if person_key:
            state.set_feishu_record_link(
                entity_kind="person",
                entity_key=person_key,
                base_token=base_token,
                table_id=table_id,
                record_id=record_id,
            )


def _live_master_fields(
    cli: LarkCli,
    *,
    base_token: str,
    people_table_id: str,
    sources_table_id: str | None,
) -> dict[str, list[dict[str, Any]]]:
    fields = {
        "People": cli.list_base_fields(
            base_token=base_token,
            table_id=people_table_id,
            identity="user",
        )
    }
    if sources_table_id and sources_table_id != people_table_id:
        fields["Sources"] = cli.list_base_fields(
            base_token=base_token,
            table_id=sources_table_id,
            identity="user",
        )
    return fields


def _semantic_field_name(
    available_fields: list[dict[str, Any]],
    field_mapping: dict[str, Any],
    semantic: str,
) -> str | None:
    available = _field_name_set(available_fields)
    by_fold = {name.casefold(): name for name in available}
    spec = PEOPLE_FIELD_SPECS[semantic]
    candidates = [
        (field_mapping or {}).get(semantic),
        spec["field_name"],
        *spec.get("aliases", ()),
    ]
    return next(
        (
            by_fold[str(candidate).casefold()]
            for candidate in candidates
            if candidate and str(candidate).casefold() in by_fold
        ),
        None,
    )


def _mask_authoritative_review_identity(
    state: PortableState,
    records: list[dict[str, Any]],
    *,
    base_token: str,
    table_id: str,
    available_fields: list[dict[str, Any]],
    field_mapping: dict[str, Any],
) -> list[dict[str, Any]]:
    """Prevent a review projection from retyping its original roster row.

    The mirror still annotates review status/detail on that row, but sees the
    synthetic review identity only in this in-memory comparison snapshot.  It
    therefore never writes ``Record Type=审核项`` or a ``review:...`` person key
    back to the authoritative input row.
    """

    review_links = {
        str(row["record_id"]): str(row["entity_key"])
        for row in state.db.execute(
            """SELECT entity_key,record_id FROM feishu_record_links
               WHERE entity_kind='person' AND base_token=? AND table_id=?
                 AND entity_key LIKE 'review:%'""",
            (base_token, table_id),
        )
    }
    if not review_links:
        return [json.loads(json.dumps(record, ensure_ascii=False)) for record in records]
    key_field = _semantic_field_name(
        available_fields, field_mapping, "person_key"
    )
    type_field = _semantic_field_name(
        available_fields, field_mapping, "record_type"
    )
    masked: list[dict[str, Any]] = []
    for record in records:
        item = json.loads(json.dumps(record, ensure_ascii=False))
        entity_key = review_links.get(_raw_base_record_id(record))
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else item
        if entity_key and key_field:
            fields[key_field] = entity_key
        if entity_key and type_field:
            fields[type_field] = "审核项"
        masked.append(item)
    return masked


def _bridge_safe_master_records(
    state: PortableState,
    records: dict[str, list[dict[str, Any]]],
    *,
    authoritative_roster: bool,
    base_token: str,
    people_table_id: str,
    sources_table_id: str | None = None,
    authoritative_source_ref: str | None = None,
    live_records: dict[str, list[dict[str, Any]]] | None = None,
    field_mapping: dict[str, Any] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    output = json.loads(json.dumps(records, ensure_ascii=False))
    live_by_table: dict[str, dict[str, dict[str, Any]]] = {}
    live_key_by_table: dict[str, dict[str, str]] = {}
    for table, table_records in (live_records or {}).items():
        indexed: dict[str, dict[str, Any]] = {}
        for record in table_records or []:
            record_id = _raw_base_record_id(record)
            if not record_id:
                raise ValueError(f"{table} live pre-read contains a row without record_id")
            if record_id in indexed:
                raise ValueError(f"{table} live pre-read contains duplicate record_id: {record_id}")
            indexed[record_id] = _bridge_record_fields(record)
        live_by_table[table] = indexed
        specs = PEOPLE_FIELD_SPECS if table == "People" else SOURCE_FIELD_SPECS
        stable_semantic = "person_key" if table == "People" else "source_key"
        available_fields = [
            {"field_name": field_name}
            for field_name in sorted(
                {
                    field_name
                    for fields in indexed.values()
                    for field_name in fields
                },
                key=str.casefold,
            )
        ]
        stable_field = _semantic_field_name(
            available_fields,
            _table_bridge_mapping(field_mapping, table),
            stable_semantic,
        )
        stable_index: dict[str, str] = {}
        if stable_field:
            for record_id, fields in indexed.items():
                value = _bridge_stable_key_value(fields.get(stable_field))
                if not value:
                    continue
                if value in stable_index and stable_index[value] != record_id:
                    raise ValueError(
                        f"{table} live pre-read contains duplicate stable key: {value}"
                    )
                stable_index[value] = record_id
        live_key_by_table[table] = stable_index
    for item in output.get("People", []):
        entity_key = str(item.get("entity_key") or "")
        target_record_id = state.feishu_record_link(
            entity_kind="person",
            entity_key=entity_key,
            base_token=base_token,
            table_id=people_table_id,
        )
        stable_record_id = (live_key_by_table.get("People") or {}).get(entity_key)
        if target_record_id and stable_record_id and target_record_id != stable_record_id:
            raise ValueError(
                f"People live stable key conflicts with the local record link: {entity_key}"
            )
        if not target_record_id and stable_record_id:
            target_record_id = stable_record_id
        if target_record_id:
            item["target_record_id"] = target_record_id
        authoritative_origin = bool(
            authoritative_roster
            and entity_key.startswith("review:")
            and authoritative_source_ref
            and str(item.get("origin_source_ref") or "") == authoritative_source_ref
            and str(item.get("origin_record_id") or "")
        )
        if authoritative_origin and not target_record_id:
            raise ValueError(
                f"authoritative review item has no original Base row: {item['entity_key']}"
            )
        if authoritative_origin and str(item.get("origin_record_id")) != target_record_id:
            raise ValueError(
                f"authoritative review item points at the wrong Base row: {item['entity_key']}"
            )
        values = item.get("field_values") or {}
        if authoritative_origin:
            # Review state is an annotation on the original user row, not a new
            # row type.  The outcome still uses review:<issue_id> to establish
            # its link.
            values.pop("person_key", None)
            values["record_type"] = "人员"
            item["preserve_authoritative_identity"] = True
        _attach_bridge_write_contract(
            item,
            table="People",
            specs=PEOPLE_FIELD_SPECS,
            target_record_id=target_record_id,
            values=values,
            live_fields=(live_by_table.get("People") or {}).get(
                str(target_record_id or "")
            ),
            field_mapping=_table_bridge_mapping(field_mapping, "People"),
        )
    if sources_table_id:
        for item in output.get("Sources", []):
            entity_key = str(item.get("entity_key") or "")
            target_record_id = state.feishu_record_link(
                entity_kind="source",
                entity_key=entity_key,
                base_token=base_token,
                table_id=sources_table_id,
            )
            stable_record_id = (live_key_by_table.get("Sources") or {}).get(
                entity_key
            )
            if target_record_id and stable_record_id and target_record_id != stable_record_id:
                raise ValueError(
                    f"Sources live stable key conflicts with the local record link: {entity_key}"
                )
            if not target_record_id and stable_record_id:
                target_record_id = stable_record_id
            if target_record_id:
                item["target_record_id"] = target_record_id
            _attach_bridge_write_contract(
                item,
                table="Sources",
                specs=SOURCE_FIELD_SPECS,
                target_record_id=target_record_id,
                values=item.get("field_values") or {},
                live_fields=(live_by_table.get("Sources") or {}).get(
                    str(target_record_id or "")
                ),
                field_mapping=_table_bridge_mapping(field_mapping, "Sources"),
            )
    return output


def _bridge_record_fields(record: dict[str, Any]) -> dict[str, Any]:
    fields = record.get("fields")
    if isinstance(fields, dict):
        return json.loads(json.dumps(fields, ensure_ascii=False, allow_nan=False))
    metadata = {"record_id", "id", "row_id", "created_time", "last_modified_time"}
    return {
        str(key): json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        for key, value in record.items()
        if key not in metadata
    }


def _bridge_stable_key_value(value: Any) -> str:
    """Read a text-like Feishu cell without guessing from multi-value cells."""

    if isinstance(value, dict):
        for key in ("text", "value", "name"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int)) and str(candidate).strip():
                return str(candidate).strip()
        return ""
    if isinstance(value, list):
        if len(value) != 1:
            return ""
        return _bridge_stable_key_value(value[0])
    return str(value or "").strip()


def _bridge_field_identity(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return " ".join(normalized.split())


def _table_bridge_mapping(field_mapping: dict[str, Any] | None, table: str) -> dict[str, Any]:
    supplied = field_mapping or {}
    nested = supplied.get(table) if isinstance(supplied, dict) else None
    if isinstance(nested, dict):
        return nested
    if table == "People" and isinstance(supplied, dict):
        return supplied
    return {}


def _bridge_field_candidates(
    specs: dict[str, dict[str, Any]],
    mapping: dict[str, Any],
    semantics: list[str],
) -> dict[str, list[str]]:
    output: dict[str, list[str]] = {}
    for semantic in semantics:
        spec = specs[semantic]
        candidates = [
            str(mapping.get(semantic) or "").strip(),
            str(spec.get("field_name") or "").strip(),
            *(str(value).strip() for value in spec.get("aliases") or ()),
        ]
        unique: list[str] = []
        seen: set[str] = set()
        for value in candidates:
            identity = _bridge_field_identity(value)
            if value and identity and identity not in seen:
                unique.append(value)
                seen.add(identity)
        output[semantic] = unique
    return output


def _attach_bridge_write_contract(
    item: dict[str, Any],
    *,
    table: str,
    specs: dict[str, dict[str, Any]],
    target_record_id: str | None,
    values: dict[str, Any],
    live_fields: dict[str, Any] | None,
    field_mapping: dict[str, Any],
) -> None:
    """Replace an unsafe desired row with a machine-only verified write plan."""

    machine_allowlist = sorted(
        semantic for semantic, spec in specs.items() if spec.get("owner") == "machine"
    )
    human_allowlist = sorted(
        semantic for semantic, spec in specs.items() if spec.get("owner") == "human"
    )
    machine_patch = {
        semantic: values[semantic]
        for semantic in machine_allowlist
        if semantic in values
    }
    desired_human_fields = {
        semantic: values.get(semantic) for semantic in human_allowlist
    }
    operation = "update" if target_record_id else "create"
    if target_record_id and live_fields is None:
        raise ValueError(
            f"master bridge update needs a verified live pre-read: {table}:{item.get('entity_key')}"
        )
    before = {
        "target_record_id": target_record_id or None,
        "not_found": not bool(target_record_id),
        "fields": live_fields if target_record_id else {},
    }
    create_human_fields = (
        {
            semantic: value
            for semantic, value in desired_human_fields.items()
            if value not in (None, "", [], {})
        }
        if not target_record_id
        else {}
    )
    machine_candidates = _bridge_field_candidates(
        specs, field_mapping, sorted(machine_patch)
    )
    human_candidates = _bridge_field_candidates(
        specs, field_mapping, human_allowlist
    )
    machine_candidate_ids = {
        _bridge_field_identity(name)
        for candidates in machine_candidates.values()
        for name in candidates
    }
    human_candidate_ids = {
        _bridge_field_identity(name)
        for candidates in human_candidates.values()
        for name in candidates
    }
    if machine_candidate_ids & human_candidate_ids:
        raise ValueError(
            f"{table} bridge field ownership collision before write contract creation"
        )
    candidate_bindings: dict[str, set[str]] = {}
    for semantic, candidates in {**machine_candidates, **human_candidates}.items():
        for candidate in candidates:
            identity = _bridge_field_identity(candidate)
            if identity:
                candidate_bindings.setdefault(identity, set()).add(semantic)
    if any(len(semantics) > 1 for semantics in candidate_bindings.values()):
        raise ValueError(
            f"{table} bridge field semantic collision before write contract creation"
        )
    contract = {
        "protocol": "machine-fields-verified-v1",
        "table": table,
        "entity_key": str(item.get("entity_key") or ""),
        "operation": operation,
        "target_record_id": target_record_id or None,
        "machine_field_allowlist": machine_allowlist,
        "human_field_allowlist": human_allowlist,
        "machine_field_candidates": machine_candidates,
        "human_field_candidates": human_candidates,
        "machine_patch": machine_patch,
        "machine_patch_hash": _bridge_contract_hash(machine_patch),
        "expected_before_fields": before,
        "expected_before_hash": _bridge_contract_hash(before),
        "create_human_fields": create_human_fields,
    }
    contract["contract_hash"] = _bridge_contract_hash(contract)
    # ``fields`` previously exposed a complete desired row and made it easy for
    # an adapter to overwrite user-owned values accidentally.  The executable
    # portion is now machine-only; initial human values are separately scoped
    # to create operations and every update carries a before/post human guard.
    item.pop("fields", None)
    item["field_values"] = machine_patch
    item["write_contract"] = contract


def _runtime_paths(args: argparse.Namespace) -> RuntimePaths:
    if args.config_root:
        os.environ["PEOPLE_TRACKING_FEISHU_CONFIG_ROOT"] = str(args.config_root)
    if args.state_root:
        os.environ["PEOPLE_TRACKING_FEISHU_STATE_ROOT"] = str(args.state_root)
    paths = RuntimePaths.discover()
    paths.ensure()
    return paths


def _lark(executable: str | None, *, strict: bool = True) -> LarkCli | None:
    try:
        cli = LarkCli(executable)
        if strict:
            cli.assert_pinned()
        return cli
    except LarkCliError:
        if strict:
            raise
        return None


def _runtime_mode(config: dict[str, Any] | None) -> str:
    requested = str(((config or {}).get("runtime") or {}).get("mode") or "auto")
    if requested in {"openclaw", "claude_lark_cli"}:
        return requested
    if shutil.which("openclaw") or Path("~/.openclaw").expanduser().exists():
        return "openclaw"
    return "claude_lark_cli"


def _public_auth(status: dict[str, Any]) -> dict[str, Any]:
    identities = status.get("identities") if isinstance(status, dict) else {}
    return {
        "identity": status.get("identity") if isinstance(status, dict) else None,
        "bot_available": bool(((identities or {}).get("bot") or {}).get("available")),
        "user_available": bool(((identities or {}).get("user") or {}).get("available")),
        "brand": status.get("brand") if isinstance(status, dict) else None,
        "secret_values_read": False,
    }


def command_doctor(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths, required=False)
    executable = args.lark_cli or shutil.which("lark-cli")
    lark_report: dict[str, Any] = {
        "executable": executable,
        "required_version": PINNED_LARK_CLI,
        "available": False,
        "version_matches": False,
    }
    if executable:
        try:
            cli = LarkCli(executable)
            version = cli.version()
            lark_report.update(
                {
                    "available": True,
                    "version": version,
                    "version_matches": version == PINNED_LARK_CLI,
                    "doctor": cli.doctor(offline=True),
                    "auth": _public_auth(cli.auth_status()),
                }
            )
        except Exception as exc:
            lark_report["error"] = f"{type(exc).__name__}: {exc}"
    state = PortableState(paths.database)
    try:
        database = state.status()
    finally:
        state.close()
    report = {
        "version": __version__,
        "platform": {"system": platform.system(), "machine": platform.machine()},
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "supported": sys.version_info[:2] in {(3, 11), (3, 12)},
        },
        "node": _tool_version("node", ["--version"]),
        "openclaw": _tool_version("openclaw", ["--version"]),
        "claude": _tool_version("claude", ["--version"]),
        "runtime_mode": _runtime_mode(config),
        "lark_cli": lark_report,
        "configuration": {
            "exists": config is not None,
            "state": config.get("state") if config else None,
            "path": str(paths.config_file),
            "mode_0600": paths.config_file.exists()
            and not bool(paths.config_file.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO)),
        },
        "state": database,
        "mutated_external_configuration": False,
        "secrets_read": False,
    }
    report["agent_review"] = {
        "mode": "execution_agent",
        "decision_authority": "skill_host_agent",
        "external_model_api": False,
    }
    return report


def _tool_version(name: str, args: list[str]) -> dict[str, Any]:
    executable = shutil.which(name)
    if not executable:
        return {"available": False}
    try:
        result = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=10, check=False
        )
        return {
            "available": result.returncode == 0,
            "executable": executable,
            "version_output": (result.stdout or result.stderr).strip()[:500],
        }
    except Exception as exc:
        return {"available": False, "executable": executable, "error": str(exc)}


def command_onboarding(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    if args.answers:
        answers = _json_file(args.answers)
        config = normalize_answers(answers)
        backup = None
        if paths.config_file.exists():
            backup = paths.config_file.with_name(
                f"config.backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
            )
            backup.write_bytes(paths.config_file.read_bytes())
            backup.chmod(0o600)
        secure_write_json(paths.config_file, config)
        return {
            "state": "draft",
            "config": _public_config(config),
            "backup": str(backup) if backup else None,
            "next": "run source-probe; do not sync or schedule yet",
        }
    config = load_config(paths, required=False)
    if args.enable:
        if args.confirmation != ENABLE_PHRASE:
            raise ValueError(f"enable requires --confirmation {ENABLE_PHRASE}")
        if not config or config.get("state") != "validated":
            raise ValueError("configuration must be validated by source-probe first")
        scheduler = _install_configured_scheduler(config, args) if args.register_schedule else None
        enabled = mark_state(paths, config, "enabled")
        return {
            "state": "enabled",
            "config": _public_config(enabled),
            "scheduler": scheduler,
            "next": "run sync --apply, then scan; preview sandbox-e2e before its apply step",
        }
    return {
        "state": config.get("state") if config else "not_started",
        "questionnaire": QUESTIONNAIRE,
        "config": _public_config(config) if config and args.show else None,
        "required_confirmation": ENABLE_PHRASE,
    }


def _public_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    if config is None:
        return None
    public = json.loads(json.dumps(config))
    deepseek = ((public.get("apis") or {}).get("deepseek") or {})
    if deepseek.get("key_reference"):
        deepseek["key_reference"] = "<configured-reference>"
    for item in ((public.get("apis") or {}).get("search") or []):
        if item.get("secret_reference"):
            item["secret_reference"] = "<configured-reference>"
    return public


def _audience_output_config(config: dict[str, Any], audience: str) -> dict[str, Any]:
    outputs = config.get("outputs") or {}
    if audience == "developer":
        value = outputs.get("developer") or {}
        return value if isinstance(value, dict) else {}
    value = outputs.get("public")
    if isinstance(value, dict):
        return value
    return {
        "document": outputs.get("document") or {"enabled": False},
        "message": outputs.get("message") or {"enabled": False},
    }


def _install_configured_scheduler(
    config: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    launcher = Path(
        getattr(args, "launcher", None)
        or shutil.which("people-tracking-feishu")
        or sys.argv[0]
    ).expanduser().resolve()
    if _runtime_mode(config) == "openclaw":
        return _register_openclaw_scheduler(
            launcher=launcher,
            timezone_name=config["schedule"]["timezone"],
            apply=True,
        )
    raise RuntimeError(
        "automatic scheduling requires an isolated OpenClaw host-Agent turn; "
        "claude_lark_cli cannot safely perform execution-agent review unattended"
    )


def command_source_probe(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    if config.get("state") == "enabled":
        raise ValueError("enabled configuration cannot be silently revalidated; create a new draft")
    if args.bridge_results:
        results = _json_file(args.bridge_results)
        sources = results.get("sources") if isinstance(results, dict) else None
        if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
            raise ValueError("bridge results must contain sources[]")
        if any(item.get("ok") is not True for item in sources):
            raise ValueError("every bridged source probe must state ok=true")
        _validate_bridge_result(
            paths,
            results,
            purpose="source_probe",
            completed_refs=[str(item.get("source_ref") or "") for item in sources],
        )
        validation = {
            "validated_at": utc_now(),
            "adapter": "agent_tool_bridge",
            "results": sources,
            "coverage": {
                "expected_refs": results["expected_refs"],
                "expected_refs_hash": results["expected_refs_hash"],
                "request_hash": results["request_hash"],
            },
            "all_ok": True,
            "secret_values_read": False,
        }
        validated = mark_state(paths, config, "validated", validation=validation)
        return {"state": "validated", "validation": validation, "config": _public_config(validated)}
    mode = _runtime_mode(config)
    cli = _lark(args.lark_cli, strict=True) if mode == "claude_lark_cli" else None
    results: list[dict[str, Any]] = []
    bridge_actions: list[dict[str, Any]] = []
    authoritative_master = _authoritative_master(config)
    for index, source in enumerate(config["sources"]):
        if authoritative_master is not None and _configured_source_is_master(
            source, authoritative_master
        ):
            continue
        kind = source["kind"]
        location = source.get("url") or source.get("path")
        ref = str(source.get("source_ref") or f"source-{index + 1}")
        if kind == "local_file":
            records = records_from_file(Path(str(location)).expanduser())
            field_names = sorted({str(key) for record in records[:50] for key in record})
            results.append({"source_ref": ref, "kind": kind, "ok": True, "records": len(records), "fields": field_names})
        elif kind == "people_intel_api":
            results.append(_probe_api(ref, str(location)))
        elif cli is None:
            bridge_actions.append(
                {
                    "source_ref": ref,
                    "kind": kind,
                    "operation": "read_schema_or_document",
                    "url": location,
                    "identity": "user",
                    "read_only": True,
                }
            )
        elif kind == "feishu_base":
            schema = cli.base_schema(
                str(location),
                identity="user",
                configured_table=source.get("table_id") or source.get("table_name"),
            )
            fields = [str(item.get("field_name") or item.get("name") or "") for item in schema["fields"]]
            results.append(
                {
                    "source_ref": ref,
                    "kind": kind,
                    "ok": True,
                    "base_token": schema["base_token"],
                    "selected_table": schema["selected_table"],
                    "fields": fields,
                }
            )
        else:
            document = cli.fetch_document(str(location), identity="user")
            results.append(
                {
                    "source_ref": ref,
                    "kind": kind,
                    "ok": True,
                    "title": first_value(document, "title", "name"),
                    "content_available": bool(
                        first_value(document, "markdown", "content", "text")
                    ),
                }
            )
    master = config.get("master_database") or {}
    if master.get("mode") == "existing_base" and master.get("url"):
        master_source_ref = authoritative_roster_source_ref(master)
        if cli is None:
            bridge_actions.append(
                {
                    "source_ref": master_source_ref,
                    "kind": "feishu_base",
                    "operation": "read_schema",
                    "url": master["url"],
                    "identity": "user",
                    "read_only": True,
                }
            )
        else:
            schema = cli.base_schema(
                str(master["url"]),
                identity="user",
                configured_table=master.get("people_table_id") or master.get("people_table_name"),
            )
            tables_by_name = {
                str(item.get("name") or ""): str(item.get("table_id") or item.get("id") or "")
                for item in schema["tables"]
            }
            selected = schema.get("selected_table") or {}
            master["base_token"] = schema["base_token"]
            master["people_table_id"] = str(
                selected.get("table_id")
                or selected.get("id")
                or tables_by_name.get(str(master.get("people_table_name") or "People"), "")
            )
            master["sources_table_id"] = str(
                master.get("sources_table_id")
                or tables_by_name.get(str(master.get("sources_table_name") or "Sources"), "")
            )
            master["authoritative_roster"] = True
            master["authoritative_source_ref"] = master_source_ref
            config["master_database"] = master
            results.append(
                {
                    "source_ref": master_source_ref,
                    "kind": "feishu_base",
                    "ok": True,
                    "base_token": schema["base_token"],
                    "tables": schema["tables"],
                    "fields": [
                        str(item.get("field_name") or item.get("name") or "")
                        for item in schema["fields"]
                    ],
                }
            )
    if bridge_actions:
        request = _new_bridge_request(
            paths,
            purpose="source_probe",
            expected_refs=[str(action["source_ref"]) for action in bridge_actions],
            request_content={"actions": bridge_actions, "config": config},
        )
        return {
            "state": "draft",
            "adapter": "agent_tool_bridge",
            "results": results,
            "actions": _bridge_actions(bridge_actions, request),
            "bridge_request": _bridge_public_fields(request),
            "next": "execute read-only actions with the official OpenClaw Feishu plugin and submit --bridge-results",
        }
    if not all(item.get("ok") for item in results):
        return {"state": "draft", "results": results, "all_ok": False}
    validation = {
        "validated_at": utc_now(),
        "adapter": "lark_cli",
        "results": results,
        "all_ok": True,
        "secret_values_read": False,
    }
    validated = mark_state(paths, config, "validated", validation=validation)
    return {"state": "validated", "validation": validation, "config": _public_config(validated)}


def _probe_api(source_ref: str, url: str) -> dict[str, Any]:
    try:
        request = urllib.request.Request(
            url.rstrip("/") + "/health",
            headers={"Accept": "application/json"},
        )
        with _public_urlopen(request, timeout=10) as response:
            body = response.read(1_000_000)
        payload = json.loads(body) if body else {}
        return {"source_ref": source_ref, "kind": "people_intel_api", "ok": response.status == 200, "health": payload}
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"source_ref": source_ref, "kind": "people_intel_api", "ok": False, "error": str(exc)}


def command_sync(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    if config.get("state") != "enabled" and not (
        getattr(args, "allow_validated", False) and config.get("state") == "validated"
    ):
        raise ValueError("sync requires enabled configuration")
    mode = _runtime_mode(config)
    cli = _lark(args.lark_cli, strict=True) if mode == "claude_lark_cli" else None
    if args.master_bridge_results:
        payload = _json_file(args.master_bridge_results)
        state = PortableState(paths.database)
        try:
            consumed, links = _validated_master_bridge_contract(
                paths,
                payload,
                config=config,
                state=state,
            )
            table_ids = payload["table_ids"]
            master = dict(config.get("master_database") or {})
            master.update(
                {
                    "mode": "existing_base",
                    "base_token": payload["base_token"],
                    "url": payload.get("base_url") or master.get("url"),
                    "people_table_id": table_ids["People"],
                    "sources_table_id": table_ids.get("Sources"),
                    "authoritative_roster": True,
                    "created_by_bridge_at": utc_now(),
                }
            )
            master["authoritative_source_ref"] = authoritative_roster_source_ref(master)
            config["master_database"] = master
            config["updated_at"] = utc_now()
            validate_config(config, require_complete=True)
            # Persist the validated locator first; the pending bridge remains
            # retryable if this filesystem write fails.  Request consumption and
            # row-link creation then happen in one SQLite transaction.
            secure_write_json(paths.config_file, config)
            _consume_master_bridge(state, request=consumed, links=links)
            return {
                "master_bridge_result_applied": True,
                "master_database": master,
                "linked_records": len(links),
            }
        finally:
            state.close()
    master = _authoritative_master(config)
    authoritative_source_ref = (
        authoritative_roster_source_ref(master) if master is not None else None
    )
    authoritative_refs = (
        {authoritative_source_ref} if authoritative_source_ref is not None else set()
    )
    raw_batches: list[tuple[str, list[dict[str, Any]]]] = []
    batch_metadata: dict[str, dict[str, Any]] = {}
    source_base_bindings: dict[str, tuple[str, str]] = {}
    roster_record_links: dict[str, dict[str, str]] = {}
    authoritative_raw_records: list[dict[str, Any]] | None = None
    live_master_records: dict[str, list[dict[str, Any]]] = {}
    source_bridge_payload: dict[str, Any] | None = None
    source_bridge_completed_refs: list[str] = []
    if args.bridge_input:
        payload = _json_file(args.bridge_input)
        batches = payload.get("sources") if isinstance(payload, dict) else None
        if not isinstance(batches, list):
            raise ValueError("bridge input must contain sources[]")
        if any(not isinstance(batch, dict) for batch in batches):
            raise ValueError("bridge input sources must be objects")
        live_master_records.update(_master_pre_read_from_bridge(payload, master))
        parsed_batches: list[tuple[str, list[dict[str, Any]]]] = []
        for batch in batches:
            ref = str(batch.get("source_ref") or "")
            records = records_from_payload(batch.get("payload"))
            parsed_batches.append((ref, records))
            source_complete = batch.get("source_complete") is True or batch.get("complete") is True
            page_cursor = batch.get("page_cursor") or batch.get("next_page_token")
            if source_complete and page_cursor:
                raise ValueError(f"complete bridge source cannot carry a page cursor: {ref}")
            batch_metadata[ref] = {
                "source_complete": source_complete,
                "page_cursor": str(page_cursor) if page_cursor else None,
            }
            if ref in authoritative_refs:
                assert master is not None
                authoritative_raw_records = records
                live_master_records["People"] = records
                authoritative_ids = [_raw_base_record_id(item) for item in records]
                if any(not record_id for record_id in authoritative_ids):
                    raise ValueError(
                        "every authoritative roster bridge row must preserve record_id"
                    )
                if len(authoritative_ids) != len(set(authoritative_ids)):
                    raise ValueError("authoritative roster bridge contains duplicate record_id")
                binding = _authoritative_bridge_binding(
                    batch,
                    master=master,
                    config=config,
                )
                source_base_bindings[ref] = binding
                master["base_token"], master["people_table_id"] = binding
                master["authoritative_roster"] = True
                master["authoritative_source_ref"] = ref
        empty_payload_refs = [
            ref
            for (ref, records), batch in zip(parsed_batches, batches, strict=True)
            if not records and batch.get("payload") not in ([], {"records": []})
        ]
        if empty_payload_refs:
            raise ValueError(
                "bridge source payload did not match the supported records schema: "
                f"refs={empty_payload_refs}; use sources[].payload.records[]"
            )
        internal_empty = not batches and not getattr(args, "include_local_sources", True)
        if not internal_empty:
            if authoritative_source_ref and authoritative_source_ref not in {
                ref for ref, _ in parsed_batches
            }:
                raise ValueError("authoritative roster bridge batch is missing")
            _validate_bridge_result(
                paths,
                payload,
                purpose="source_sync",
                completed_refs=[ref for ref, _ in parsed_batches],
                consume=False,
            )
            source_bridge_payload = payload
            source_bridge_completed_refs = [ref for ref, _ in parsed_batches]
        raw_batches.extend(parsed_batches)
        if getattr(args, "include_local_sources", True):
            bridged_refs = {ref for ref, _ in raw_batches}
            for index, source in enumerate(config["sources"]):
                ref = str(source.get("source_ref") or f"source-{index + 1}")
                if master is not None and _configured_source_is_master(source, master):
                    continue
                if source["kind"] == "local_file" and ref not in bridged_refs:
                    raw_batches.append(
                        (
                            ref,
                            records_from_file(Path(str(source.get("path") or source.get("url"))).expanduser()),
                        )
                    )
                    batch_metadata[ref] = {"source_complete": True, "page_cursor": None}
    else:
        bridge_actions: list[dict[str, Any]] = []
        for index, source in enumerate(config["sources"]):
            ref = str(source.get("source_ref") or f"source-{index + 1}")
            kind = source["kind"]
            location = str(source.get("url") or source.get("path"))
            if master is not None and _configured_source_is_master(source, master):
                # The master People table is injected once below under its stable
                # authoritative ref.  Legacy configs may still repeat it in
                # sources; never ingest a view-filtered duplicate.
                continue
            if kind == "local_file":
                raw_batches.append((ref, records_from_file(Path(location).expanduser())))
                batch_metadata[ref] = {"source_complete": True, "page_cursor": None}
            elif kind == "feishu_base" and cli:
                schema = cli.base_schema(
                    location,
                    configured_table=source.get("table_id") or source.get("table_name"),
                )
                selected = schema["selected_table"] or {}
                table_id = str(selected.get("table_id") or selected.get("id") or selected.get("name") or "")
                records = cli.list_base_records(
                    base_token=schema["base_token"],
                    table_id=table_id,
                    view_id=source.get("view_id") or source.get("view_name"),
                    # Read the complete row after discovering the live schema.
                    # Optional v0.9 fields may not exist yet, while user-owned
                    # columns must survive reconciliation.  Passing configured
                    # display names as field IDs is both brittle and lossy.
                    field_names=None,
                )
                raw_batches.append((ref, records))
                batch_metadata[ref] = {"source_complete": True, "page_cursor": None}
                source_base_bindings[ref] = (str(schema["base_token"]), table_id)
            elif kind in {"feishu_doc", "feishu_wiki"} and cli:
                raw_batches.append((ref, records_from_payload(cli.fetch_document(location))))
                batch_metadata[ref] = {"source_complete": True, "page_cursor": None}
            elif kind == "people_intel_api":
                bridge_actions.append({"source_ref": ref, "operation": "people_intel_export", "url": location})
            else:
                bridge_actions.append(
                    {
                        "source_ref": ref,
                        "operation": "read_people_source",
                        "kind": kind,
                        "url": location,
                        "identity": "user",
                        "result_requirements": {
                            "source_complete": "true only after every page was read",
                            "page_cursor": "required when source_complete is false and more pages remain",
                            "record_id": "preserve the stable Feishu record_id on every record",
                        },
                    }
                )
        if master is not None:
            assert authoritative_source_ref is not None
            configured_table = (
                master.get("people_table_id")
                or master.get("people_table_name")
                or master.get("table_id")
                or master.get("table_name")
            )
            if cli is not None:
                schema = cli.base_schema(
                    str(master["url"]),
                    identity="user",
                    configured_table=str(configured_table) if configured_table else None,
                )
                selected = schema.get("selected_table") or {}
                table_id = str(
                    selected.get("table_id")
                    or selected.get("id")
                    or selected.get("name")
                    or ""
                ).strip()
                base_token = str(schema.get("base_token") or "").strip()
                if not base_token or not table_id:
                    raise ValueError("unable to resolve the authoritative People table")
                if master.get("base_token") and str(master["base_token"]) != base_token:
                    raise ValueError("authoritative Base token changed unexpectedly")
                configured_id = str(master.get("people_table_id") or "").strip()
                if configured_id and configured_id != table_id:
                    raise ValueError("authoritative People table changed unexpectedly")
                records = cli.list_base_records(
                    base_token=base_token,
                    table_id=table_id,
                    identity="user",
                    # A view is never a complete authoritative roster snapshot.
                    view_id=None,
                    field_names=None,
                )
                authoritative_raw_records = records
                raw_batches.append((authoritative_source_ref, records))
                batch_metadata[authoritative_source_ref] = {
                    "source_complete": True,
                    "page_cursor": None,
                }
                source_base_bindings[authoritative_source_ref] = (base_token, table_id)
                master["base_token"] = base_token
                master["people_table_id"] = table_id
                tables_by_name = {
                    str(item.get("name") or ""): str(
                        item.get("table_id") or item.get("id") or ""
                    )
                    for item in schema.get("tables") or []
                }
                sources_name = str(master.get("sources_table_name") or "").strip()
                if not master.get("sources_table_id") and sources_name:
                    master["sources_table_id"] = tables_by_name.get(sources_name) or None
                master["authoritative_roster"] = True
                master["authoritative_source_ref"] = authoritative_source_ref
            else:
                bridge_actions.append(
                    {
                        "source_ref": authoritative_source_ref,
                        "operation": "read_authoritative_people_roster",
                        "kind": "feishu_base",
                        "url": master["url"],
                        "configured_table": configured_table,
                        "identity": "user",
                        "read_only": True,
                        "view_id": None,
                        "result_requirements": {
                            "source_complete": "must be true after every page was read",
                            "page_cursor": "must be absent",
                            "base_token": "actual Base token",
                            "table_id": "actual People table ID",
                            "schema_fields": "complete live People field schema",
                            "record_id": "preserve record_id on every row",
                            "locator_proof": (
                                "for a Wiki/indirect URL, echo requested_url, resolved "
                                "object_type/base_token and selected_table id/name"
                            ),
                            "selected_table": (
                                "when the configured locator has no opaque table ID, "
                                "return the actual table_id and display name"
                            ),
                            "master_pre_read": (
                                "when a Sources table exists, also return one complete top-level "
                                "master_pre_read snapshot with base_token, table_ids, schema_fields, "
                                "and records so later updates bind to actual live before-values"
                            ),
                        },
                    }
                )
        if bridge_actions:
            request = _new_bridge_request(
                paths,
                purpose="source_sync",
                expected_refs=[str(action["source_ref"]) for action in bridge_actions],
                request_content={"actions": bridge_actions, "config": config},
            )
            return {
                "apply": False,
                "adapter": "agent_tool_bridge",
                "actions": _bridge_actions(bridge_actions, request),
                "bridge_request": _bridge_public_fields(request),
                "local_batches": len(raw_batches),
                "authoritative_roster_pending": authoritative_source_ref in {
                    str(action["source_ref"]) for action in bridge_actions
                },
            }
    canonical_batches: list[tuple[str, list[dict[str, Any]]]] = []
    for ref, records in raw_batches:
        normalized = canonical_records(records, config["field_mapping"])
        canonical_batches.append((ref, normalized))
        if ref in authoritative_refs:
            roster_record_links[ref] = _authoritative_record_links(records, normalized)
    incremental: list[dict[str, Any]] = []
    preview_state = PortableState(paths.database)
    try:
        for ref, records in canonical_batches:
            metadata = batch_metadata.get(
                ref, {"source_complete": False, "page_cursor": "unknown"}
            )
            previous = []
            for stored in preview_state.source_roster_records(ref, include_removed=True):
                item = dict(stored["canonical_record"])
                item["lifecycle_status"] = (
                    "removed" if stored["status"] == "removed" else item.get("lifecycle_status", "active")
                )
                previous.append(item)
            if not metadata["source_complete"]:
                current_ids = {str(item["source_record_id"]) for item in records}
                previous = [
                    item for item in previous if str(item.get("source_record_id")) in current_ids
                ]
            delta = reconcile_incremental_records(previous, records)
            incremental.append(
                {
                    "source_ref": ref,
                    "source_complete": bool(metadata["source_complete"]),
                    "page_cursor": metadata["page_cursor"],
                    "counts": delta["counts"],
                    "changed_record_ids": delta["changed_record_ids"],
                    "removals_evaluated": bool(metadata["source_complete"]),
                }
            )
    finally:
        preview_state.close()
    preview = {
        "input_records": sum(len(records) for _, records in canonical_batches),
        "with_required_profile": sum(
            1 for _, records in canonical_batches for record in records if record["canonical_name"] and record["urls"]
        ),
        "missing_required_profile": sum(
            1 for _, records in canonical_batches for record in records if record["canonical_name"] and not record["urls"]
        ),
        "sources": [{"source_ref": ref, "records": len(records)} for ref, records in canonical_batches],
        "incremental": incremental,
    }
    if not args.apply:
        preview_master = config.get("master_database") or {}
        if preview_master.get("mode") == "create_base":
            preview["master_database"] = (
                create_master_base(
                    cli,
                    timezone_name=config["schedule"]["timezone"],
                    folder_token=preview_master.get("folder_token"),
                    apply=False,
                )
                if cli
                else {
                    "adapter": "agent_tool_bridge",
                    "operation": "create_master_base",
                    "schema": master_schema(),
                }
            )
        return {"apply": False, "preview": preview, "mutated": False}
    state = PortableState(paths.database)
    try:
        if master is not None:
            master["authoritative_roster"] = True
            master["authoritative_source_ref"] = authoritative_source_ref
            if config.get("master_database") != master:
                config["master_database"] = master
                config["updated_at"] = utc_now()
                validate_config(config, require_complete=True)
                secure_write_json(paths.config_file, config)
        imports: list[dict[str, Any]] = []
        for ref, records in canonical_batches:
            metadata = batch_metadata.get(
                ref, {"source_complete": False, "page_cursor": "unknown"}
            )
            authoritative = ref in authoritative_refs
            if authoritative and (
                metadata.get("source_complete") is not True
                or metadata.get("page_cursor")
            ):
                raise ValueError("authoritative roster snapshot is incomplete")
            if authoritative and ref not in source_base_bindings:
                raise ValueError("authoritative roster has no verified Base/table binding")
            result = _reconcile_roster_batch(
                state,
                ref,
                records,
                source_complete=bool(metadata.get("source_complete")),
                page_cursor=metadata.get("page_cursor"),
                authoritative=authoritative,
            )
            if authoritative:
                base_token, table_id = source_base_bindings[ref]
                _link_authoritative_roster_rows(
                    state,
                    result=result,
                    record_links=roster_record_links[ref],
                    base_token=base_token,
                    table_id=table_id,
                )
            imports.append(result)
        if master is not None and authoritative_source_ref and live_master_records:
            bound_base, bound_people_table = source_base_bindings[
                authoritative_source_ref
            ]
            _store_master_live_pre_read(
                state,
                base_token=bound_base,
                people_table_id=bound_people_table,
                sources_table_id=(
                    str(master.get("sources_table_id"))
                    if master.get("sources_table_id")
                    else None
                ),
                records=live_master_records,
            )
        validated_source_bridge: dict[str, Any] | None = None
        if source_bridge_payload is not None:
            validated_source_bridge = _validate_bridge_result(
                paths,
                source_bridge_payload,
                purpose="source_sync",
                completed_refs=source_bridge_completed_refs,
                state=state,
                consume=True,
            )
        scan_source_ids = sorted(
            {
                source_id
                for result in imports
                for source_id in result.get("scan_source_ids", [])
            }
        )
        scholar_run_limit = int(
            (((config.get("scan_policy") or {}).get("scholar") or {}).get(
                "max_requests_per_run", 8
            ))
        )
        staggered_scholar_source_ids = state.tracker.stagger_new_scholar_sources(
            scan_source_ids,
            immediate_limit=scholar_run_limit,
        )
        scan_source_ids = sorted(set(scan_source_ids) - set(staggered_scholar_source_ids))
        source_routes = _sync_source_routes(
            state,
            paths,
            list(config.get("source_routes") or []),
            apply=True,
        )
        if source_bridge_payload is not None:
            _write_scheduler_roster_checkpoint(
                state,
                config=config,
                imports=imports,
                scan_source_ids=scan_source_ids,
                bridge_request=validated_source_bridge,
            )
        master_result: dict[str, Any] | None = None
        master = dict(config.get("master_database") or {})
        if master.get("mode") == "create_base" and not master.get("base_token"):
            if cli:
                created = create_master_base(
                    cli,
                    timezone_name=config["schedule"]["timezone"],
                    folder_token=master.get("folder_token"),
                    apply=True,
                )
                master.update(
                    {
                        "mode": "existing_base",
                        "base_token": created["base_token"],
                        "url": created.get("base_url"),
                        "people_table_id": created["table_ids"]["People"],
                        "sources_table_id": created["table_ids"]["Sources"],
                        "authoritative_roster": True,
                        "created_at": utc_now(),
                    }
                )
                master["authoritative_source_ref"] = authoritative_roster_source_ref(master)
                config["master_database"] = master
                config["updated_at"] = utc_now()
                validate_config(config, require_complete=True)
                secure_write_json(paths.config_file, config)
                master_result = created
            else:
                bridge_root = paths.state_root / "bridge"
                bridge_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                bridge_file = bridge_root / "master-sync.json"
                records = _bridge_safe_master_records(
                    state,
                    visible_records(state),
                    authoritative_roster=False,
                    base_token="",
                    people_table_id="People",
                    sources_table_id="Sources",
                )
                request = _new_bridge_request(
                    paths,
                    purpose="master_sync",
                    expected_refs=_master_expected_refs(records),
                    request_content={
                        "operation": "create_and_sync_master_base",
                        "master_database": master,
                        "schema": master_schema(),
                        "records": records,
                        "write_protocol": "machine-fields-verified-v1",
                        "batch_policy": "preflight every row before any write; abort the whole batch on mismatch",
                        "result_contract": _master_bridge_result_contract(
                            include_sources=True
                        ),
                    },
                    state=state,
                )
                secure_write_json(
                    bridge_file,
                    {
                        "schema": master_schema(),
                        "records": records,
                        "write_protocol": "machine-fields-verified-v1",
                        "batch_policy": "preflight every row before any write; abort the whole batch on mismatch",
                        "result_contract": _master_bridge_result_contract(
                            include_sources=True
                        ),
                        **_bridge_public_fields(request),
                        "synthetic": False,
                        "contains_secrets": False,
                    },
                )
                master_result = {
                    "adapter": "agent_tool_bridge",
                    "operation": "create_and_sync_master_base",
                    "payload_file": str(bridge_file),
                    **_bridge_public_fields(request),
                    "next": "submit --master-bridge-results with actual Base/table/record IDs",
                }
        if master.get("mode") == "existing_base":
            if cli and not master.get("base_token") and master.get("url"):
                selected_schema = cli.base_schema(
                    str(master["url"]),
                    configured_table=(
                        master.get("people_table_id")
                        or master.get("people_table_name")
                        or master.get("table_id")
                        or master.get("table_name")
                    ),
                )
                selected_table = selected_schema.get("selected_table") or {}
                selected_id = str(
                    selected_table.get("table_id")
                    or selected_table.get("id")
                    or selected_table.get("name")
                    or ""
                )
                if not selected_id:
                    raise ValueError("unable to resolve the existing People table")
                master["base_token"] = selected_schema["base_token"]
                master["people_table_id"] = selected_id
            master["authoritative_roster"] = True
            master["authoritative_source_ref"] = authoritative_roster_source_ref(master)
            if config.get("master_database") != master:
                config["master_database"] = master
                config["updated_at"] = utc_now()
                secure_write_json(paths.config_file, config)
            if master.get("base_token") and cli:
                people_table = master.get("people_table_id") or master.get("people_table_name") or "People"
                sources_table = master.get("sources_table_id") or master.get("sources_table_name")
                available_fields = _live_master_fields(
                    cli,
                    base_token=str(master["base_token"]),
                    people_table_id=str(people_table),
                    sources_table_id=str(sources_table) if sources_table else None,
                )
                existing_records = None
                if authoritative_raw_records is not None:
                    existing_records = {
                        "People": _mask_authoritative_review_identity(
                            state,
                            authoritative_raw_records,
                            base_token=str(master["base_token"]),
                            table_id=str(people_table),
                            available_fields=available_fields["People"],
                            field_mapping=config.get("field_mapping") or {},
                        )
                    }
                master_result = sync_visible_master(
                    state,
                    cli,
                    base_token=str(master["base_token"]),
                    people_table_id=str(people_table),
                    sources_table_id=str(sources_table) if sources_table else None,
                    apply=True,
                    field_mapping={"People": config.get("field_mapping") or {}},
                    existing_records=existing_records,
                    available_fields=available_fields,
                    authoritative_roster=True,
                )
            elif cli is None:
                bridge_root = paths.state_root / "bridge"
                bridge_root.mkdir(parents=True, exist_ok=True, mode=0o700)
                bridge_file = bridge_root / "master-sync.json"
                records = _bridge_safe_master_records(
                    state,
                    visible_records(state),
                    authoritative_roster=True,
                    base_token=str(master.get("base_token") or ""),
                    people_table_id=str(
                        master.get("people_table_id")
                        or master.get("people_table_name")
                        or ""
                    ),
                    sources_table_id=(
                        str(master.get("sources_table_id") or master.get("sources_table_name"))
                        if master.get("sources_table_id") or master.get("sources_table_name")
                        else None
                    ),
                    authoritative_source_ref=authoritative_source_ref,
                    live_records=live_master_records,
                    field_mapping={"People": config.get("field_mapping") or {}},
                )
                include_sources = bool(
                    master.get("sources_table_id") or master.get("sources_table_name")
                )
                request = _new_bridge_request(
                    paths,
                    purpose="master_sync",
                    expected_refs=_master_expected_refs(
                        records, include_sources=include_sources
                    ),
                    request_content={
                        "operation": "sync_existing_master_base",
                        "master_database": master,
                        "schema": master_schema(),
                        "records": {
                            "People": records["People"],
                            "Sources": records["Sources"] if include_sources else [],
                        },
                        "field_mapping": {"People": config.get("field_mapping") or {}},
                        "write_protocol": "machine-fields-verified-v1",
                        "batch_policy": "preflight every row before any write; abort the whole batch on mismatch",
                        "write_policy": "read-before-write; preserve human fields; no row deletion",
                        "authoritative_roster": True,
                        "result_contract": _master_bridge_result_contract(
                            include_sources=include_sources
                        ),
                    },
                    state=state,
                )
                secure_write_json(
                    bridge_file,
                    {
                        "master_database": master,
                        "schema": master_schema(),
                        "records": {
                            "People": records["People"],
                            "Sources": records["Sources"] if include_sources else [],
                        },
                        "field_mapping": {"People": config.get("field_mapping") or {}},
                        "write_protocol": "machine-fields-verified-v1",
                        "batch_policy": "preflight every row before any write; abort the whole batch on mismatch",
                        "required_readback": [
                            "existing records with record_id and fields",
                            "actual available field names",
                        ],
                        "result_contract": _master_bridge_result_contract(
                            include_sources=include_sources
                        ),
                        "write_policy": "read-before-write; preserve human fields; no row deletion",
                        "authoritative_roster": True,
                        **_bridge_public_fields(request),
                        "synthetic": False,
                        "contains_secrets": False,
                    },
                )
                master_result = {
                    "adapter": "agent_tool_bridge",
                    "operation": "sync_existing_master_base",
                    "payload_file": str(bridge_file),
                    **_bridge_public_fields(request),
                    "next": "submit --master-bridge-results with actual Base/table IDs",
                }
        status = state.status()
    finally:
        state.close()
    return {
        "apply": True,
        "preview": preview,
        "imports": imports,
        "scan_source_ids": scan_source_ids,
        "staggered_scholar_source_ids": staggered_scholar_source_ids,
        "source_routes": source_routes,
        "master_database": master_result,
        "state": status,
    }


def _sync_source_routes(
    state: PortableState,
    paths: RuntimePaths,
    routes: list[dict[str, Any]],
    *,
    apply: bool,
) -> dict[str, Any]:
    preview = state.sync_source_routes(routes, apply=False)
    if not apply:
        return preview
    if preview["counts"]["blocked"]:
        return state.sync_source_routes(routes, apply=True)
    ready = sum(item.get("status") == "ready" for item in preview["routes"])
    backup = None
    if ready:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = state.backup_database(
            paths.state_root / "backups" / f"source-routes-{stamp}.sqlite3"
        )
    result = state.sync_source_routes(routes, apply=True)
    result["backup"] = backup
    return result


def command_source_routes(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    """Safely preview or apply exact-URL public retrieval route configuration."""

    config = load_config(paths)
    if args.apply and config.get("state") not in {"validated", "enabled"}:
        raise ValueError("source route apply requires validated or enabled configuration")
    state = PortableState(paths.database)
    try:
        return _sync_source_routes(
            state,
            paths,
            list(config.get("source_routes") or []),
            apply=args.apply,
        )
    finally:
        state.close()


def _bootstrap_state(paths: RuntimePaths) -> dict[str, Any]:
    state = PortableState(paths.database)
    try:
        return state.get_meta("bootstrap", {}) or {}
    finally:
        state.close()


def _set_bootstrap_state(paths: RuntimePaths, value: dict[str, Any]) -> None:
    state = PortableState(paths.database)
    try:
        state.set_meta("bootstrap", value)
    finally:
        state.close()


def _anchor_discovery_action(
    paths: RuntimePaths,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    state = PortableState(paths.database)
    try:
        batch_size = int((config.get("intake") or {}).get("discovery_batch_size", 50))
        issues = state.open_anchor_issues(limit=batch_size)
        if not issues:
            return None
        bridge_root = paths.state_root / "bridge"
        bridge_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload_file = bridge_root / "anchor-discovery.json"
        secure_write_json(
            payload_file,
            {
                "schema_version": "people-tracking-anchor-discovery-v1",
                "people": issues,
                "allowed_profile_kinds": ["homepage", "scholar", "github", "linkedin"],
                "rules": {
                    "use_name_secondary_id_alias_school_and_research_context": True,
                    "accept_confirmed_github_nickname_relationship": True,
                    "do_not_require_display_name_exact_match": True,
                    "return_original_profile_and_evidence_urls": True,
                    "do_not_guess_when_identity_is_ambiguous": True,
                },
                "result_schema": {
                    "all_reviewed": True,
                    "resolutions": [
                        {
                            "issue_id": "iss_...",
                            "canonical_name": "same name or confirmed alias",
                            "urls": ["https://profile.example/person"],
                            "evidence_urls": ["https://profile.example/person"],
                            "evidence_notes": "compact identity evidence",
                        }
                    ],
                    "unresolved": [{"issue_id": "iss_...", "reason": "ambiguous"}],
                },
            },
        )
        return {
            "adapter": "agent_tool_bridge",
            "operation": "discover_missing_profile_anchors",
            "payload_file": str(payload_file),
            "people_in_batch": len(issues),
            "read_only_web_research": True,
            "next": "write the result schema to a 0600 JSON file and resume bootstrap with --anchor-bridge-input",
        }
    finally:
        state.close()


def _bootstrap_sync_args(
    args: argparse.Namespace,
    *,
    bridge_input: Path | None = None,
    master_bridge_results: Path | None = None,
    include_local_sources: bool = True,
) -> argparse.Namespace:
    return argparse.Namespace(
        lark_cli=args.lark_cli,
        bridge_input=bridge_input,
        master_bridge_results=master_bridge_results,
        apply=True,
        allow_validated=True,
        include_local_sources=include_local_sources,
    )


def command_bootstrap(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    """Resume-safe first import through baseline and scheduler registration."""

    if args.confirmation != ENABLE_PHRASE:
        raise ValueError(f"bootstrap requires --confirmation {ENABLE_PHRASE}")
    config = load_config(paths)
    if config.get("state") not in {"validated", "enabled"}:
        raise ValueError("bootstrap requires a validated onboarding configuration")
    runtime_mode = _runtime_mode(config)
    if runtime_mode == "claude_lark_cli" and not args.skip_schedule:
        # A raw launchd/systemd process cannot execute bridge actions or make the
        # host-Agent review decisions required before anything is published.
        # Require an explicit acknowledgement instead of silently installing a
        # scheduler that can only stall (or, worse, appear healthy).
        return {
            "status": "waiting_for_interactive_only_confirmation",
            "runtime_mode": runtime_mode,
            "scheduler": {
                "status": "unsupported",
                "reason": "isolated_host_agent_required",
            },
            "public_delivery_blocked": True,
            "next": (
                "claude_lark_cli supports interactive Skill runs only; rerun this "
                "bootstrap command with --skip-schedule to acknowledge that no "
                "unattended schedule will be registered, or use OpenClaw isolated "
                "host-Agent automation"
            ),
        }
    progress = _bootstrap_state(paths)
    progress.setdefault("started_at", utc_now())
    progress["status"] = "running"
    progress["updated_at"] = utc_now()
    _set_bootstrap_state(paths, progress)

    if not progress.get("source_import_completed"):
        sync_result = command_sync(
            _bootstrap_sync_args(args, bridge_input=args.source_bridge_input),
            paths,
        )
        if sync_result.get("actions"):
            progress.update({"status": "waiting_for_source_bridge", "updated_at": utc_now()})
            _set_bootstrap_state(paths, progress)
            return {
                "status": progress["status"],
                "actions": sync_result["actions"],
                "next": "execute every read_people_source action and resume bootstrap with --source-bridge-input",
            }
        progress.update(
            {
                "source_import_completed": True,
                "source_import": sync_result.get("preview"),
                "updated_at": utc_now(),
            }
        )
        _set_bootstrap_state(paths, progress)

    intake = config.get("intake") or {}
    if intake.get("missing_anchor_policy", "agent_discovery") == "agent_discovery":
        if args.anchor_bridge_input:
            payload = _json_file(args.anchor_bridge_input)
            state = PortableState(paths.database)
            try:
                progress["anchor_resolution"] = state.apply_anchor_resolutions(
                    payload,
                    minimum_evidence_links=int(intake.get("minimum_evidence_links", 1)),
                )
            finally:
                state.close()
            progress["updated_at"] = utc_now()
            _set_bootstrap_state(paths, progress)
        anchor_action = _anchor_discovery_action(paths, config)
        if anchor_action:
            progress.update({"status": "waiting_for_anchor_discovery", "updated_at": utc_now()})
            _set_bootstrap_state(paths, progress)
            return {
                "status": progress["status"],
                "actions": [anchor_action],
                "next": "research this bounded batch, return evidence-backed results, and resume the same bootstrap command",
            }
    progress["anchor_discovery_completed"] = True
    _set_bootstrap_state(paths, progress)

    if not progress.get("master_database_completed"):
        if args.master_bridge_results:
            master_result = command_sync(
                _bootstrap_sync_args(args, master_bridge_results=args.master_bridge_results),
                paths,
            )
            progress["master_database"] = master_result
            progress["master_database_completed"] = True
            progress["updated_at"] = utc_now()
            _set_bootstrap_state(paths, progress)
        else:
            bridge_root = paths.state_root / "bridge"
            bridge_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            empty_sources = bridge_root / "empty-source-sync.json"
            secure_write_json(empty_sources, {"sources": []})
            master_sync = command_sync(
                _bootstrap_sync_args(
                    args,
                    bridge_input=empty_sources,
                    include_local_sources=False,
                ),
                paths,
            )
            master_result = master_sync.get("master_database")
            if isinstance(master_result, dict) and master_result.get("adapter") == "agent_tool_bridge":
                progress.update(
                    {
                        "status": "waiting_for_master_bridge",
                        "master_action": master_result,
                        "updated_at": utc_now(),
                    }
                )
                _set_bootstrap_state(paths, progress)
                return {
                    "status": progress["status"],
                    "actions": [master_result],
                    "next": "create or sync the visible Base and resume bootstrap with --master-bridge-results",
                }
            progress["master_database"] = master_result
            progress["master_database_completed"] = True
            progress["updated_at"] = utc_now()
            _set_bootstrap_state(paths, progress)

    config = load_config(paths)
    if config.get("state") != "enabled":
        config = mark_state(paths, config, "enabled")
    if not progress.get("baseline_completed"):
        state = PortableState(paths.database)
        try:
            baseline = scan_due(
                state,
                paths,
                config,
                # Respect per-host budgets and the stable Scholar phase assigned
                # during roster reconciliation.  Every due non-Scholar source is
                # still fetched now, while a large Scholar roster establishes its
                # baselines gradually instead of defeating the anti-429 policy.
                force_all=False,
                force_full_fetch=True,
            )
            progress["baseline"] = {
                "tracker_run_id": baseline["tracker_run_id"],
                "metrics": baseline["metrics"],
                "validation": baseline["validation"],
            }
            progress["baseline_completed"] = True
            progress["baseline_degraded"] = not baseline["validation"]["passed"]
            progress["updated_at"] = utc_now()
            _set_bootstrap_state(paths, progress)
        finally:
            state.close()

    if not args.skip_schedule and not progress.get("scheduler_registered"):
        scheduler = _install_configured_scheduler(config, args)
        progress["scheduler"] = scheduler
        progress["scheduler_registered"] = True
    elif args.skip_schedule:
        progress["scheduler"] = {
            "status": "not_registered",
            "reason": "explicit_skip_schedule",
            "runtime_mode": runtime_mode,
        }
    state = PortableState(paths.database)
    try:
        final_state = state.status()
    finally:
        state.close()
    progress.update({"status": "ready", "completed_at": utc_now(), "updated_at": utc_now()})
    _set_bootstrap_state(paths, progress)
    return {
        "status": "ready",
        "configuration_state": "enabled",
        "database": final_state,
        "baseline": progress.get("baseline"),
        "baseline_degraded": bool(progress.get("baseline_degraded")),
        "scheduler": progress.get("scheduler"),
        "pending_manual_intake": final_state["counts"]["open_intake_issues"],
        "next": (
            (
                "no unattended schedule is active; failed sources retry automatically "
                "during each manual Skill invocation and stay in the developer report"
                if progress.get("baseline_degraded")
                else "no unattended schedule is active; invoke the Skill interactively for future scans and digests"
            )
            if args.skip_schedule
            else (
                "isolated OpenClaw host-Agent automation is active; healthy changes remain "
                "reportable while failed sources retry and appear only in the developer report"
                if progress.get("baseline_degraded")
                else "isolated OpenClaw host-Agent automation is active; future turns run scans, reviews, and dated digests"
            )
        ),
    }


def command_scan(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    state = PortableState(paths.database)
    try:
        return scan_due(
            state,
            paths,
            config,
            force_all=bool(getattr(args, "force_all", False)),
            force_full_fetch=bool(getattr(args, "force_full_fetch", False)),
            source_kinds=getattr(args, "source_kind", None),
            source_ids=getattr(args, "source_id", None),
            max_error_rate=float(getattr(args, "max_error_rate", 0.10)),
            homepage_retries=int(getattr(args, "homepage_retries", 1)),
            homepage_backoff_seconds=float(
                getattr(args, "homepage_backoff_seconds", 1.0)
            ),
        )
    finally:
        state.close()


def command_digest(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    state = PortableState(paths.database)
    try:
        if args.bridge_results:
            payload = _json_file(args.bridge_results)
            key = str(payload.get("delivery_key") or "")
            delivery = state.delivery(key) if key else None
            if not delivery:
                raise ValueError("bridge result delivery_key is unknown")
            if payload.get("doc_url"):
                state.update_delivery(
                    key,
                    doc_url=markdown_http_url(payload["doc_url"]),
                    doc_token=str(payload.get("doc_token") or ""),
                )
            if payload.get("message_id"):
                state.update_delivery(key, message_id=str(payload["message_id"]))
            delivery = state.delivery(key) or delivery
            targets = _audience_output_config(config, str(delivery.get("audience") or "public"))
            document_done = not (targets.get("document") or {}).get("enabled") or bool(
                delivery.get("doc_url")
            )
            message_done = not (targets.get("message") or {}).get("enabled") or bool(
                delivery.get("message_id")
            )
            if document_done and message_done:
                state.update_delivery(key, status="completed")
            return {"bridge_result_applied": True, "delivery": state.delivery(key)}
        mode = _runtime_mode(config)
        cli = _lark(args.lark_cli, strict=True) if mode == "claude_lark_cli" else None
        audience = str(getattr(args, "audience", "both"))
        results: dict[str, Any] = {}
        if audience in {"public", "both"}:
            results["public"] = deliver_digest(
                state,
                paths,
                config,
                output_kind=args.kind,
                tracker_run_id=args.tracker_run_id,
                apply=args.apply,
                lark=cli,
            )
        if audience in {"developer", "both"}:
            results["developer"] = deliver_developer_digest(
                state,
                paths,
                config,
                output_kind=args.kind,
                apply=args.apply,
                lark=cli,
            )
        return {"audience": audience, "reports": results}
    finally:
        state.close()


def command_review_export(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    if config.get("state") != "enabled":
        raise ValueError("agent review export requires enabled tracking")
    lookback_days = max(1, min(366, int(args.lookback_days)))
    state = PortableState(paths.database)
    try:
        return export_agent_review_requests(
            state,
            args.output,
            since=datetime.now(timezone.utc) - timedelta(days=lookback_days),
            batch_size=int((config.get("agent_review") or {}).get("batch_size", 100)),
        )
    finally:
        state.close()


def command_review_apply(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    if config.get("state") != "enabled":
        raise ValueError("agent review apply requires enabled tracking")
    payload = _json_file(args.input)
    state = PortableState(paths.database)
    try:
        return apply_agent_review_results(state, payload)
    finally:
        state.close()


def command_sandbox(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    if args.apply and args.confirmation != SANDBOX_PHRASE:
        raise ValueError(f"sandbox apply requires --confirmation {SANDBOX_PHRASE}")
    if args.apply and config.get("state") != "enabled":
        raise ValueError("sandbox apply requires enabled configuration")
    sandbox_root = paths.state_root / "sandbox"
    sandbox_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    sandbox_db = sandbox_root / f"sandbox-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
    algorithm = algorithm_e2e(sandbox_db)
    mode = _runtime_mode(config)
    cli = _lark(args.lark_cli, strict=True) if mode == "claude_lark_cli" else None
    feishu = feishu_e2e(
        lark=cli,
        algorithm=algorithm,
        apply=args.apply,
        message_target=(_audience_output_config(config, "public").get("message") or {}),
    )
    return {
        "synthetic_only": True,
        "algorithm": algorithm,
        "feishu": feishu,
        "sandbox_database": str(sandbox_db),
        "production_database_touched": False,
    }


def _register_openclaw_scheduler(
    *,
    launcher: Path,
    timezone_name: str,
    apply: bool,
) -> dict[str, Any]:
    executable = shutil.which("openclaw")
    if not executable:
        raise RuntimeError("OpenClaw runtime selected but openclaw CLI is unavailable")
    listed = subprocess.run(
        [executable, "automations", "list", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if listed.returncode != 0:
        raise RuntimeError("unable to inspect existing OpenClaw automations")
    if "people-tracking-feishu-" in listed.stdout:
        raise RuntimeError(
            "a people-tracking-feishu- automation already exists; refusing to overwrite"
        )
    plans = openclaw_cron_plan(launcher, timezone_name)
    if apply:
        for plan in plans:
            argv = [executable, *plan["create_argv"][1:]]
            result = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
            if result.returncode != 0:
                raise RuntimeError("OpenClaw automation registration failed")
    return {"apply": apply, "jobs": plans}


# The scheduler cadence is 15 minutes.  A shorter lease lets an immediately
# resumed bridge workflow continue, while making every later cron tick obtain a
# fresh authoritative Base snapshot before it can publish.
_ROSTER_CHECKPOINT_MAX_AGE = timedelta(minutes=10)


def _roster_checkpoint_config_hash(config: dict[str, Any]) -> str:
    master = _authoritative_master(config)
    roster_sources = [
        {
            key: source.get(key)
            for key in (
                "source_ref",
                "kind",
                "url",
                "path",
                "table_id",
                "table_name",
                "view_id",
                "view_name",
            )
        }
        for source in config.get("sources") or []
    ]
    master_locator = {
        key: (master or {}).get(key)
        for key in (
            "mode",
            "base_token",
            "people_table_id",
            "people_table_name",
            "table_id",
            "table_name",
            "authoritative_roster",
            "authoritative_source_ref",
        )
    }
    # View selection is intentionally excluded from authoritative reads.  A
    # verified bridge may normalize the stored URL by removing ``?view=...``;
    # that must not invalidate the checkpoint for the workflow it is resuming.
    master_url = str((master or {}).get("url") or "")
    master_locator["url"] = _base_locator(master_url)
    master_query = dict(parse_qsl(urlparse(master_url).query))
    master_locator["url_table"] = (
        None
        if (master or {}).get("people_table_id")
        else master_query.get("table") or master_query.get("table_id")
    )
    return _bridge_contract_hash(
        {
            "sources": roster_sources,
            "field_mapping": config.get("field_mapping") or {},
            "intake": config.get("intake") or {},
            "authoritative_source_ref": (
                authoritative_roster_source_ref(master) if master else None
            ),
            "master": master_locator,
        }
    )


def _write_scheduler_roster_checkpoint(
    state: PortableState,
    *,
    config: dict[str, Any],
    imports: list[dict[str, Any]],
    scan_source_ids: list[str],
    bridge_request: dict[str, Any] | None,
) -> None:
    if any(
        item.get("source_complete") is not True
        or item.get("removals_evaluated") is not True
        for item in imports
    ):
        return
    master = _authoritative_master(config)
    authoritative_ref = authoritative_roster_source_ref(master) if master else None
    authoritative_import = next(
        (
            item
            for item in imports
            if authoritative_ref and item.get("source_ref") == authoritative_ref
        ),
        None,
    )
    if authoritative_ref and not authoritative_import:
        return
    if (
        not isinstance(bridge_request, dict)
        or bridge_request.get("purpose") != "source_sync"
        or bridge_request.get("status") != "consumed"
        or not bridge_request.get("bridge_nonce")
        or not bridge_request.get("request_hash")
    ):
        return
    state.set_meta(
        "scheduler_roster_checkpoint",
        {
            "status": "ready",
            "completed_at": utc_now(),
            "config_hash": _roster_checkpoint_config_hash(config),
            "all_sources_complete": True,
            "authoritative_import": authoritative_import,
            "imports": imports,
            "scan_source_ids": sorted(set(scan_source_ids)),
            "workflow_id": "roster_"
            + stable_hash(str(bridge_request["bridge_nonce"]), 32),
            "bridge_nonce": bridge_request["bridge_nonce"],
            "bridge_request_hash": bridge_request["request_hash"],
        },
    )


def _queue_scheduled_authoritative_roster_refresh(
    paths: RuntimePaths,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Queue the lightweight authoritative Base read for a non-scan tick.

    OpenClaw cannot read Feishu Base from this local process, so the isolated
    host Agent completes this read through the same strict ``source_sync``
    bridge used by ``command_sync``.  Keeping this path separate from
    ``scan_due`` lets every 15-minute tick reconcile membership without
    turning every tick into a homepage crawl.
    """

    master = _authoritative_master(config)
    if master is None:
        return None
    source_ref = authoritative_roster_source_ref(master)
    configured_table = (
        master.get("people_table_id")
        or master.get("people_table_name")
        or master.get("table_id")
        or master.get("table_name")
    )
    action = {
        "source_ref": source_ref,
        "operation": "read_authoritative_people_roster",
        "kind": "feishu_base",
        "url": master["url"],
        "configured_table": configured_table,
        "identity": "user",
        "read_only": True,
        "view_id": None,
        "result_requirements": {
            "source_complete": "must be true after every page was read",
            "page_cursor": "must be absent",
            "base_token": "actual Base token",
            "table_id": "actual People table ID",
            "schema_fields": "complete live People field schema",
            "record_id": "preserve record_id on every row",
            "locator_proof": (
                "for a Wiki/indirect URL, echo requested_url, resolved "
                "object_type/base_token and selected_table id/name"
            ),
            "selected_table": (
                "when the configured locator has no opaque table ID, return the "
                "actual table_id and display name"
            ),
            "master_pre_read": (
                "when a Sources table exists, also return one complete top-level "
                "master_pre_read snapshot with base_token, table_ids, schema_fields, "
                "and records so later updates bind to actual live before-values"
            ),
        },
    }
    request = _new_bridge_request(
        paths,
        purpose="source_sync",
        expected_refs=[source_ref],
        request_content={"actions": [action], "config": config},
    )
    return {
        "apply": False,
        "adapter": "agent_tool_bridge",
        "actions": _bridge_actions([action], request),
        "bridge_request": _bridge_public_fields(request),
        "local_batches": 0,
        "authoritative_roster_pending": True,
        "scan_requested": False,
    }


def _usable_scheduler_roster_checkpoint(
    checkpoint: Any,
    *,
    config: dict[str, Any],
    now: datetime,
    last_scan: datetime | None,
    bridge_request: Any,
) -> dict[str, Any] | None:
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("status") != "ready"
        or checkpoint.get("all_sources_complete") is not True
        or checkpoint.get("config_hash") != _roster_checkpoint_config_hash(config)
        or not isinstance(checkpoint.get("scan_source_ids"), list)
        or not isinstance(bridge_request, dict)
        or bridge_request.get("purpose") != "source_sync"
        or bridge_request.get("status") != "consumed"
        or checkpoint.get("bridge_nonce") != bridge_request.get("bridge_nonce")
        or checkpoint.get("bridge_request_hash") != bridge_request.get("request_hash")
        or checkpoint.get("workflow_id")
        != "roster_" + stable_hash(str(bridge_request.get("bridge_nonce") or ""), 32)
    ):
        return None
    try:
        completed = datetime.fromisoformat(
            str(checkpoint.get("completed_at") or "").replace("Z", "+00:00")
        )
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=timezone.utc)
        completed = completed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
    if completed > now + timedelta(minutes=5) or now - completed > _ROSTER_CHECKPOINT_MAX_AGE:
        return None
    # A successful scan happens after its roster read.  If that scan is newer
    # than the read, the checkpoint must explicitly bind the same completion
    # marker; otherwise a leftover pre-scan checkpoint from another workflow
    # could be reused after the scan cadence advanced.
    if last_scan is not None and last_scan > completed:
        try:
            scan_completed = datetime.fromisoformat(
                str(checkpoint.get("scan_completed_at") or "").replace("Z", "+00:00")
            )
            if scan_completed.tzinfo is None:
                scan_completed = scan_completed.replace(tzinfo=timezone.utc)
            scan_completed = scan_completed.astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None
        if scan_completed != last_scan:
            return None
    master = _authoritative_master(config)
    if master is not None:
        authoritative = checkpoint.get("authoritative_import")
        if (
            not isinstance(authoritative, dict)
            or authoritative.get("source_ref")
            != authoritative_roster_source_ref(master)
            or authoritative.get("source_complete") is not True
            or authoritative.get("removals_evaluated") is not True
        ):
            return None
    return checkpoint


def _scan_interval(schedule: dict[str, Any]) -> timedelta:
    cadence = str(schedule.get("scan") or "weekly")
    intervals = {
        "hourly": timedelta(hours=1),
        "daily": timedelta(days=1),
        "weekly": timedelta(days=7),
    }
    try:
        return intervals[cadence]
    except KeyError as exc:
        raise ValueError("schedule.scan must be hourly, daily, or weekly") from exc


def _scheduled_scan_due(
    state: PortableState,
    schedule: dict[str, Any],
    *,
    now: datetime,
) -> tuple[bool, datetime | None]:
    raw = state.get_meta("scheduler_last_scan_at")
    if not raw:
        return True, None
    try:
        last = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        last = last.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return True, None
    if last > now + timedelta(minutes=5):
        return True, last
    return now - last >= _scan_interval(schedule), last


def _sync_or_queue_visible_master(
    state: PortableState,
    paths: RuntimePaths,
    config: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    master = dict(config.get("master_database") or {})
    if master.get("mode") != "existing_base":
        return None
    base_token = str(master.get("base_token") or "")
    people_table = str(master.get("people_table_id") or master.get("people_table_name") or "")
    sources_value = master.get("sources_table_id") or master.get("sources_table_name")
    sources_table = str(sources_value) if sources_value else None
    authoritative_roster = master.get("authoritative_roster", True) is True
    if not base_token or not people_table:
        raise ValueError("visible master sync requires base_token and a People table ID")
    if _runtime_mode(config) == "claude_lark_cli":
        cli = _lark(args.lark_cli, strict=True)
        assert cli is not None
        available_fields = _live_master_fields(
            cli,
            base_token=base_token,
            people_table_id=people_table,
            sources_table_id=sources_table,
        )
        live_people = cli.list_base_records(
            base_token=base_token,
            table_id=people_table,
            identity="user",
            view_id=None,
            field_names=None,
        )
        existing_records = {
            "People": _mask_authoritative_review_identity(
                state,
                live_people,
                base_token=base_token,
                table_id=people_table,
                available_fields=available_fields["People"],
                field_mapping=config.get("field_mapping") or {},
            )
        }
        return sync_visible_master(
            state,
            cli,
            base_token=base_token,
            people_table_id=people_table,
            sources_table_id=sources_table,
            apply=True,
            field_mapping={"People": config.get("field_mapping") or {}},
            existing_records=existing_records,
            available_fields=available_fields,
            authoritative_roster=authoritative_roster,
        )
    live_records = _load_master_live_pre_read(
        state,
        base_token=base_token,
        people_table_id=people_table,
        sources_table_id=sources_table,
    )
    records = _bridge_safe_master_records(
        state,
        visible_records(state),
        authoritative_roster=authoritative_roster,
        base_token=base_token,
        people_table_id=people_table,
        sources_table_id=sources_table,
        authoritative_source_ref=str(master.get("authoritative_source_ref") or "") or None,
        live_records=live_records,
        field_mapping={"People": config.get("field_mapping") or {}},
    )
    if authoritative_roster:
        records["People"] = [
            item
            for item in records["People"]
            if (item.get("field_values") or {}).get("sync_status") != "已移除"
        ]
    include_sources = bool(sources_table)
    request = _new_bridge_request(
        paths,
        purpose="master_sync",
        expected_refs=_master_expected_refs(records, include_sources=include_sources),
        request_content={
            "operation": "sync_existing_master_base",
            "master_database": master,
            "schema": master_schema(),
            "records": {
                "People": records["People"],
                "Sources": records["Sources"] if include_sources else [],
            },
            "field_mapping": {"People": config.get("field_mapping") or {}},
            "authoritative_roster": authoritative_roster,
            "write_protocol": "machine-fields-verified-v1",
            "batch_policy": "preflight every row before any write; abort the whole batch on mismatch",
            "write_policy": "read-before-write; preserve human fields; no row deletion",
            "result_contract": _master_bridge_result_contract(
                include_sources=include_sources
            ),
        },
        state=state,
    )
    bridge_root = paths.state_root / "bridge"
    bridge_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    bridge_file = bridge_root / "scheduled-master-sync.json"
    secure_write_json(
        bridge_file,
        {
            "master_database": master,
            "schema": master_schema(),
            "records": {
                "People": records["People"],
                "Sources": records["Sources"] if include_sources else [],
            },
            "field_mapping": {"People": config.get("field_mapping") or {}},
            "authoritative_roster": authoritative_roster,
            "write_protocol": "machine-fields-verified-v1",
            "batch_policy": "preflight every row before any write; abort the whole batch on mismatch",
            "required_readback": [
                "existing records with record_id and fields",
                "actual available field names",
            ],
            "result_contract": _master_bridge_result_contract(
                include_sources=include_sources
            ),
            **_bridge_public_fields(request),
            "synthetic": False,
            "contains_secrets": False,
        },
    )
    return {
        "adapter": "agent_tool_bridge",
        "operation": "sync_existing_master_base",
        "payload_file": str(bridge_file),
        **_bridge_public_fields(request),
        "next": "sync every expected ref and submit --master-bridge-results",
    }


def command_schedule_tick(args: argparse.Namespace, paths: RuntimePaths) -> dict[str, Any]:
    config = load_config(paths)
    if config.get("state") != "enabled":
        return {"skipped": True, "reason": "configuration is not enabled"}
    if _runtime_mode(config) != "openclaw":
        state = PortableState(paths.database)
        try:
            run_id = state.begin_run("scheduler_agent_required")
            state.complete_run(
                run_id,
                {
                    "audience": "developer",
                    "public_delivery_blocked": True,
                    "reason": (
                        "an unattended local process cannot perform the required "
                        "execution-Agent review"
                    ),
                },
                status="failed",
            )
        finally:
            state.close()
        return {
            "skipped": False,
            "validation_failed": True,
            "agent_turn_required": True,
            "public_delivery_blocked": True,
            "actions": [],
            "next": "run the Skill interactively or register an isolated OpenClaw automation",
        }
    authoritative_master = _authoritative_master(config)
    authoritative_ref = (
        authoritative_roster_source_ref(authoritative_master)
        if authoritative_master is not None
        else None
    )
    now = datetime.now(timezone.utc)
    schedule_state = PortableState(paths.database)
    try:
        scan_is_due, last_successful_scan = _scheduled_scan_due(
            schedule_state,
            config["schedule"],
            now=now,
        )
        pending_source_bridge = schedule_state.get_meta("bridge_request:source_sync")
        pending_master_bridge = schedule_state.get_meta("bridge_request:master_sync")
        roster_checkpoint = schedule_state.get_meta("scheduler_roster_checkpoint")
    finally:
        schedule_state.close()
    bridge_already_pending = (
        isinstance(pending_source_bridge, dict)
        and pending_source_bridge.get("status") == "pending"
    )
    usable_roster_checkpoint = _usable_scheduler_roster_checkpoint(
        roster_checkpoint,
        config=config,
        now=now,
        last_scan=last_successful_scan,
        bridge_request=pending_source_bridge,
    )
    master_bridge_waiting = (
        isinstance(pending_master_bridge, dict)
        and pending_master_bridge.get("status") == "pending"
    )
    actions: list[dict[str, Any]] = []
    changed_source_ids: list[str] = []
    roster_sync_error: dict[str, str] | None = None
    roster_bridge_waiting = bridge_already_pending
    if (
        master_bridge_waiting
        and usable_roster_checkpoint is None
        and not bridge_already_pending
    ):
        state = PortableState(paths.database)
        try:
            run_id = state.begin_run("master_bridge_pending")
            state.complete_run(
                run_id,
                {
                    "audience": "developer",
                    "public_delivery_blocked": True,
                    "reason": (
                        "a pending master bridge must be completed before a new "
                        "roster/scan cycle starts"
                    ),
                },
                status="failed",
            )
        finally:
            state.close()
        return {
            "skipped": False,
            "scan_due": scan_is_due,
            "validation_failed": False,
            "roster_sync_failed": False,
            "master_bridge_waiting": True,
            "public_delivery_blocked": True,
            "reviews_waiting": False,
            "actions": [
                {
                    "operation": "sync_visible_master_after_scan",
                    "status": "waiting_for_master_bridge",
                    "bridge_request": _bridge_public_fields(pending_master_bridge),
                }
            ],
            "next": "complete and apply the pending master bridge, then resume this tick",
        }
    if bridge_already_pending:
        roster_sync_error = {
            "error_type": "RosterBridgePending",
            "message": "roster source bridge is pending; refusing to publish from stale membership",
        }
        actions.append(
            {
                "operation": "reconcile_roster",
                "status": "waiting_for_source_bridge",
                "bridge_request": _bridge_public_fields(pending_source_bridge),
            }
        )
    elif usable_roster_checkpoint is not None:
        changed_source_ids = list(
            usable_roster_checkpoint.get("scan_source_ids") or []
        )
        actions.append(
            {
                "operation": "reconcile_roster",
                "status": "reused_fresh_checkpoint",
                "completed_at": usable_roster_checkpoint.get("completed_at"),
                "authoritative_import": usable_roster_checkpoint.get(
                    "authoritative_import"
                ),
                "scan_source_ids": changed_source_ids,
            }
        )
        if not scan_is_due:
            actions.append(
                {
                    "operation": "scan_cadence",
                    "status": "not_due",
                    "cadence": config["schedule"]["scan"],
                    "last_successful_scan_at": (
                        last_successful_scan.isoformat()
                        if last_successful_scan
                        else None
                    ),
                }
            )
    elif scan_is_due:
        try:
            sync_result = command_sync(
                argparse.Namespace(
                    lark_cli=args.lark_cli,
                    bridge_input=None,
                    master_bridge_results=None,
                    apply=True,
                    allow_validated=False,
                    include_local_sources=True,
                ),
                paths,
            )
            actions.append({"operation": "reconcile_roster", "result": sync_result})
            if sync_result.get("apply") is False or sync_result.get("actions"):
                roster_bridge_waiting = True
                raise RuntimeError(
                    "roster source bridge is pending; refusing to scan stale membership"
                )
            if authoritative_ref:
                if sync_result.get("authoritative_roster_pending"):
                    raise RuntimeError(
                        "authoritative roster bridge is pending; refusing to scan stale membership"
                    )
                authoritative_import = next(
                    (
                        item
                        for item in sync_result.get("imports") or []
                        if item.get("source_ref") == authoritative_ref
                    ),
                    None,
                )
                if not authoritative_import:
                    raise RuntimeError(
                        "authoritative roster was not reconciled; refusing to scan stale membership"
                    )
                if (
                    authoritative_import.get("source_complete") is not True
                    or authoritative_import.get("removals_evaluated") is not True
                ):
                    raise RuntimeError(
                        "authoritative roster snapshot is incomplete; refusing to scan stale membership"
                    )
            changed_source_ids = list(sync_result.get("scan_source_ids") or [])
        except Exception as exc:
            roster_sync_error = {
                "error_type": type(exc).__name__,
                "message": str(exc)[:800],
            }
            actions.append(
                {"operation": "reconcile_roster", "error": roster_sync_error}
            )
    else:
        actions.append(
            {
                "operation": "scan_cadence",
                "status": "not_due",
                "cadence": config["schedule"]["scan"],
                "last_successful_scan_at": (
                    last_successful_scan.isoformat() if last_successful_scan else None
                ),
            }
        )
        try:
            roster_refresh = _queue_scheduled_authoritative_roster_refresh(
                paths,
                config,
            )
            if roster_refresh is not None:
                roster_bridge_waiting = True
                actions.append(
                    {
                        "operation": "reconcile_roster",
                        "status": "waiting_for_source_bridge",
                        "result": roster_refresh,
                    }
                )
        except Exception as exc:
            roster_sync_error = {
                "error_type": type(exc).__name__,
                "message": str(exc)[:800],
            }
            actions.append(
                {"operation": "reconcile_roster", "error": roster_sync_error}
            )

    state = PortableState(paths.database)
    try:
        if roster_sync_error:
            run_id = state.begin_run("roster_sync")
            state.complete_run(
                run_id,
                {
                    **roster_sync_error,
                    "audience": "developer",
                    "authoritative_roster": bool(authoritative_ref),
                    "bridge_waiting": roster_bridge_waiting,
                    "public_delivery_blocked": True,
                },
                status="failed",
            )
        if roster_sync_error:
            # Fail closed before selecting due sources: a stale local roster must
            # never drive either scans or a user-facing digest.
            return {
                "skipped": False,
                "validation_failed": True,
                "roster_sync_failed": True,
                "reviews_waiting": False,
                "roster_bridge_waiting": roster_bridge_waiting,
                "public_delivery_blocked": True,
                "actions": actions,
                "next": (
                    "complete every roster read bridge and rerun schedule-tick"
                    if roster_bridge_waiting
                    else "repair the roster source and rerun schedule-tick"
                ),
            }
        validation_failed = False
        scan_error: dict[str, str] | None = None
        if scan_is_due:
            try:
                result = scan_due(
                    state,
                    paths,
                    config,
                    source_ids=changed_source_ids,
                )
                actions.append({"operation": "scan_due_only", "result": result})
                validation_failed = not result.get("validation", {}).get(
                    "passed", False
                )
                if not validation_failed:
                    state.set_meta("scheduler_last_scan_at", now.isoformat())
                    if usable_roster_checkpoint is not None:
                        usable_roster_checkpoint = {
                            **usable_roster_checkpoint,
                            "scan_completed_at": now.isoformat(),
                        }
                        state.set_meta(
                            "scheduler_roster_checkpoint", usable_roster_checkpoint
                        )
            except Exception as exc:
                scan_error = {
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:800],
                }
                validation_failed = True
                actions.append({"operation": "scan_due_only", "error": scan_error})

        if scan_is_due and not validation_failed:
            try:
                master_sync = _sync_or_queue_visible_master(
                    state,
                    paths,
                    config,
                    args,
                )
                if master_sync is not None:
                    actions.append(
                        {
                            "operation": "sync_visible_master_after_scan",
                            "result": master_sync,
                        }
                    )
                    master_bridge_waiting = bool(
                        isinstance(master_sync, dict)
                        and master_sync.get("adapter") == "agent_tool_bridge"
                    )
            except Exception as exc:
                master_error = {
                    "error_type": type(exc).__name__,
                    "message": str(exc)[:800],
                    "audience": "developer",
                }
                run_id = state.begin_run("visible_master_sync")
                state.complete_run(run_id, master_error, status="failed")
                actions.append(
                    {
                        "operation": "sync_visible_master_after_scan",
                        "error": master_error,
                    }
                )
                master_bridge_waiting = True

        reviews_waiting = False
        delivery_waiting = False
        for kind in ("daily", "weekly"):
            if not _digest_due_now(kind, now, config["schedule"]):
                continue
            prepared_period = _scheduler_period(kind, now, config["schedule"]["timezone"])
            if state.get_meta(f"scheduler_digest_{kind}") == prepared_period:
                continue
            if validation_failed:
                # Validation is a publication gate, not a diagnostics gate.
                # Do not call the public preparation/delivery path here: that
                # would freeze a public report window from evidence that this
                # tick has already rejected.
                developer_result = deliver_developer_digest(
                    state,
                    paths,
                    config,
                    output_kind=kind,
                    apply=True,
                    lark=None,
                )
                developer_complete = (
                    (developer_result.get("delivery") or {}).get("status")
                    == "completed"
                )
                delivery_waiting = delivery_waiting or not developer_complete
                actions.append(
                    {
                        "operation": f"digest_{kind}_developer",
                        "developer": developer_result,
                        "public": {"status": "blocked_by_scan_validation"},
                    }
                )
                continue
            if roster_bridge_waiting:
                # A pending authoritative read blocks only the user-facing
                # digest.  Existing review work may still be surfaced and the
                # developer diagnostics remain independently deliverable.
                review_path = (
                    paths.reports
                    / f"scheduled-{kind}-{prepared_period}-agent-review.json"
                )
                review_bundle = export_agent_review_requests(
                    state,
                    review_path,
                    since=now - timedelta(days=8 if kind == "daily" else 15),
                    through=now,
                    batch_size=int(
                        (config.get("agent_review") or {}).get("batch_size", 100)
                    ),
                )
                reviews_waiting = reviews_waiting or bool(
                    review_bundle.get("requests")
                )
                developer_result = deliver_developer_digest(
                    state,
                    paths,
                    config,
                    output_kind=kind,
                    apply=True,
                    lark=None,
                )
                developer_complete = (
                    (developer_result.get("delivery") or {}).get("status")
                    == "completed"
                )
                delivery_waiting = delivery_waiting or not developer_complete
                actions.append(
                    {
                        "operation": f"digest_{kind}_developer",
                        "developer": developer_result,
                        "public": {"status": "waiting_for_roster_bridge"},
                    }
                )
                continue
            if master_bridge_waiting:
                developer_result = deliver_developer_digest(
                    state,
                    paths,
                    config,
                    output_kind=kind,
                    apply=True,
                    lark=None,
                )
                developer_complete = (
                    (developer_result.get("delivery") or {}).get("status")
                    == "completed"
                )
                delivery_waiting = delivery_waiting or not developer_complete
                actions.append(
                    {
                        "operation": f"digest_{kind}_developer",
                        "developer": developer_result,
                        "public": {"status": "waiting_for_master_bridge"},
                    }
                )
                continue
            review_path = paths.reports / f"scheduled-{kind}-{prepared_period}-agent-review.json"
            review_bundle = export_agent_review_requests(
                state,
                review_path,
                since=now - timedelta(days=8 if kind == "daily" else 15),
                through=now,
                batch_size=int(
                    (config.get("agent_review") or {}).get("batch_size", 100)
                ),
            )
            if review_bundle.get("requests"):
                reviews_waiting = True
                actions.append(
                    {
                        "operation": "execution_agent_review",
                        "kind": kind,
                        "payload_file": str(review_path),
                        "requests": len(review_bundle["requests"]),
                        "decision_authority": "skill_host_agent",
                        "next": (
                            "review every request with the current Agent, run review-apply, "
                            "then rerun schedule-tick; do not call an external model API"
                        ),
                    }
                )
                mode = _runtime_mode(config)
                cli = _lark(args.lark_cli, strict=True) if mode == "claude_lark_cli" else None
                developer_result = deliver_developer_digest(
                    state,
                    paths,
                    config,
                    output_kind=kind,
                    apply=True,
                    lark=cli,
                )
                developer_complete = (
                    (developer_result.get("delivery") or {}).get("status")
                    == "completed"
                )
                delivery_waiting = delivery_waiting or not developer_complete
                actions.append(
                    {
                        "operation": f"digest_{kind}_developer",
                        "developer": developer_result,
                        "public": {"status": "waiting_for_agent_review"},
                    }
                )
                continue
            mode = _runtime_mode(config)
            cli = _lark(args.lark_cli, strict=True) if mode == "claude_lark_cli" else None
            public_result = deliver_digest(
                state, paths, config, output_kind=kind, apply=True, lark=cli
            )
            developer_result = deliver_developer_digest(
                state, paths, config, output_kind=kind, apply=True, lark=cli
            )
            actions.append(
                {
                    "operation": f"digest_{kind}",
                    "public": public_result,
                    "developer": developer_result,
                }
            )
            public_complete = (
                (public_result.get("delivery") or {}).get("status") == "completed"
            )
            developer_complete = (
                (developer_result.get("delivery") or {}).get("status") == "completed"
            )
            delivery_waiting = delivery_waiting or not (
                public_complete and developer_complete
            )
            if public_complete and developer_complete:
                state.set_meta(f"scheduler_digest_{kind}", prepared_period)
        if usable_roster_checkpoint is not None and (
            validation_failed
            or not (
                roster_bridge_waiting
                or master_bridge_waiting
                or reviews_waiting
                or delivery_waiting
            )
        ):
            state.set_meta(
                "scheduler_roster_checkpoint",
                {
                    **usable_roster_checkpoint,
                    "status": "consumed",
                    "consumed_at": utc_now(),
                },
            )
        public_delivery_blocked = bool(
            validation_failed
            or roster_bridge_waiting
            or master_bridge_waiting
            or reviews_waiting
            or delivery_waiting
        )
        return {
            "skipped": False,
            "scan_due": scan_is_due,
            "validation_failed": validation_failed,
            "scan_error": scan_error,
            "roster_sync_failed": roster_sync_error is not None,
            "roster_bridge_waiting": roster_bridge_waiting,
            "master_bridge_waiting": master_bridge_waiting,
            "public_delivery_blocked": public_delivery_blocked,
            "reviews_waiting": reviews_waiting,
            "delivery_waiting": delivery_waiting,
            "actions": actions,
            "next": (
                "complete every roster read bridge and rerun schedule-tick"
                if roster_bridge_waiting
                else "complete and apply the pending master bridge, then resume this tick"
                if master_bridge_waiting
                else "inspect the developer report for repair details"
                if validation_failed
                else "apply the bounded execution-agent review bundle and resume this tick"
                if reviews_waiting
                else "complete the pending delivery bridge and resume this tick"
                if delivery_waiting
                else None
            ),
        }
    finally:
        state.close()


def _scheduler_period(kind: str, now: datetime, timezone_name: str) -> str:
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(timezone_name))
    if kind == "daily":
        return local.date().isoformat()
    start = local.date() - timedelta(days=local.weekday())
    return start.isoformat()


def _digest_due_now(kind: str, now: datetime, schedule: dict[str, Any]) -> bool:
    from zoneinfo import ZoneInfo

    local = now.astimezone(ZoneInfo(schedule["timezone"]))
    if kind == "daily":
        value = str(schedule.get("daily_digest") or "18:00")
        try:
            hour, minute = (int(item) for item in value.split(":", 1))
        except (TypeError, ValueError):
            raise ValueError("daily_digest must use HH:MM")
        return (local.hour, local.minute) >= (hour, minute)
    value = str(schedule.get("weekly_digest") or "MON 08:30").upper()
    try:
        day, clock = value.split(None, 1)
        hour, minute = (int(item) for item in clock.split(":", 1))
    except (TypeError, ValueError):
        raise ValueError("weekly_digest must use DDD HH:MM")
    weekdays = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}
    if day not in weekdays:
        raise ValueError("weekly_digest weekday is invalid")
    return local.weekday() > weekdays[day] or (
        local.weekday() == weekdays[day] and (local.hour, local.minute) >= (hour, minute)
    )


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="people-tracking-feishu")
    root.add_argument("--config-root", type=Path)
    root.add_argument("--state-root", type=Path)
    root.add_argument("--lark-cli")
    root.add_argument("--json", action="store_true", help="emit the stable JSON envelope")
    commands = root.add_subparsers(dest="command", required=True)

    onboarding = commands.add_parser("onboarding")
    onboarding.add_argument("--answers", type=Path)
    onboarding.add_argument("--show", action="store_true")
    onboarding.add_argument("--enable", action="store_true")
    onboarding.add_argument("--confirmation")
    onboarding.add_argument("--register-schedule", action="store_true")
    onboarding.add_argument("--launcher")

    probe = commands.add_parser("source-probe")
    probe.add_argument("--bridge-results", type=Path)

    sync = commands.add_parser("sync")
    sync.add_argument("--bridge-input", type=Path)
    sync.add_argument("--master-bridge-results", type=Path)
    sync.add_argument("--apply", action="store_true")

    source_routes = commands.add_parser("source-routes")
    source_routes.add_argument("--apply", action="store_true")

    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--confirmation", required=True)
    bootstrap.add_argument("--source-bridge-input", type=Path)
    bootstrap.add_argument("--anchor-bridge-input", type=Path)
    bootstrap.add_argument("--master-bridge-results", type=Path)
    bootstrap.add_argument("--launcher")
    bootstrap.add_argument(
        "--skip-schedule",
        action="store_true",
        help="confirm interactive-only operation without unattended host-Agent automation",
    )

    scan = commands.add_parser("scan")
    scan.add_argument("--force-all", action="store_true")
    scan.add_argument(
        "--force-full-fetch",
        action="store_true",
        help=(
            "disable ETag/Last-Modified for already selected sources without "
            "changing which sources are scheduled"
        ),
    )
    scan.add_argument(
        "--source-kind",
        action="append",
        choices=("homepage", "scholar", "github", "linkedin"),
        help="scan only this public source kind; repeat to select multiple kinds",
    )
    scan.add_argument(
        "--source-id",
        action="append",
        help="scan this changed source in addition to normally due sources; repeat as needed",
    )
    scan.add_argument(
        "--max-error-rate",
        type=float,
        default=0.10,
        help="mark scan validation failed when source errors exceed this ratio",
    )
    scan.add_argument(
        "--homepage-retries",
        type=int,
        choices=(0, 1),
        default=1,
        help="retry a narrow transient Homepage failure once at most",
    )
    scan.add_argument(
        "--homepage-backoff-seconds",
        type=_nonnegative_finite_float,
        default=1.0,
        help="initial Homepage retry delay before deterministic jitter",
    )

    digest = commands.add_parser("digest")
    digest.add_argument("--kind", choices=("daily", "weekly"), default="daily")
    digest.add_argument(
        "--audience", choices=("public", "developer", "both"), default="both"
    )
    digest.add_argument("--tracker-run-id")
    digest.add_argument("--apply", action="store_true")
    digest.add_argument("--bridge-results", type=Path)

    review_export = commands.add_parser("review-export")
    review_export.add_argument("--output", type=Path, required=True)
    review_export.add_argument("--lookback-days", type=int, default=8)

    review_apply = commands.add_parser("review-apply")
    review_apply.add_argument("--input", type=Path, required=True)

    commands.add_parser("doctor")

    sandbox = commands.add_parser("sandbox-e2e")
    sandbox.add_argument("--apply", action="store_true")
    sandbox.add_argument("--confirmation")

    commands.add_parser("schedule-tick", help=argparse.SUPPRESS)
    return root


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    # Agent examples naturally put the output flag after the subcommand.  Keep
    # one stable parser while accepting that harmless ordering.
    if "--json" in values:
        values = ["--json", *(value for value in values if value != "--json")]
    args = parser().parse_args(values)
    try:
        paths = _runtime_paths(args)
        handlers = {
            "onboarding": command_onboarding,
            "source-probe": command_source_probe,
            "sync": command_sync,
            "source-routes": command_source_routes,
            "bootstrap": command_bootstrap,
            "scan": command_scan,
            "digest": command_digest,
            "review-export": command_review_export,
            "review-apply": command_review_apply,
            "doctor": command_doctor,
            "sandbox-e2e": command_sandbox,
            "schedule-tick": command_schedule_tick,
        }
        data = handlers[args.command](args, paths)
        scan_partial = (
            args.command == "scan"
            and data.get("tracker_run_status") == "partial"
        )
        validation_failed = (
            args.command == "scan"
            and not data.get("validation", {}).get("passed", False)
        ) or (
            args.command == "schedule-tick"
            and bool(data.get("validation_failed") or data.get("roster_sync_failed"))
        )
        if validation_failed or scan_partial:
            error_type = "ScanPartialFailure" if scan_partial else "ScanValidationFailed"
            error_message = (
                "scan completed partially; source errors were preserved for retry"
                if scan_partial
                else "scan evidence was preserved, but strict validation failed"
            )
            payload = {
                "ok": False,
                "command": args.command,
                "version": __version__,
                "data": data,
                "error": {
                    "type": error_type,
                    "message": error_message,
                },
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 3
        payload = {"ok": True, "command": args.command, "version": __version__, "data": data}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except (
        ConfigError,
        LarkCliError,
        RuntimeError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        payload = {
            "ok": False,
            "command": getattr(args, "command", None),
            "version": __version__,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
