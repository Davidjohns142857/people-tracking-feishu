from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from people_intel.light_cli import _fetch
from people_intel.light_tracker import FetchObservation, LightTracker
from people_intel.person_profile_schemas import PersonDigestBatch
from people_intel.person_profiles import PersonProfileService
from people_intel.service import TemporalMemoryService


def _urls_from_value(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith(("http://", "https://")) else []
    if isinstance(value, dict):
        output: list[str] = []
        for item in value.values():
            output.extend(_urls_from_value(item))
        return output
    if isinstance(value, list):
        output = []
        for item in value:
            output.extend(_urls_from_value(item))
        return output
    return []


def sync_profiles(
    service: TemporalMemoryService, tracker: LightTracker
) -> dict[str, int]:
    profiles = PersonProfileService(service)
    counts = {"profiles": 0, "admitted": 0, "without_trackable_url": 0}
    for summary in profiles.list_profiles():
        counts["profiles"] += 1
        view = profiles.profile_view(summary.person_key)
        urls: list[str] = []
        if view.search_plan is not None:
            urls.extend(
                target.value
                for target in view.search_plan.targets
                if target.enabled
                and target.target_type == "known_url"
                and target.value.startswith(("http://", "https://"))
            )
        for item in view.profile.fields.get("urls", []):
            urls.extend(_urls_from_value(item.value))
        urls = list(dict.fromkeys(urls))
        if not urls:
            counts["without_trackable_url"] += 1
            continue
        tracker.add_person(
            view.profile.canonical_name,
            aliases=view.profile.aliases,
            urls=urls,
            profile={"backend_person_key": summary.person_key},
            secondary_id=("person_key", summary.person_key),
        )
        counts["admitted"] += 1
    return counts


def due_sources(
    tracker: LightTracker, *, cadence_days: int, now: datetime | None = None
) -> list[dict[str, Any]]:
    boundary = now or datetime.now(timezone.utc)
    cutoff = boundary - timedelta(days=cadence_days)
    output: list[dict[str, Any]] = []
    for person in tracker.list_people():
        for source in person["sources"]:
            last_checked = source.get("last_checked_at")
            if source["kind"] == "linkedin" and source.get("next_check_at"):
                try:
                    next_check = datetime.fromisoformat(
                        str(source["next_check_at"]).replace("Z", "+00:00")
                    )
                except ValueError:
                    next_check = boundary
                if next_check > boundary:
                    continue
            elif last_checked:
                try:
                    checked = datetime.fromisoformat(
                        str(last_checked).replace("Z", "+00:00")
                    )
                except ValueError:
                    checked = cutoff
                if checked > cutoff:
                    continue
            output.append({**source, "canonical_name": person["canonical_name"]})
    return output


def _reportable_people(tracker: LightTracker, run_id: str) -> list[str]:
    return [
        str(row["person_key"])
        for row in tracker.db.execute(
            """SELECT DISTINCT s.person_key
               FROM observations o JOIN sources s USING(source_id)
               WHERE o.run_id=? AND o.decision_status IN ('changed','source_issue')
               ORDER BY s.person_key""",
            (run_id,),
        )
    ]


def run_once(
    service: TemporalMemoryService,
    *,
    state_database: str | Path,
    cadence_days: int = 7,
    workers: int = 8,
) -> dict[str, Any]:
    tracker = LightTracker(state_database)
    try:
        sync = sync_profiles(service, tracker)
        due = due_sources(tracker, cadence_days=cadence_days)
        observations: dict[str, FetchObservation] = {}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(_fetch, source): source for source in due}
            for future in as_completed(futures):
                source = futures[future]
                try:
                    observations[source["source_id"]] = future.result()
                except Exception as exc:
                    observations[source["source_id"]] = FetchObservation(
                        body=None,
                        status_code=0,
                        final_url=source["url"],
                        error=f"{type(exc).__name__}: {exc}",
                    )

        run_id = tracker.start_run("bundled-weekly-web-monitor")
        counts: dict[str, int] = {}
        for source in due:
            decision = tracker.observe(
                run_id,
                source["source_id"],
                observations[source["source_id"]],
            )
            counts[decision.status] = counts.get(decision.status, 0) + 1
        tracker.complete_run(run_id)
        report = tracker.render_report(run_id)
        reportable_people = _reportable_people(tracker, run_id)
        digest_batch_id = None
        if reportable_people:
            stored = service.object_store.put_text(report)
            digest = PersonDigestBatch(
                period="weekly",
                person_keys=reportable_people,
                markdown_ref=stored.object_ref,
                markdown_hash=stored.digest,
            )
            service.ledger.append_person_digest_batch(digest)
            digest_batch_id = digest.digest_batch_id
        return {
            "ok": True,
            "run_id": run_id,
            "profiles_sync": sync,
            "sources_due": len(due),
            "decisions": dict(sorted(counts.items())),
            "reportable_people": len(reportable_people),
            "digest_batch_id": digest_batch_id,
            "agent_review": {
                "mode": "execution_agent",
                "decision_authority": "skill_host_agent",
                "external_model_api_called": False,
            },
            "report": report,
        }
    finally:
        tracker.close()


def run_forever(
    service: TemporalMemoryService,
    *,
    state_database: str | Path,
    cadence_days: int,
    workers: int,
    poll_seconds: float,
) -> None:
    while True:
        try:
            result = run_once(
                service,
                state_database=state_database,
                cadence_days=cadence_days,
                workers=workers,
            )
        except Exception as exc:
            result = {
                "ok": False,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "secrets_returned": False,
            }
        print(json.dumps(result, ensure_ascii=False), flush=True)
        time.sleep(max(60.0, poll_seconds))


__all__ = ["due_sources", "run_forever", "run_once", "sync_profiles"]
