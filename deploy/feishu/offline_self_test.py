#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "runtime"))
WHEEL_RUNTIME = tempfile.TemporaryDirectory(prefix="people-tracking-feishu-wheels-")
for wheel in sorted((ROOT / "vendor" / "wheels").glob("*.whl")):
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(WHEEL_RUNTIME.name)
sys.path.insert(0, WHEEL_RUNTIME.name)

from people_intel.light_tracker import FetchObservation  # noqa: E402
from people_tracking_feishu.cli import command_bootstrap, command_sync  # noqa: E402
from people_tracking_feishu.config import (  # noqa: E402
    ConfigError,
    RuntimePaths,
    mark_state,
    normalize_answers,
    secure_write_json,
    validate_config,
)
from people_tracking_feishu.reporting import (  # noqa: E402
    PUBLIC_DECISION_SCHEMA,
    apply_agent_review_decisions,
    build_agent_review_bundle,
    materialize_window_events,
)
from people_tracking_feishu.sandbox import algorithm_e2e  # noqa: E402
from people_tracking_feishu.scheduler import scheduler_artifacts  # noqa: E402
from people_tracking_feishu.state import PortableState  # noqa: E402
import people_tracking_feishu.tracking as tracking_module  # noqa: E402


def main() -> int:
    results: list[dict[str, object]] = []

    def case(name: str, fn) -> None:
        started = perf_counter()
        try:
            fn()
        except Exception as exc:
            results.append(
                {
                    "name": name,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "wall_ms": round((perf_counter() - started) * 1000, 3),
                }
            )
        else:
            results.append(
                {
                    "name": name,
                    "status": "passed",
                    "wall_ms": round((perf_counter() - started) * 1000, 3),
                }
            )

    with tempfile.TemporaryDirectory(prefix="people-tracking-feishu-self-test-") as temporary:
        root = Path(temporary)
        case(
            "algorithm_baseline_candidate_changed",
            lambda: (
                algorithm_e2e(root / "sandbox.sqlite3")["decisions"]
                == ["baseline", "candidate", "changed"]
                or (_ for _ in ()).throw(AssertionError("unexpected decision sequence"))
            ),
        )

        def inline_secret() -> None:
            try:
                payload = normalize_answers(
                    {
                        "sources": [{"kind": "local_file", "path": str(root / "synthetic.md")}],
                        "master_database": {"mode": "local_or_api"},
                        "outputs": {
                            "document": {"enabled": False},
                            "message": {"enabled": False},
                        },
                        "apis": {
                            "search": [
                                {
                                    "name": "synthetic-search",
                                    "secret_reference": "api_" + "key=synthetic-test-value",
                                }
                            ]
                        },
                    }
                )
                validate_config(payload)
            except ConfigError:
                return
            raise AssertionError("inline secret was accepted")

        case("inline_secret_rejected", inline_secret)
        case(
            "macos_scheduler_namespaced",
            lambda: (
                all(
                    "people-tracking-feishu-" in path.name
                    for path in scheduler_artifacts(
                        home=root,
                        launcher=root / "launcher",
                        platform="darwin",
                    )
                )
                or (_ for _ in ()).throw(AssertionError("macOS scheduler not namespaced"))
            ),
        )
        case(
            "linux_scheduler_namespaced",
            lambda: (
                all(
                    "people-tracking-feishu-" in path.name
                    for path in scheduler_artifacts(
                        home=root,
                        launcher=root / "launcher",
                        platform="linux",
                    )
                )
                or (_ for _ in ()).throw(AssertionError("Linux scheduler not namespaced"))
            ),
        )
        case(
            "skill_payload_present",
            lambda: (
                (ROOT / "packages" / "feishu" / "people-tracking" / "SKILL.md").is_file()
                or (_ for _ in ()).throw(AssertionError("skill is missing"))
            ),
        )

        def homepage_force_full_fetch_and_retry() -> None:
            paths = RuntimePaths(
                config_root=root / "retry-config",
                state_root=root / "retry-state",
                config_file=root / "retry-config/config.json",
                database=root / "retry-state/people.sqlite3",
                reports=root / "retry-state/reports",
                install_state=root / "retry-config/install-state.json",
            )
            paths.ensure()
            state = PortableState(paths.database)
            original_fetch = tracking_module._fetch
            try:
                person = state.tracker.add_person(
                    "Synthetic Retry",
                    urls=["https://synthetic.invalid/retry"],
                )
                source_id = person["sources"][0]["source_id"]
                state.db.execute(
                    "UPDATE sources SET etag='synthetic-etag',last_full_fetch_at=? WHERE source_id=?",
                    (datetime.now(timezone.utc).isoformat(), source_id),
                )
                state.db.commit()
                calls: list[dict[str, object]] = []

                def synthetic_fetch(source_row):
                    calls.append(dict(source_row))
                    if len(calls) == 1:
                        return FetchObservation(
                            body=None,
                            status_code=503,
                            error="synthetic transient upstream failure",
                        )
                    return FetchObservation(
                        body=(
                            "<html><title>Synthetic Retry</title><h1>Synthetic Retry</h1>"
                            "<h2>Publications</h2><ul>"
                            "<li>Reliable Retry Research, 2026</li></ul></html>"
                        ),
                        status_code=200,
                    )

                tracking_module._fetch = synthetic_fetch
                result = tracking_module.scan_due(
                    state,
                    paths,
                    {"state": "enabled", "apis": {"search": []}},
                    force_all=True,
                    force_full_fetch=True,
                    source_kinds=["homepage"],
                    homepage_retries=1,
                    homepage_backoff_seconds=0.0,
                )
                if len(calls) != 2 or not all(call.get("_force_full_fetch") for call in calls):
                    raise AssertionError("forced body retry policy was not applied")
                if result["metrics"]["homepage_retries"] != 1:
                    raise AssertionError("homepage retry metric is incorrect")
                if result["metrics"]["full_fetch_attempted"] != 1:
                    raise AssertionError("full-fetch metric is incorrect")
            finally:
                tracking_module._fetch = original_fetch
                state.close()

        case("homepage_force_full_fetch_and_retry", homepage_force_full_fetch_and_retry)

        def source_routes_preserve_baseline() -> None:
            state = PortableState(root / "source-routes.sqlite3")
            try:
                person = state.tracker.add_person(
                    "Synthetic Routed",
                    urls=["https://synthetic.invalid/routed"],
                )
                source_id = person["sources"][0]["source_id"]
                state.db.execute(
                    "UPDATE sources SET snapshot_json=?,semantic_hash=? WHERE source_id=?",
                    ('{"synthetic":"baseline"}', "synthetic-baseline", source_id),
                )
                state.db.commit()
                route = {
                    "url": "https://synthetic.invalid/routed",
                    "alternate_routes": [
                        {
                            "route": "raw_html",
                            "url": "https://raw.githubusercontent.com/example/profile/main/index.html",
                        }
                    ],
                }
                preview = state.sync_source_routes([route], apply=False)
                applied = state.sync_source_routes([route], apply=True)
                row = state.db.execute(
                    "SELECT snapshot_json,semantic_hash FROM sources WHERE source_id=?",
                    (source_id,),
                ).fetchone()
                if preview["mutated"] or applied["counts"]["configured"] != 1:
                    raise AssertionError("source route preview/apply contract failed")
                if tuple(row) != ('{"synthetic":"baseline"}', "synthetic-baseline"):
                    raise AssertionError("source route changed the confirmed baseline")
            finally:
                state.close()

        case("source_routes_preserve_baseline", source_routes_preserve_baseline)

        def execution_agent_review_and_report_separation() -> None:
            paths = RuntimePaths(
                config_root=root / "report-config",
                state_root=root / "report-state",
                config_file=root / "report-config/config.json",
                database=root / "report-state/people.sqlite3",
                reports=root / "report-state/reports",
                install_state=root / "report-config/install-state.json",
            )
            paths.ensure()
            state = PortableState(paths.database)
            try:
                person = state.tracker.add_person(
                    "Synthetic Reporter",
                    urls=["https://synthetic.invalid/reporter"],
                )
                source_id = person["sources"][0]["source_id"]
                observations = [
                    (
                        "paper",
                        "changed",
                        "healthy",
                        {
                            "additions": [
                                {
                                    "category": "publication",
                                    "text": "Concrete Offline Paper, 2026",
                                    "stable_id": "paper:offline-2026",
                                }
                            ],
                            "modifications": [],
                            "removals": [],
                        },
                        "confirmed publication change",
                    ),
                    (
                        "failure",
                        "source_issue",
                        "rate_limited",
                        {},
                        "HTTP 429 parser error while scanning",
                    ),
                ]
                for suffix, decision_status, health_status, delta, summary in observations:
                    run_id = f"run-offline-{suffix}"
                    observation_id = f"obs-offline-{suffix}"
                    state.db.execute(
                        """INSERT INTO runs(
                             run_id,started_at,completed_at,trigger,status,purpose
                           ) VALUES(?,?,?,?,?,'production')""",
                        (
                            run_id,
                            "2026-09-04T01:00:00+00:00",
                            "2026-09-04T01:05:00+00:00",
                            "offline-self-test",
                            "completed",
                        ),
                    )
                    state.db.execute(
                        """INSERT INTO observations(
                             observation_id,run_id,source_id,observed_at,health_status,
                             semantic_hash,decision_status,score,delta_json,summary,reviewer
                           ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            observation_id,
                            run_id,
                            source_id,
                            "2026-09-04T01:03:00+00:00",
                            health_status,
                            f"hash-offline-{suffix}",
                            decision_status,
                            0.95,
                            json.dumps(delta, ensure_ascii=False),
                            summary,
                            "deterministic",
                        ),
                    )
                state.db.commit()
                materialize_window_events(
                    state,
                    window_start="2026-09-04T00:00:00+00:00",
                    window_end="2026-09-04T02:00:00+00:00",
                )
                bundle = build_agent_review_bundle(state)
                if len(bundle["requests"]) != 1 or state.unreported_public_events(
                    through="2026-09-04T02:00:00+00:00"
                ):
                    raise AssertionError("public change bypassed execution-Agent review")
                request = bundle["requests"][0]
                evidence = request["evidence"]
                applied = apply_agent_review_decisions(
                    state,
                    {
                        "schema_version": PUBLIC_DECISION_SCHEMA,
                        "review_snapshot_id": bundle["review_snapshot"]["snapshot_id"],
                        "decided_by": "execution-agent:offline-self-test",
                        "decisions": [
                            {
                                "request_id": request["request_id"],
                                "evidence_hash": request["evidence_hash"],
                                "decision": "publish",
                                "change_type": "publication_added",
                                "headline": "新增论文",
                                "what_changed": "新增论文：Concrete Offline Paper, 2026",
                                "why_material": "这是明确的新论文成果",
                                "confidence": 0.99,
                                "evidence_ids": [evidence["event_id"]],
                            }
                        ],
                    },
                )
                if applied["applied"] != 1:
                    raise AssertionError("execution-Agent decision was not applied")
                prepared = tracking_module.prepare_digest(
                    state,
                    paths,
                    {
                        "state": "enabled",
                        "schedule": {"timezone": "Asia/Shanghai"},
                        "outputs": {
                            "public": {
                                "document": {"enabled": False},
                                "message": {"enabled": False},
                            },
                            "developer": {
                                "document": {"enabled": False},
                                "message": {"enabled": False},
                            },
                        },
                    },
                    output_kind="daily",
                    at=datetime(2026, 9, 4, 2, 0, tzinfo=timezone.utc),
                )
                public_report = prepared["report"]
                developer_report = prepared["developer"]["report"]
                if "Concrete Offline Paper, 2026" not in public_report:
                    raise AssertionError("approved concrete change is missing from user report")
                if "HTTP 429" in public_report or "parser" in public_report:
                    raise AssertionError("developer diagnostic leaked into user report")
                if "HTTP 429 parser error while scanning" not in developer_report:
                    raise AssertionError("developer report lost the source diagnostic")
            finally:
                state.close()

        case(
            "execution_agent_review_and_report_separation",
            execution_agent_review_and_report_separation,
        )

        def authoritative_base_bridge_preserves_live_human_fields() -> None:
            paths = RuntimePaths(
                config_root=root / "bridge-config",
                state_root=root / "bridge-state",
                config_file=root / "bridge-config/config.json",
                database=root / "bridge-state/people.sqlite3",
                reports=root / "bridge-state/reports",
                install_state=root / "bridge-config/install-state.json",
            )
            paths.ensure()
            config = normalize_answers(
                {
                    "runtime": {"mode": "openclaw"},
                    "sources": [],
                    "field_mapping": {
                        "person_key": "Person Key",
                        "record_type": "Record Type",
                        "name": "Name",
                        "secondary_id": "ID",
                        "homepage": "Homepage",
                    },
                    "master_database": {
                        "mode": "existing_base",
                        "url": (
                            "https://synthetic.feishu.cn/base/bas_offline"
                            "?table=tbl_people"
                        ),
                        "base_token": "bas_offline",
                        "people_table_id": "tbl_people",
                    },
                    "outputs": {
                        "public": {
                            "document": {"enabled": False},
                            "message": {"enabled": False},
                        },
                        "developer": {"local_markdown": {"enabled": True}},
                    },
                    "schedule": {"timezone": "Asia/Shanghai", "scan": "daily"},
                }
            )
            config["state"] = "enabled"
            config["validation"] = {"all_ok": True}
            secure_write_json(paths.config_file, config)
            sync_args = argparse.Namespace(
                lark_cli=None,
                bridge_input=None,
                master_bridge_results=None,
                apply=True,
                allow_validated=False,
                include_local_sources=True,
            )
            waiting = command_sync(sync_args, paths)
            request = waiting["bridge_request"]
            source_ref = config["master_database"]["authoritative_source_ref"]
            bridge_file = root / "authoritative-source-bridge.json"
            secure_write_json(
                bridge_file,
                {
                    "all_ok": True,
                    "bridge_nonce": request["bridge_nonce"],
                    "expected_refs": request["expected_refs"],
                    "expected_refs_hash": request["expected_refs_hash"],
                    "request_hash": request["request_hash"],
                    "sources": [
                        {
                            "source_ref": source_ref,
                            "source_complete": True,
                            "base_token": "bas_offline",
                            "table_id": "tbl_people",
                            "schema_fields": [
                                "Person Key",
                                "Record Type",
                                "Name",
                                "ID",
                                "Homepage",
                                "Sync Status",
                                "Human Notes",
                            ],
                            "payload": {
                                "records": [
                                    {
                                        "record_id": "rec_offline_person",
                                        "Name": "Offline Person",
                                        "Person Key": "person_offline_1",
                                        "Record Type": "人员",
                                        "ID": "offline-1",
                                        "Homepage": "https://synthetic.invalid/offline-person",
                                        "Sync Status": "旧状态",
                                        "Human Notes": "must survive",
                                    }
                                ]
                            },
                        }
                    ],
                },
            )
            sync_args.bridge_input = bridge_file
            result = command_sync(sync_args, paths)
            master_result = result.get("master_database") or {}
            payload_file = Path(str(master_result.get("payload_file") or ""))
            if not payload_file.is_file():
                raise AssertionError("authoritative sync did not queue its safe write bridge")
            payload = json.loads(payload_file.read_text(encoding="utf-8"))
            people = payload["records"]["People"]
            if len(people) != 1:
                raise AssertionError("authoritative person row was duplicated")
            item = people[0]
            contract = item["write_contract"]
            if item.get("target_record_id") != "rec_offline_person":
                raise AssertionError("write bridge lost the authoritative record_id")
            if contract["operation"] != "update":
                raise AssertionError("authoritative row was converted into a create")
            if contract["expected_before_fields"]["fields"].get(
                "Human Notes"
            ) != "must survive":
                raise AssertionError("write bridge did not bind the live human fields")
            if {"name", "homepage", "secondary_id"} & set(item["field_values"]):
                raise AssertionError("write bridge exposed human-owned fields for update")

        case(
            "authoritative_base_bridge_preserves_live_human_fields",
            authoritative_base_bridge_preserves_live_human_fields,
        )

        def bootstrap_pipeline() -> None:
            source = root / "bootstrap-people.md"
            source.write_text(
                """| Name | ID | Homepage |
| --- | --- | --- |
| Synthetic Portable | portable-1 | https://synthetic.invalid/portable |
""",
                encoding="utf-8",
            )
            paths = RuntimePaths(
                config_root=root / "config",
                state_root=root / "state",
                config_file=root / "config/config.json",
                database=root / "state/people.sqlite3",
                reports=root / "state/reports",
                install_state=root / "config/install-state.json",
            )
            paths.ensure()
            config = normalize_answers(
                {
                    "runtime": {"mode": "openclaw"},
                    "sources": [{"kind": "local_file", "path": str(source)}],
                    "field_mapping": {
                        "name": "Name",
                        "secondary_id": "ID",
                        "homepage": "Homepage",
                    },
                    "intake": {"missing_anchor_policy": "manual_queue"},
                    "master_database": {"mode": "local_or_api"},
                    "outputs": {
                        "document": {"enabled": False},
                        "message": {"enabled": False},
                    },
                    "schedule": {"timezone": "Asia/Shanghai"},
                    "apis": {"search": []},
                }
            )
            secure_write_json(paths.config_file, config)
            mark_state(paths, config, "validated", validation={"all_ok": True})
            tracking_module._fetch = lambda source_row: FetchObservation(
                body=(
                    "<html><title>Synthetic Portable</title>"
                    "<h1>Synthetic Portable</h1><h2>Publications</h2><ul>"
                    "<li>Reliable Synthetic Profile Monitoring, 2026</li>"
                    "</ul></html>"
                ),
                status_code=200,
            )
            result = command_bootstrap(
                argparse.Namespace(
                    lark_cli=None,
                    confirmation="确认启用",
                    source_bridge_input=None,
                    anchor_bridge_input=None,
                    master_bridge_results=None,
                    launcher=None,
                    skip_schedule=True,
                ),
                paths,
            )
            if result.get("status") != "ready":
                raise AssertionError("bootstrap did not reach ready")
            if result["database"]["counts"]["people"] != 1:
                raise AssertionError("bootstrap did not import the synthetic person")

        case("bootstrap_import_baseline_ready", bootstrap_pipeline)
    passed = sum(item["status"] == "passed" for item in results)
    payload = {
        "ok": passed == len(results),
        "passed": passed,
        "total": len(results),
        "synthetic_only": True,
        "network_used": False,
        "results": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
