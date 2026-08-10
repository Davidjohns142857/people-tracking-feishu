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
from people_tracking_feishu.cli import command_bootstrap  # noqa: E402
from people_tracking_feishu.config import (  # noqa: E402
    ConfigError,
    RuntimePaths,
    mark_state,
    normalize_answers,
    secure_write_json,
    validate_config,
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
                            "deepseek": {
                                "enabled": True,
                                "model": "deepseek-v4-flash",
                                "key_reference": "api_" + "key=synthetic-test-value",
                            }
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
                    {"state": "enabled", "apis": {"deepseek": {"enabled": False}}},
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
                    "apis": {"deepseek": {"enabled": False, "model": "deepseek-v4-flash"}},
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
