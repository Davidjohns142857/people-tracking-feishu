"""Production scan receipts for explicitly marked control profiles."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from tempfile import NamedTemporaryFile
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import __version__

PREFIX = "[TEST] "
SETTINGS = "control_debug_settings"
FIRST = "control-debug:first"
VERSION = rb"CTRL-[a-f0-9]{8}-[0-9]{5,}"
MARKER = re.compile(rb'\bdata-people-tracking-debug-version=["\'](' + VERSION + rb')["\']')


def is_debug_name(name: str) -> bool:
    return name.upper().startswith(PREFIX)


def record_fetch(state, source: dict, started: str, observation) -> None:
    """Save the transport result, even if parsing later rejects the page."""
    key = "control_debug_fetch:" + source["source_id"]
    previous = state.get_meta(key, {})
    result = {
        "due_at": source.get("next_check_at"),
        "attempted_at": started,
        "response_at": observation.observed_at if observation else None,
        "http_status": observation.status_code if observation else None,
        "received_version": None,
        "last_version_receipt": previous.get("last_version_receipt"),
    }
    if observation and observation.status_code == 200:
        body = observation.body
        if isinstance(body, str):
            body = body.encode()
        match = MARKER.search(body) if isinstance(body, bytes) else None
        if match:
            version = match[1].decode()
            result["received_version"] = version
            result["last_version_receipt"] = {
                "version": version, "received_at": observation.observed_at,
                "body_sha256": hashlib.sha256(body).hexdigest(),
            }
    state.set_meta(key, result)


def _version_progress(db, source_id: str, receipt: dict | None) -> dict:
    progress = {
        "first_detected_at": None, "first_detection_observation_id": None,
        "confirmed_at": None, "confirmation_observation_id": None, "fact_review": None,
    }
    if not receipt:
        return progress
    version = receipt["version"]
    # The full-body receipt is post-transport. A delta must contain the matching
    # token in the actual changed item, and not merely in a report summary.
    for row in db.execute(
        """SELECT observation_id, observed_at, decision_status, delta_json
           FROM observations WHERE source_id=? AND health_status='healthy'
           AND decision_status IN ('candidate','changed')
           AND instr(delta_json,?)>0
           ORDER BY julianday(observed_at), observation_id""",
        (source_id, version),
    ):
        delta = json.loads(row["delta_json"])
        items = [
            (item, field)
            for group, field in (("additions", "text"), ("modifications", "after"))
            for item in delta.get(group, [])
            if isinstance(item, dict)
        ]
        if not any(re.search(r"\b" + re.escape(version) + r"\b", str(item.get(field, "")))
                   for item, field in items):
            continue
        if not progress["first_detected_at"]:
            progress["first_detected_at"] = row["observed_at"]
            progress["first_detection_observation_id"] = row["observation_id"]
        if row["decision_status"] == "changed" and not progress["confirmed_at"]:
            progress["confirmed_at"] = row["observed_at"]
            progress["confirmation_observation_id"] = row["observation_id"]
    review = db.execute(
        """SELECT e.disposition,e.updated_at
           FROM event_ledger e WHERE e.source_id=? AND e.event_type='person_signal'
           AND json_extract(e.payload_json,'$.debug_only')=1
           AND instr(COALESCE(json_extract(e.payload_json,'$.raw_item.after'),
                              json_extract(e.payload_json,'$.raw_item.text'),''),?)>0
           ORDER BY julianday(e.updated_at) DESC LIMIT 1""",
        (source_id, version),
    ).fetchone()
    if review:
        progress["fact_review"] = dict(review)
    return progress


def status(state, *, now: datetime, timezone_name: str, tick: dict | None = None) -> dict:
    db = state.db
    cutoff = (now - timedelta(hours=84)).isoformat()
    active = db.execute(
        """SELECT COUNT(DISTINCT s.person_key), COUNT(*) FROM sources s
           JOIN people p USING(person_key)
           WHERE s.tracking_enabled=1 AND p.canonical_name NOT LIKE '[TEST] %'"""
    ).fetchone()
    seen = db.execute(
        """SELECT COUNT(DISTINCT s.person_key),COUNT(DISTINCT o.source_id),
                  COUNT(DISTINCT CASE WHEN o.health_status='healthy' AND
                      o.decision_status IN ('unchanged','noise','candidate','changed')
                      THEN o.source_id END)
           FROM observations o JOIN sources s USING(source_id)
           JOIN people p USING(person_key) JOIN runs r USING(run_id)
           WHERE r.purpose='production' AND p.canonical_name NOT LIKE '[TEST] %'
           AND julianday(o.observed_at)>julianday(?)
           AND julianday(o.observed_at)<=julianday(?)""",
        (cutoff, now.isoformat()),
    ).fetchone()
    last = db.execute(
        """SELECT status,started_at,completed_at FROM runs WHERE purpose='production'
           AND julianday(started_at)<=julianday(?) ORDER BY julianday(started_at) DESC LIMIT 1""",
        (now.isoformat(),),
    ).fetchone()
    sources = []
    for row in db.execute(
        """SELECT s.source_id,s.kind,s.tracking_enabled,s.next_check_at,s.health_status,
                  s.last_checked_at,s.last_success_at,s.candidate_count,
                  s.candidate_first_seen_at,s.last_changed_at,
                  (SELECT decision_status FROM observations o WHERE o.source_id=s.source_id
                   ORDER BY julianday(observed_at) DESC,observation_id DESC LIMIT 1) AS latest_decision,
                  EXISTS(SELECT 1 FROM source_roster_records rr
                         WHERE rr.person_key=s.person_key AND rr.status='active') AS roster_active
           FROM sources s JOIN people p USING(person_key)
           WHERE p.canonical_name LIKE '[TEST] %' AND s.kind='homepage'
           ORDER BY s.source_id LIMIT 8"""
    ):
        source = dict(row)
        fetch = state.get_meta("control_debug_fetch:" + source["source_id"], {})
        source["fetch"] = fetch
        source.update(_version_progress(db, source["source_id"], fetch.get("last_version_receipt")))
        sources.append(source)
    flags = ("roster_sync_failed", "roster_bridge_waiting", "master_bridge_waiting",
             "validation_failed", "reviews_waiting")
    return {
        "version": __version__, "timezone": timezone_name,
        "generated_at": now.isoformat(), "coverage_since": cutoff,
        "coverage": dict(zip(
            ("enabled_people", "enabled_sources", "observed_people",
             "observed_sources", "healthy_comparable_sources"), (*active, *seen)
        )),
        "last_run": dict(last) if last else None,
        "control_sources": sources,
        "tick_blockers": [flag for flag in flags if tick and tick.get(flag)],
        "scope": "production database observations; excludes test profiles from coverage",
    }


def markdown(report: dict) -> str:
    zone = ZoneInfo(report["timezone"])

    def when(value):
        return (datetime.fromisoformat(value).astimezone(zone).strftime("%m-%d %H:%M")
                if value else "无记录")

    c = report["coverage"]
    last = report["last_run"] or {}
    lines = [
        "**人员跟踪 Debug 回执**",
        f"{when(report['generated_at'])}（{zone.key}） · {report['version']}",
        f"过去 84 小时：观察 {c['observed_people']} 人、{c['observed_sources']} 个来源，"
        f"可健康比较 {c['healthy_comparable_sources']} 个。当前启用 {c['enabled_people']} 人。",
        f"最近扫描：{when(last.get('started_at'))}（{last.get('status', '无记录')}）。",
    ]
    if report["tick_blockers"]:
        lines.append("本轮待处理：" + "、".join(report["tick_blockers"]))
    if not report["control_sources"]:
        lines.append("控制主页待接入，尚无线上测试回执。")
    for source in report["control_sources"]:
        fetch = source["fetch"]
        receipt = fetch.get("last_version_receipt") or {}
        review = source["fact_review"] or {}
        lines.append(
            f"- {source['source_id']} · {'已启用' if source['tracking_enabled'] else '停用'}；"
            f"上次到期 {when(fetch.get('due_at'))}，下次 {when(source['next_check_at'])}；"
            f"最近请求 {when(fetch.get('attempted_at'))} "
            f"HTTP {fetch.get('http_status') or '—'}；最近读取版本 {receipt.get('version', '—')} "
            f"({when(receipt.get('received_at'))})；首次发现 {when(source['first_detected_at'])}；"
            f"确认 {when(source['confirmed_at'])}；事实审核 {review.get('disposition', '无记录')}；"
            f"最近观察 {source['latest_decision'] or '无记录'}。"
        )
    return "\n".join(lines)


def export_control_observations(state, config: dict, source_id: str) -> dict:
    """Return only the ledger and list readback for one controlled homepage."""
    db = state.db
    source = db.execute(
        """SELECT s.url,s.person_key FROM sources s JOIN people p USING(person_key)
           WHERE s.source_id=? AND s.kind='homepage' AND p.canonical_name LIKE '[TEST] %'""",
        (source_id,),
    ).fetchone()
    if not source:
        raise ValueError("source_id must identify a tracked [TEST] homepage")
    ref = (config.get("master_database") or {}).get("authoritative_source_ref")
    records = db.execute(
        """SELECT source_record_id,last_seen_at FROM source_roster_records rr
           WHERE rr.person_key=? AND rr.status='active' AND (? IS NULL OR rr.source_ref=?)
           AND EXISTS(SELECT 1 FROM json_each(rr.source_ids_json) WHERE value=?)
           LIMIT 2""",
        (source["person_key"], ref, ref, source_id),
    ).fetchall()
    if len(records) != 1:
        return {"status": "missing_or_ambiguous_list_readback", "observations": []}
    record = records[0]
    common = {
        "source_id": source_id, "source_url": source["url"],
        "record_id": record["source_record_id"],
    }
    observations = []
    for row in db.execute(
        """SELECT o.observation_id,o.run_id,o.observed_at,o.health_status,
                  o.decision_status,o.delta_json FROM observations o JOIN runs r USING(run_id)
           WHERE o.source_id=? AND r.purpose='production' AND r.status IN ('completed','partial')
           ORDER BY julianday(o.observed_at) DESC,o.observation_id DESC LIMIT 60""",
        (source_id,),
    ):
        delta = json.loads(row["delta_json"])
        decision = {"status": row["decision_status"], "health_status": row["health_status"]}
        for group in ("additions", "modifications", "removals"):
            decision[group] = [
                {k: item[k][:700] for k in ("category", "text", "before", "after",
                                           "stable_id", "after_stable_id")
                 if isinstance(item.get(k), str)}
                for item in delta.get(group, [])
                if isinstance(item, dict) and re.search(VERSION, json.dumps(item).encode())
            ][:12]
        observations.append({
            **common, "observation_id": row["observation_id"], "run_id": row["run_id"],
            "observed_at": row["observed_at"], "decision": decision,
            "evidence_ref": "production-observation:" + row["observation_id"],
        })
    return {
        "status": "ready",
        "registration": {**common, "readback_at": record["last_seen_at"],
                         "evidence_ref": "production-roster:" + record["source_record_id"]},
        "observations": list(reversed(observations)),
    }


def recipient(config: dict, settings: dict) -> dict | None:
    message = settings.get("target") or (config.get("outputs", {}).get("developer", {}).get("message") or {})
    if not message.get("enabled") or message.get("target_kind") not in {"chat", "user"} or not message.get("target_id"):
        return None
    public = config.get("outputs", {}).get("public", {}).get("message") or {}
    target = {key: message[key] for key in ("enabled", "target_kind", "target_id")}
    if public.get("enabled") and all(public.get(key) == target[key] for key in ("target_kind", "target_id")):
        raise ValueError("debug delivery requires a separate private developer destination")
    return target


def _current_slot(now: datetime, zone: ZoneInfo) -> datetime:
    local = now.astimezone(zone)
    monday = local.date() - timedelta(days=local.weekday())
    at = datetime.combine(monday, time(8), zone)
    return max(slot for slot in (at - timedelta(hours=84), at, at + timedelta(hours=84)) if slot <= local)


def prepare(state, config: dict, *, now: datetime, tick: dict | None = None) -> dict | None:
    settings = state.get_meta(SETTINGS, {})
    if not settings.get("enabled"):
        return None
    target = recipient(config, settings)
    if not target:
        if not settings.get("use_existing_outputs"):
            return {"status": "developer_target_missing", "actions": []}
        document = config.get("outputs", {}).get("developer", {}).get("document") or {}
        target = {"control_destination": "document", "document": document} if document.get("enabled") else {
            "control_destination": "local"
        }
    timezone_name = config["schedule"]["timezone"]
    first = state.delivery(FIRST)
    pending = state.db.execute(
        """SELECT delivery_key FROM delivery_outbox WHERE audience='developer'
           AND output_kind='control_debug' AND status!='completed'
           ORDER BY created_at LIMIT 1"""
    ).fetchone()
    if pending:
        key = pending["delivery_key"]
    elif not first:
        key = FIRST
    else:
        first_report = state.get_meta(FIRST) or {}
        slot = _current_slot(now, ZoneInfo(timezone_name))
        if slot <= datetime.fromisoformat(first_report["generated_at"]):
            return None
        key = "control-debug:" + slot.astimezone(timezone.utc).strftime("%Y%m%dT%H%MZ")
        if state.delivery(key):
            return None
    saved = state.get_meta(key)
    if not saved:
        report = status(state, now=now, timezone_name=timezone_name, tick=tick)
        body = markdown(report)
        saved = {
            "generated_at": report["generated_at"], "markdown": body,
            "message_hash": hashlib.sha256(body.encode()).hexdigest(),
            "target": target,
        }
        # Freeze the first prepared body across retries of the same window.
        state.db.execute(
            "INSERT OR IGNORE INTO portable_meta(key,value_json,updated_at) VALUES(?,?,?)",
            (key, json.dumps(saved, ensure_ascii=False), now.isoformat()),
        )
        state.db.commit()
        saved = state.get_meta(key)
    if saved["target"] != target:
        return {"status": "pending_target_changed", "actions": []}
    destination = target.get("control_destination", "message")
    report_path = ""
    if destination in {"local", "document"}:
        directory = state.path.parent / "reports" / "control-debug"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (key.removeprefix("control-debug:") + ".md")
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as stream:
            stream.write(saved["markdown"] + "\n")
        Path(stream.name).replace(path)
        report_path = str(path)
    delivery = state.prepare_delivery(
        key=key, output_kind="control_debug", period=key.removeprefix("control-debug:"),
        title="人员跟踪 Debug 回执", report_path=report_path, audience="developer",
    )
    if delivery["status"] == "completed":
        return None
    if destination == "local":
        state.update_delivery(key, status="completed")
        return {"status": "saved_to_existing_developer_reports", "delivery_key": key,
                "report_path": report_path, "actions": []}
    if destination == "document":
        return {"status": "waiting_for_receipt", "delivery_key": key, "actions": [{
            "adapter": "agent_tool_bridge", "operation": "create_document",
            "audience": "developer", "target": target["document"], "identity": "bot",
            "idempotency_key": key + ":doc", "delivery_key": key,
            "message_hash": saved["message_hash"], "title": delivery["title"],
            "markdown_file": report_path,
        }]}
    return {"status": "waiting_for_receipt", "delivery_key": key, "actions": [{
        "adapter": "agent_tool_bridge", "operation": "send_current_or_configured_message",
        "audience": "developer", "target": target, "identity": "bot",
        "idempotency_key": key + ":message", "delivery_key": key,
        "message_hash": saved["message_hash"], "markdown": saved["markdown"],
    }]}


def apply_receipt(state, payload: dict) -> dict:
    key = payload.get("delivery_key")
    delivery = state.delivery(key) if isinstance(key, str) else None
    report = state.get_meta(key) if delivery and delivery["output_kind"] == "control_debug" else None
    if not report or report["message_hash"] != payload.get("message_hash"):
        raise ValueError("debug receipt does not match a frozen delivery")
    if report["target"].get("control_destination") == "document":
        if set(payload) != {"delivery_key", "doc_url", "message_hash"}:
            raise ValueError("debug document receipt requires delivery_key, doc_url and message_hash")
        doc_url = payload["doc_url"]
        if delivery["doc_url"] and delivery["doc_url"] != doc_url:
            raise ValueError("debug receipt cannot replace a previous document")
        return delivery if delivery["status"] == "completed" else state.update_delivery(
            key, doc_url=doc_url, status="completed"
        )
    if set(payload) != {"delivery_key", "message_id", "message_hash"}:
        raise ValueError("debug receipt requires delivery_key, message_id and message_hash")
    message_id = payload["message_id"]
    if not isinstance(message_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id):
        raise ValueError("debug receipt requires a valid message ID")
    if delivery["message_id"] and delivery["message_id"] != message_id:
        raise ValueError("debug receipt cannot replace a previous message")
    if delivery["status"] == "completed":
        return delivery
    return state.update_delivery(key, message_id=message_id, status="completed")
