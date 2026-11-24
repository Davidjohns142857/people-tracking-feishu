"""Persistent controlled Homepage and ground-truth validation, isolated from live data.

Only ``public/index.html`` is intended for static hosting. The private manifest,
hash-chained injection/observation ledgers and validation.sqlite3 stay on the
Agent host. ``tick_profile`` is a bounded callable for the existing host tick;
this module never installs a scheduler or sends Feishu messages.
"""

from __future__ import annotations

import fcntl
import hashlib
import html
import json
import os
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator
from urllib.request import ProxyHandler, Request, build_opener

from people_intel.light_cli import _fetch, _validate_public_url
from people_intel.light_tracker import FetchObservation, LightTracker

SCHEMA = "people-tracking-controlled-profile-v1"
SCENARIOS = ("current", "historical_backfill", "undated", "no_change")
CAREER_SCENARIOS = ("startup", "job_change")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("validation timestamps require a timezone")
    return result.astimezone(timezone.utc)


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.chmod(0o600)
    pending.replace(path)


def _records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    result = []
    previous = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        actual = record.pop("record_hash")
        digest = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if actual != digest or record.get("previous_hash") != previous:
            raise ValueError(f"validation ledger hash chain failed: {path.name}")
        record["record_hash"] = actual
        previous = actual
        result.append(record)
    return result


def _append(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    old = _records(path)
    value = {**record, "previous_hash": old[-1]["record_hash"] if old else ""}
    value["record_hash"] = hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)
    return value


@contextmanager
def _locked(root: Path) -> Iterator[dict[str, Any]]:
    path = root / "manifest.json"
    if not path.is_file():
        raise ValueError("controlled profile is not initialized")
    with (root / "profile.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        manifest = _read(path)
        if manifest.get("schema_version") != SCHEMA or manifest.get("test_only") is not True:
            raise ValueError("not an isolated controlled profile")
        yield manifest


def _render(root: Path, manifest: dict[str, Any]) -> None:
    rows, roles = [], []
    version = f"CTRL-{manifest['instance_id'][:8]}-00000"
    for event in _records(root / "injections.jsonl"):
        if event["scenario"] == "no_change":
            continue
        version = event["event_id"]
        date = f", published {event['event_date']}" if event["event_date"] else ""
        destination = roles if event["scenario"] in CAREER_SCENARIOS else rows
        destination.append(
            f'<p><a href="https://example.invalid/controlled/{event["event_id"]}">'
            f'{html.escape(event["title"])}{html.escape(date)}</a></p>'
        )
    name = html.escape(manifest["display_name"])
    body = (
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="robots" content="noindex,nofollow">'
        f'<title>{name}</title></head><body><main data-people-tracking-debug-version="{version}"><h1>{name}</h1>'
        '<p>TEST CONTROLLED PROFILE. 此页用于自动跟踪系统验证。以下成果均为模拟测试，'
        '不代表任何真实论文、任职或个人履历。No contact information is published.</p>'
        '<h2>Research</h2><p>Interests: AI/ML research reading; financial research; '
        'researcher information research; tool development. '
        '兴趣：AI/ML研究阅读、金融研究、人物信息调研、工具开发。</p>'
        '<h2>Publications</h2><p>Controlled Baseline Reference, Evaluation Systems 2025</p>'
        + "".join(rows)
        + ("<h2>Experience</h2>" + "".join(roles) if roles else "")
        + '</main></body></html>'
    )
    public = root / "public"
    public.mkdir(exist_ok=True)
    pending = public / "index.html.pending"
    pending.write_text(body, encoding="utf-8")
    pending.replace(public / "index.html")


def init_profile(
    root: Path,
    *,
    subject_name: str,
    public_url: str | None = None,
    interval_seconds: int = 86400,
    detection_sla_seconds: int = 3600,
    confirmation_sla_seconds: int = 7200,
) -> dict[str, Any]:
    """Create an explicit test identity; never infer biography or contact details."""
    root = Path(root)
    if not subject_name.strip() or len(subject_name) > 100 or "\n" in subject_name:
        raise ValueError("subject_name must be a short explicit display identity")
    if any(type(value) is not int or value < 1 for value in (
        interval_seconds, detection_sla_seconds, confirmation_sla_seconds
    )):
        raise ValueError("validation intervals and SLAs must be positive integers")
    if confirmation_sla_seconds < detection_sla_seconds:
        raise ValueError("confirmation SLA cannot precede detection SLA")
    if public_url:
        _validate_public_url(public_url, resolve=False)
    root.mkdir(parents=True, exist_ok=True)
    if (root / "manifest.json").exists():
        with _locked(root) as manifest:
            return {**manifest, "idempotent_replay": True}
    if any(root.iterdir()):
        raise ValueError("initialize controlled profile in a new empty directory")
    now = _now()
    manifest = {
        "schema_version": SCHEMA,
        "test_only": True,
        "instance_id": str(uuid.uuid4()),
        "display_name": f"[TEST] {subject_name.strip()} — Controlled Profile",
        "public_url": public_url,
        "created_at": now,
        "next_injection_at": (_time(now) + timedelta(seconds=interval_seconds)).isoformat(),
        "interval_seconds": interval_seconds,
        "detection_sla_seconds": detection_sla_seconds,
        "confirmation_sla_seconds": confirmation_sla_seconds,
        "sequence": 0,
        "remote_registration_verified": False,
    }
    _write(root / "manifest.json", manifest)
    _render(root, manifest)
    export_registration(root)
    return manifest


def _advance(root: Path, manifest: dict[str, Any], scenario: str, at: str) -> dict[str, Any]:
    if scenario not in SCENARIOS + CAREER_SCENARIOS:
        raise ValueError("unknown controlled-profile scenario")
    sequence = manifest["sequence"] + 1
    event_id = f"CTRL-{manifest['instance_id'][:8]}-{sequence:05}"
    event_date = (
        (_time(at) - timedelta(days=30)).date().isoformat()
        if scenario == "historical_backfill"
        else _time(at).date().isoformat() if scenario in ("current", *CAREER_SCENARIOS) else None
    )
    titles = {
        "startup": f"I co-founded Controlled Example Studio [TEST] ({event_id})",
        "job_change": f"I joined Controlled Example Lab as Research Engineer [TEST] ({event_id})",
    }
    record = _append(root / "injections.jsonl", {
        "event_id": event_id,
        "scenario": scenario,
        "injected_at": at,
        "event_date": event_date,
        "event_date_status": "unknown" if scenario in {"undated", "no_change"} else scenario,
        "expected_page_change": scenario != "no_change",
        "title": titles.get(scenario, f"Controlled Validation {event_id}, Evaluation Systems 2026 [TEST]"),
    })
    manifest.update(sequence=sequence, next_injection_at=(
        _time(at) + timedelta(seconds=manifest["interval_seconds"])
    ).isoformat())
    _write(root / "manifest.json", manifest)
    _render(root, manifest)
    return record


def advance_profile(root: Path, *, scenario: str = "current") -> dict[str, Any]:
    """Inject now, recording observation-clock injection independently of event date."""
    root = Path(root)
    with _locked(root) as manifest:
        return _advance(root, manifest, scenario, _now())


def export_registration(root: Path) -> dict[str, Any]:
    """Export a test-only list row and truthful target-Agent deployment checklist."""
    root = Path(root)
    with _locked(root) as manifest:
        result = {
            "schema_version": SCHEMA,
            "test_only": True,
            "record": {
                "Name": manifest["display_name"],
                "Homepage": manifest["public_url"],
                "ID": "control-debug:" + manifest["instance_id"],
                "Tracking Status": "Active" if manifest["public_url"] else "Pending deployment",
                "Record Type": "validation_profile",
                "Validation Instance": manifest["instance_id"],
                "Notes": "模拟控制主页；不得合并为真实人物履历，不发送人才动态。",
            },
            "registered_in_remote_list": bool(manifest.get("registration_receipt")),
            "remote_registration_verified_by_this_process": False,
            "registration_receipt": manifest.get("registration_receipt"),
            "deployment": {
                "publish_directory": str(root / "public"),
                "public_url_required": not bool(manifest["public_url"]),
                "private_files_must_not_be_published": ["manifest.json", "injections.jsonl", "observations.jsonl", "validation.sqlite3"],
                "host_tick_callable": "people_tracking_feishu.validation_profile.tick_profile",
                "remote_steps": [
                    "Continuously serve this exact public directory at the declared public HTTPS URL, or publish every mutation using an external writer.",
                    "Register only the explicit [TEST] row in the roster read by the target runner and preserve its record_id.",
                    "Read back record_id and URL; run ordinary remote collection and retain its real receipts.",
                    "Retain a publication/readback receipt with each event ID and its actual publicly visible published_at; local render time is not remote publication time.",
                    "Invoke tick_profile from the existing authorized host tick; for static-copy hosting publish and read back every refreshed index.html before production latency evaluation.",
                    "Evaluate real observations against injection IDs; missing or late detection is a failure.",
                ],
            },
        }
        _write(root / "registration.json", result)
        return result


def bind_origin(root: Path, public_url: str) -> dict[str, Any]:
    """Bind a prepared local profile to its actual public origin once."""
    _validate_public_url(public_url, resolve=False)
    root = Path(root)
    with _locked(root) as manifest:
        if manifest["public_url"] and manifest["public_url"] != public_url:
            raise ValueError("controlled profile public URL is already bound")
        manifest["public_url"] = public_url
        _write(root / "manifest.json", manifest)
    return export_registration(root)


def record_registration_receipt(root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Retain host-Agent readback references, without claiming independent verification."""
    root = Path(root)
    required = {"record_id", "source_id", "source_url", "readback_at", "evidence_ref"}
    if not isinstance(receipt, dict) or set(receipt) != required or any(not isinstance(receipt[k], str) or not receipt[k].strip() for k in required):
        raise ValueError("registration receipt requires record/source IDs, URL, readback time and evidence ref")
    _time(receipt["readback_at"])
    with _locked(root) as manifest:
        if not manifest["public_url"] or receipt["source_url"] != manifest["public_url"]:
            raise ValueError("receipt URL must match the deployed controlled-profile URL")
        existing = manifest.get("registration_receipt")
        if existing and existing != receipt:
            raise ValueError("registration receipt is already frozen; use a new validation instance")
        manifest["registration_receipt"] = receipt
        _write(root / "manifest.json", manifest)
    return export_registration(root)


def import_remote_observations(root: Path, evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Import actual target-runner evidence separately from the local self-test ledger.

    The target Agent must obtain these rows from its real observation ledger.
    References are mandatory; this importer cannot authenticate remote claims.
    """
    root = Path(root)
    with _locked(root) as manifest:
        receipt = manifest.get("registration_receipt")
        if not receipt:
            raise ValueError("record the remote list readback receipt before importing evidence")
        prepared = []
        required = {"observation_id", "run_id", "source_id", "source_url", "record_id", "observed_at", "decision", "evidence_ref"}
        existing = {r["observation_id"]: r for r in _records(root / "remote-observations.jsonl")}
        for item in evidence:
            if not isinstance(item, dict) or set(item) != required:
                raise ValueError("remote observation fields mismatch")
            if any(not isinstance(item[key], str) or not item[key].strip() for key in required - {"decision"}):
                raise ValueError("remote observation references must be nonempty strings")
            if any(item[key] != receipt[key] for key in ("record_id", "source_id", "source_url")):
                raise ValueError("remote observation does not match the registered test row")
            _time(item["observed_at"])
            decision = item["decision"]
            if not isinstance(decision, dict) or decision.get("status") not in {"baseline", "candidate", "changed", "unchanged", "noise", "source_issue_pending", "source_issue", "parser_anomaly", "ambiguous", "binding_review", "binding_conflict"}:
                raise ValueError("invalid remote observation decision")
            if len(json.dumps(item, ensure_ascii=False)) > 65536:
                raise ValueError("remote observation exceeds compact-evidence size limit")
            old = existing.get(item["observation_id"])
            if old:
                if {k: v for k, v in old.items() if k not in {"previous_hash", "record_hash"}} != item:
                    raise ValueError("remote observation ID was replayed with changed evidence")
                continue
            if item["observation_id"] in {p["observation_id"] for p in prepared}:
                raise ValueError("duplicate remote observation IDs in import")
            prepared.append(item)
        for item in prepared:
            _append(root / "remote-observations.jsonl", item)
        return {"imported": len(prepared), "evidence_source": "target_runner_receipts",
                "independently_verified": False}


def record_publication_receipt(root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Record target origin visibility, independently of local mutation preparation."""
    root = Path(root)
    required = {"event_id", "published_at", "source_url", "evidence_ref"}
    if not isinstance(receipt, dict) or set(receipt) != required or any(
        not isinstance(receipt[k], str) or not receipt[k].strip() for k in required
    ):
        raise ValueError("publication receipt requires event ID, public URL, published_at and evidence ref")
    published = _time(receipt["published_at"])
    with _locked(root) as manifest:
        if not manifest["public_url"] or receipt["source_url"] != manifest["public_url"]:
            raise ValueError("publication receipt must match the deployed public URL")
        events = {r["event_id"]: r for r in _records(root / "injections.jsonl")}
        event = events.get(receipt["event_id"])
        if not event or published < _time(event["injected_at"]) or published > _time(_now()):
            raise ValueError("publication timestamp must follow local preparation and cannot be in the future")
        old = {r["event_id"]: r for r in _records(root / "publications.jsonl")}.get(receipt["event_id"])
        if old:
            if {k: v for k, v in old.items() if k not in {"previous_hash", "record_hash"}} != receipt:
                raise ValueError("publication receipt is already frozen")
            return old
        return _append(root / "publications.jsonl", receipt)


def _observe(root: Path, manifest: dict[str, Any], source_url: str, fetcher: Any) -> dict[str, Any]:
    tracker = LightTracker(root / "validation.sqlite3")
    try:
        people = tracker.list_people()
        if people:
            if len(people) != 1 or people[0]["canonical_name"] != manifest["display_name"]:
                raise ValueError("validation database contains an unexpected identity")
            person = people[0]
        else:
            person = tracker.add_person(
                manifest["display_name"], urls=[source_url],
                secondary_id=("controlled_validation", manifest["instance_id"]),
                profile={"test_only": True, "validation_instance": manifest["instance_id"]},
            )
        source = person["sources"][0]
        if source["url"].rstrip("/") != source_url.rstrip("/"):
            raise ValueError("validation source changed; use a new isolated validation root")
        observation = fetcher(source)
        run_id = tracker.start_run("controlled-profile-test", purpose="production")
        try:
            decision = tracker.observe(run_id, source["source_id"], observation)
            tracker.complete_run(run_id)
        except Exception:
            tracker.fail_run(run_id, {"error": "controlled profile observation failed"})
            raise
        row = tracker.db.execute(
            "SELECT observation_id FROM observations WHERE run_id=? AND source_id=?",
            (run_id, source["source_id"]),
        ).fetchone()
        result = _append(root / "observations.jsonl", {
            "run_id": run_id,
            "observation_id": row[0],
            "observed_at": observation.observed_at,
            "source_url": source_url,
            "http_status": observation.status_code,
            "transport": "real_http",
            "decision": asdict(decision),
            "body_sha256": hashlib.sha256(str(observation.body or "").encode()).hexdigest(),
        })
        return result
    finally:
        tracker.close()


def observe_profile(root: Path) -> dict[str, Any]:
    root = Path(root)
    with _locked(root) as manifest:
        if not manifest["public_url"]:
            raise ValueError("public URL not deployed; use run_local_http_smoke for local verification")
        return _observe(root, manifest, manifest["public_url"], _fetch)


def _matches_fact(decision: dict[str, Any], event: dict[str, Any]) -> bool:
    """Match the expected changed item, never arbitrary summaries/metadata."""
    if decision.get("health_status") != "healthy":
        return False
    expected_url = f"https://example.invalid/controlled/{event['event_id']}"
    normalize = lambda text: " ".join(unicodedata.normalize("NFKC", html.unescape(str(text))).split()).casefold()
    expected_title = normalize(event["title"])
    allowed = (
        {"position", "affiliation", "identity", "content", "research"}
        if event["scenario"] in CAREER_SCENARIOS else {"publication"}
    )
    for key, text_key, id_keys in (
        ("additions", "text", ("stable_id",)),
        ("modifications", "after", ("after_stable_id", "stable_id")),
    ):
        values = decision.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict) or item.get("category") not in allowed:
                continue
            if any(item.get(field) == expected_url for field in id_keys):
                return True
            text = normalize(item.get(text_key) or "")
            if text == expected_title or text.startswith(expected_title + ", published "):
                return True
    return False


def evaluate_profile(root: Path, *, at: str | None = None, evidence_source: str = "local") -> dict[str, Any]:
    root = Path(root)
    with _locked(root) as manifest:
        now = _time(at or _now())
        events = [e for e in _records(root / "injections.jsonl") if _time(e["injected_at"]) <= now]
        if evidence_source not in {"local", "remote"}:
            raise ValueError("evidence_source must be local or remote")
        observations = sorted(
            [o for o in _records(root / ("observations.jsonl" if evidence_source == "local" else "remote-observations.jsonl")) if _time(o["observed_at"]) <= now],
            key=lambda o: (_time(o["observed_at"]), o["observation_id"]),
        )
        publications = {r["event_id"]: r for r in _records(root / "publications.jsonl") if _time(r["published_at"]) <= now}
        results = []
        for index, event in enumerate(events):
            publication = publications.get(event["event_id"])
            if evidence_source == "remote" and not publication:
                results.append({"event_id": event["event_id"], "scenario": event["scenario"],
                                "injected_at": event["injected_at"], "published_at": None,
                                "event_date": event["event_date"], "status": "pending",
                                "pending_reason": "awaiting_publication_receipt", "failures": []})
                continue
            start = _time(publication["published_at"] if evidence_source == "remote" else event["injected_at"])
            following = events[index + 1] if index + 1 < len(events) else None
            following_publication = publications.get(following["event_id"]) if following else None
            end = (
                _time(following_publication["published_at"]) if evidence_source == "remote" and following_publication
                else now if evidence_source == "remote"
                else _time(following["injected_at"]) if following else now
            )
            if event["expected_page_change"]:
                matches = [o for o in observations if _time(o["observed_at"]) >= start and _matches_fact(o["decision"], event)]
                detected = next((o for o in matches if o["decision"]["status"] in {"candidate", "changed"}), None)
                confirmed = next((o for o in matches if o["decision"]["status"] == "changed"), None)
                discovery_delay = (_time(detected["observed_at"]) - start).total_seconds() if detected else None
                confirmation_delay = (_time(confirmed["observed_at"]) - start).total_seconds() if confirmed else None
                failures = []
                if discovery_delay is not None and discovery_delay > manifest["detection_sla_seconds"] or detected is None and (now-start).total_seconds() > manifest["detection_sla_seconds"]:
                    failures.append("discovery_sla_exceeded")
                if confirmation_delay is not None and confirmation_delay > manifest["confirmation_sla_seconds"] or confirmed is None and (now-start).total_seconds() > manifest["confirmation_sla_seconds"]:
                    failures.append("confirmation_sla_exceeded")
                status = "failed" if failures else "passed" if confirmed else "pending"
            else:
                period = [o for o in observations if start <= _time(o["observed_at"]) <= end]
                healthy = [o for o in period if o["decision"].get("health_status") == "healthy"]
                false_positive = any(o["decision"]["status"] in {"candidate", "changed"} for o in healthy)
                failures = ["no_change_false_positive"] if false_positive else []
                comparable = any(o["decision"]["status"] in {"unchanged", "noise"} for o in healthy)
                if not comparable and (now-start).total_seconds() > manifest["confirmation_sla_seconds"]:
                    failures.append("no_healthy_comparable_observation")
                discovery_delay = confirmation_delay = None
                detected = confirmed = None
                status = "failed" if failures else "passed" if comparable else "pending"
            results.append({
                "event_id": event["event_id"], "scenario": event["scenario"],
                "injected_at": event["injected_at"], "event_date": event["event_date"],
                "published_at": publication["published_at"] if publication else None,
                "measurement_start": "origin_publication_receipt" if evidence_source == "remote" else "local_mutation_ready",
                "measurement_start_at": start.isoformat(),
                "first_detected_at": detected["observed_at"] if detected else None,
                "confirmed_at": confirmed["observed_at"] if confirmed else None,
                "discovery_delay_seconds": discovery_delay,
                "confirmation_delay_seconds": confirmation_delay,
                "status": status, "failures": failures,
            })
        failures = sum(r["status"] == "failed" for r in results)
        pending = sum(r["status"] == "pending" for r in results)
        registration_missing = evidence_source == "remote" and not manifest.get("registration_receipt")
        publication_missing = evidence_source == "remote" and any(r.get("pending_reason") == "awaiting_publication_receipt" for r in results)
        readiness = "blocked" if registration_missing or publication_missing else "not_started" if not events or not observations else "ready"
        report = {
            "schema_version": SCHEMA, "test_only": True, "evaluated_at": now.isoformat(),
            "scope": "HTTP and ordinary tracker observation, isolated validation database" if evidence_source == "local" else "Imported target-runner observation receipts; not independently authenticated",
            "evidence_source": evidence_source,
            "remote_registration_verified": manifest["remote_registration_verified"],
            "production_scheduler_verified": False,
            "readiness": readiness,
            "status": "failed" if failures else readiness if readiness != "ready" else "pending" if pending else "passed",
            "public_report_temporal_classification_verified": False,
            "results": results,
            "failed": failures,
            "pending": pending,
        }
        _write(root / ("validation-result.json" if evidence_source == "local" else "remote-validation-result.json"), report)
        return report


def tick_profile(root: Path, *, evidence_source: str = "local") -> dict[str, Any]:
    """Bounded host tick; remote mode waits for actual external-runner receipts.

    Local mode is an independent harness. Remote mode never fetches/observes on
    behalf of the production scheduler, and cannot use local confirmations to
    advance mutations of its controlled origin.
    """
    root = Path(root)
    with _locked(root) as manifest:
        now = _now()
        if evidence_source not in {"local", "remote"}:
            raise ValueError("evidence_source must be local or remote")
        if not manifest["public_url"]:
            raise ValueError("tick requires a deployed public URL; remote enrollment is not verified")
        if evidence_source == "remote" and not manifest.get("registration_receipt"):
            raise ValueError("remote tick requires the target list readback receipt")
        if evidence_source == "local" and manifest.get("registration_receipt"):
            raise ValueError("a remotely registered origin must use remote tick evidence")
        # Establish the actual baseline before injecting any new test event.
        gate_evidence_source = evidence_source
        observations = _records(root / ("remote-observations.jsonl" if gate_evidence_source == "remote" else "observations.jsonl"))
        observations = [o for o in observations if _time(o["observed_at"]) <= _time(now)]
        has_baseline = any(o["decision"].get("health_status") == "healthy" and o["decision"]["status"] in {"baseline", "unchanged", "changed"} for o in observations)
        events = _records(root / "injections.jsonl")
        last = events[-1] if events else None
        publications = {p["event_id"]: p for p in _records(root / "publications.jsonl") if _time(p["published_at"]) <= _time(now)}
        last_publication = publications.get(last["event_id"]) if last else None
        last_start = (
            _time(last_publication["published_at"]) if evidence_source == "remote" and last_publication
            else None if evidence_source == "remote" else _time(last["injected_at"]) if last else None
        )
        if last is None:
            previous_complete = True
        elif last_start is None:
            previous_complete = False
        elif last["expected_page_change"]:
            previous_complete = any(
                _time(o["observed_at"]) >= last_start
                and o["decision"]["status"] == "changed"
                and _matches_fact(o["decision"], last)
                for o in observations
            )
        else:
            previous_complete = any(
                _time(o["observed_at"]) >= last_start
                and o["decision"].get("health_status") == "healthy"
                and o["decision"]["status"] in {"unchanged", "noise"}
                for o in observations
            )
        injection = None
        if has_baseline and previous_complete and _time(now) >= _time(manifest["next_injection_at"]):
            rotation = ("startup", "job_change", "no_change") if evidence_source == "remote" else SCENARIOS
            scenario = rotation[manifest["sequence"] % len(rotation)]
            injection = _advance(root, manifest, scenario, now)
        observation = (
            _observe(root, manifest, manifest["public_url"], _fetch)
            if evidence_source == "local" else None
        )
    evaluation = evaluate_profile(root, evidence_source=gate_evidence_source)
    return {"injection": injection, "observation": observation, "evaluation": evaluation,
            "execution_scope": "local_harness" if evidence_source == "local" else "external_target_runner_receipts",
            "stage": "awaiting_remote_baseline" if evidence_source == "remote" and not has_baseline else "awaiting_publication_receipt" if evidence_source == "remote" and (injection or last and not last_publication) else "waiting_for_previous_confirmation" if not previous_complete else "observed" if observation else "waiting_for_next_injection",
            "publication_required": evidence_source == "remote" and injection is not None,
            "publish_directory": str(root / "public"),
            "production_scheduler_verified": False, "injection_gate_evidence_source": gate_evidence_source,
            "injection_waiting_for_previous_confirmation": not previous_complete,
            "case_failed_stalled": not previous_complete and bool(evaluation["failed"])}


@contextmanager
def local_profile_server(root: Path) -> Iterator[ThreadingHTTPServer]:
    """Exact loopback test endpoint; never weakens the public fetch safety policy."""
    root = Path(root)
    with _locked(root):
        pass

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in {"/", "/index.html"}:
                self.send_error(404)
                return
            body = (root / "public/index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def run_local_http_smoke(root: Path, *, subject_name: str = "Synthetic Validation User") -> dict[str, Any]:
    """Actual local HTTP baseline/candidate/confirmation and historical controls."""
    root = Path(root)
    init_profile(root, subject_name=subject_name)
    if (root / "validation.sqlite3").exists():
        raise ValueError("local smoke needs a fresh validation root")
    with local_profile_server(root) as server:
        url = f"http://127.0.0.1:{server.server_port}/"
        opener = build_opener(ProxyHandler({}))

        def fetch_exact_local(_):
            # URL is constructed solely from this owned in-process server.
            with opener.open(Request(url), timeout=5) as response:
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024 or response.geturl() != url:
                    raise ValueError("unexpected controlled-profile response")
                return FetchObservation(body=body, status_code=response.status, final_url=url)

        def observe():
            with _locked(root) as manifest:
                return _observe(root, manifest, url, fetch_exact_local)

        decisions = [observe()["decision"]["status"]]
        for scenario in SCENARIOS:
            advance_profile(root, scenario=scenario)
            decisions.extend(observe()["decision"]["status"] for _ in range(2))
        expected = ["baseline", "candidate", "changed", "candidate", "changed", "candidate", "changed", "unchanged", "unchanged"]
        if decisions != expected:
            raise RuntimeError(f"controlled HTTP validation failed: {decisions}")
    evaluation = evaluate_profile(root)
    if evaluation["failed"] or evaluation["pending"]:
        raise RuntimeError("controlled profile ground-truth evaluation did not pass")
    return {"local_http_verified": True, "decisions": decisions, "evaluation": evaluation,
            "remote_registration_verified": False, "homepage": str(root / "public/index.html")}
