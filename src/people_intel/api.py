from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from people_intel.commands import CommandProcessor
from people_intel.api_security import ApiSecurityConfig
from people_intel.connector_jobs import ConnectorJobQueue, build_scan_request_handler
from people_intel.connector_policy import connector_policy_definitions
from people_intel.connector_scheduler import (
    ConnectorScheduleProducer,
    load_connector_schedule,
    merge_person_profile_subscriptions,
)
from people_intel.connector_runtime import execute_connector_run
from people_intel.connectors import ConnectorExecutor
from people_intel.content_store import ContentAddressedStore
from people_intel.demo import DemoController, SCENARIO, build_memory_graph
from people_intel.events import EventBroker
from people_intel.importers import AppleScholarsMarkdownImporter, IcmlPeopleMarkdownImporter
from people_intel.ledger import InMemoryLedger, LedgerNotFound
from people_intel.memory_architecture import MEMORY_ARCHITECTURE
from people_intel.person_profiles import PersonProfileService
from people_intel.platform_tracking import (
    AdversarialEvaluationReport,
    PLATFORM_TRACKING_DEFINITION,
    PlatformTrackingDefinition,
    evaluate_adversarial_fixture,
)
from people_intel.tracking_benchmark import (
    LiveSmokeManifest,
    TrackingBenchmarkDefinition,
    TrackingBenchmarkReport,
    evaluate_tracking_benchmark,
    load_live_smoke_manifest,
    tracking_benchmark_definition,
)
from people_intel.person_profile_schemas import (
    PersonCogneeProjection,
    PersonCogneeRecallRequest,
    PersonCogneeRecallResponse,
    PersonDigestBatch,
    PersonGraphComparison,
    PersonProfileImportRequest,
    PersonProfileImportRun,
    PersonProfileRevision,
    PersonProfileScanRequest,
    PersonProfileScanResponse,
    PersonProfileScanRun,
    PersonProfileView,
    PersonPlatformAccount,
    PersonSearchPlanRevision,
    PersonUpdateBundle,
    ProfilePatchReviewBatchView,
    ProfilePatchReviewDecisionRequest,
)
from people_intel.observability import (
    CAPABILITIES,
    collection_page,
    component_statuses,
    json_hash,
    latest_verification,
    latest_snapshot_hash,
    ledger_counts,
    ledger_metrics,
)
from people_intel.openclaw_integration import (
    OpenClawDocumentImportRequest,
    OpenClawDocumentImportResponse,
    OpenClawIntegrationManifest,
    OpenClawSchedulerTickRequest,
    OpenClawSchedulerTickResponse,
    integration_manifest,
)
from people_intel.ontology import OntologyRegistry
from people_intel.schemas import (
    Annotation,
    AnnotationRequest,
    CogneeBatchProjectionRequest,
    CogneeBatchProjectionResponse,
    CogneeRecallRequest,
    CogneeRecallResponse,
    CogneeRuntimeStatus,
    CognifyRequest,
    CognifyResponse,
    CommandEnvelope,
    CommandReceipt,
    ConnectorRunRequest,
    ConnectorRunResponse,
    ConnectorJobEnqueueRequest,
    ConnectorJobEvent,
    ConnectorJobView,
    ConnectorPolicyDefinition,
    ConnectorScheduleConfig,
    ConsolidatedReviewDocument,
    DemoRunRequest,
    DemoSession,
    DeriveRequest,
    GraphQuery,
    GraphView,
    ExtractionRun,
    InitialReviewBatchView,
    InitialReviewDecision,
    InitialReviewDecisionRequest,
    InitialKnowledgeImportRequest,
    InitialKnowledgeImportResponse,
    DirectoryWatchScanRequest,
    DirectoryWatchScanResponse,
    FeishuDocumentScanRequest,
    FeishuDocumentScanResponse,
    FetchMethod,
    KnowledgeJourney,
    MemoryArchitectureDefinition,
    MemoryGraph,
    ReviewDeliveryPage,
    ReviewMessageCommandRequest,
    ReviewMessageCommandResult,
    ReviewBulkDecisionRequest,
    ReviewBulkDecisionResponse,
    ScenarioStepResult,
    ScanRun,
    SourceDocumentVersion,
    SourceIngestionRequest,
    SourceIngestionResponse,
    SourceType,
    SystemManifest,
    StorySession,
    StorySessionCreate,
    StoryStepResult,
    WorkflowArtifact,
    WorkflowDefinition,
    WorkingView,
    utc_now,
)
from people_intel.service import MemoryValidationError, TemporalMemoryService
from people_intel.source_tracking import DirectorySourceTracker
from people_intel.workflow import StoryController, WORKFLOW_DEFINITION


APP_VERSION = "0.3.0"
PROJECT_ROOT = Path(
    os.environ.get("PEOPLE_INTEL_PROJECT_ROOT", Path(__file__).resolve().parents[2])
).resolve()


_AUTO_COGNEE = object()


def build_sandbox_service(
    object_root: str | Path | None = None,
    *,
    cognify_adapter: Any = _AUTO_COGNEE,
) -> TemporalMemoryService:
    root = Path(object_root or os.environ.get("PEOPLE_INTEL_OBJECT_ROOT", ".people_intel/objects")).resolve()
    if cognify_adapter is _AUTO_COGNEE:
        from people_intel.adapters.cognee import CogneeAdapter, CogneeSandboxConfig

        cognee_root = root.parent / "cognee"
        cognify_adapter = CogneeAdapter(CogneeSandboxConfig.from_environment(cognee_root))
    return TemporalMemoryService(
        InMemoryLedger(), ContentAddressedStore(root), OntologyRegistry.default(), cognify_adapter=cognify_adapter
    )


def build_live_service() -> TemporalMemoryService | None:
    database_url = os.environ.get("PEOPLE_INTEL_DATABASE_URL")
    if not database_url:
        return None
    from people_intel.adapters.postgres import PostgresLedger

    return TemporalMemoryService(
        PostgresLedger(database_url),
        ContentAddressedStore(Path(os.environ.get("PEOPLE_INTEL_OBJECT_ROOT", ".people_intel/objects"))),
        OntologyRegistry.default(),
    )


def build_default_service() -> TemporalMemoryService:
    return build_live_service() or build_sandbox_service()


def create_app(
    service: TemporalMemoryService | None = None,
    *,
    live_service: TemporalMemoryService | None = None,
    allow_live_writes: bool = False,
    serve_frontend: bool = True,
    enable_demo: bool = True,
) -> FastAPI:
    sandbox = service or build_sandbox_service()
    broker = EventBroker()
    app = FastAPI(
        title="People Intelligence Temporal Memory",
        version=APP_VERSION,
        description="Append-only source memory, bitemporal assertions, and an observable MVP cockpit.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    api_security = ApiSecurityConfig.from_environment()
    app.state.api_security = api_security

    @app.middleware("http")
    async def protect_api(request: Request, call_next):
        if request.url.path.startswith("/v1/"):
            if request.method != "OPTIONS" and not api_security.accepts(
                request.headers.get("authorization")
            ):
                return JSONResponse(
                    {"detail": "valid Bearer token required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if request.method in {"POST", "PUT", "PATCH"}:
                content_length = request.headers.get("content-length")
                if content_length:
                    try:
                        too_large = int(content_length) > api_security.max_request_bytes
                    except ValueError:
                        too_large = True
                    if too_large:
                        return JSONResponse(
                            {"detail": "request body exceeds configured limit"},
                            status_code=413,
                        )
        response = await call_next(request)
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
        return response
    app.state.memory = sandbox
    app.state.live_memory = live_service
    app.state.allow_live_writes = allow_live_writes
    app.state.events = broker
    app.state.demo_enabled = enable_demo
    app.state.demo = DemoController(sandbox, broker, PROJECT_ROOT) if enable_demo else None
    app.state.stories = StoryController(sandbox, broker, PROJECT_ROOT) if enable_demo else None
    app.state.connectors = ConnectorExecutor(sandbox.object_store)

    def make_scan_handler(memory: TemporalMemoryService):
        return build_scan_request_handler(
            memory,
            publish=lambda event_type, aggregate_type, aggregate_id, payload: broker.publish(
                event_type, aggregate_type, aggregate_id, payload=payload
            ),
        )

    app.state.commands = CommandProcessor(sandbox, scan_request_handler=make_scan_handler(sandbox))
    app.state.make_scan_handler = make_scan_handler

    def memory_for(request: Request, *, write: bool = False) -> TemporalMemoryService:
        environment = request.headers.get("x-people-intel-environment", "sandbox")
        if environment == "sandbox":
            return app.state.memory
        if environment != "live":
            raise HTTPException(status_code=400, detail="environment must be sandbox or live")
        if app.state.live_memory is None:
            raise HTTPException(status_code=503, detail="live PostgreSQL is not configured")
        if write and not app.state.allow_live_writes:
            raise HTTPException(status_code=403, detail="live ledger is read-only in the demo cockpit")
        return app.state.live_memory

    def profiles_for(request: Request, *, write: bool = False) -> PersonProfileService:
        return PersonProfileService(memory_for(request, write=write))

    def stories_for_demo() -> StoryController:
        if app.state.stories is None:
            raise HTTPException(status_code=404, detail="demo endpoints are disabled")
        return app.state.stories

    def controller_for_demo() -> DemoController:
        if app.state.demo is None:
            raise HTTPException(status_code=404, detail="demo endpoints are disabled")
        return app.state.demo

    def connectors_for_runtime() -> ConnectorExecutor:
        # Keep demo injection/test compatibility while production has no fixture dependency.
        return (
            app.state.stories.connectors
            if app.state.stories is not None
            else app.state.connectors
        )

    def publish(event_type: str, aggregate_type: str, aggregate_id: str | None, payload: dict[str, Any] | None = None):
        return broker.publish(event_type, aggregate_type, aggregate_id, payload=payload)

    @app.exception_handler(LedgerNotFound)
    async def not_found_handler(_, exc: LedgerNotFound):
        return PlainTextResponse(f"not found: {exc}", status_code=404)

    @app.exception_handler(MemoryValidationError)
    async def validation_handler(_, exc: MemoryValidationError):
        return PlainTextResponse(str(exc), status_code=422)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": APP_VERSION, "ontology_version": app.state.memory.ontology.version}

    @app.get(
        "/v1/platform-tracking/definition",
        response_model=PlatformTrackingDefinition,
        tags=["platform tracking"],
    )
    def platform_tracking_definition() -> PlatformTrackingDefinition:
        """Return the versioned X, XHS and GitHub identity/tracking contract."""
        return PLATFORM_TRACKING_DEFINITION

    @app.get(
        "/v1/platform-tracking/adversarial-evaluation",
        response_model=AdversarialEvaluationReport,
        tags=["platform tracking"],
    )
    def platform_tracking_adversarial_evaluation() -> AdversarialEvaluationReport:
        """Replay the six real-evidence cases through all three algorithm rounds."""
        return evaluate_adversarial_fixture(
            PROJECT_ROOT / "fixtures" / "platform_adversarial_cases.json"
        )

    @app.get(
        "/v1/openclaw/manifest",
        response_model=OpenClawIntegrationManifest,
        tags=["openclaw integration"],
    )
    def openclaw_manifest() -> OpenClawIntegrationManifest:
        """Return the versioned OpenClaw/Feishu workflow and security contract."""
        return integration_manifest(app.state.api_security.public_status())

    @app.post(
        "/v1/openclaw/documents/import",
        response_model=OpenClawDocumentImportResponse,
        status_code=201,
        tags=["openclaw integration"],
    )
    def openclaw_import_document(
        request: Request,
        payload: OpenClawDocumentImportRequest,
    ) -> OpenClawDocumentImportResponse:
        """Import normalized Feishu content without allowing the agent to bypass review."""
        memory = memory_for(request, write=True)
        lowered = f"{payload.source_uri}\n{payload.content[:20000]}".casefold()
        if payload.importer == "auto":
            if any(marker in lowered for marker in ("apple scholars", "apple scholar", "苹果 ai_ml 博士生学者")):
                selected = "apple_scholars"
            elif "icml 2026" in lowered and any(marker in lowered for marker in ("获奖", "award", "贡献者")):
                selected = "icml_people"
            else:
                selected = "generic"
        else:
            selected = payload.importer

        review_batch_ids: list[str] = []
        imported_people = 0
        if selected == "apple_scholars":
            report = AppleScholarsMarkdownImporter(memory).import_text(
                payload.content,
                source_uri=payload.source_uri,
                observed_at=payload.observed_at,
                created_by=payload.actor_id,
                normalized_markdown=payload.normalized_markdown,
            )
            source_version_id = report.source_version_id
            version_status = report.version_status
            review_batch_ids = [report.review_batch_id]
            imported_people = report.counts["people"]
        elif selected == "icml_people":
            report = IcmlPeopleMarkdownImporter(memory).import_text(
                payload.content,
                source_uri=payload.source_uri,
                observed_at=payload.observed_at,
                created_by=payload.actor_id,
                normalized_markdown=payload.normalized_markdown,
            )
            source_version_id = report.source_version_id
            version_status = report.version_status
            review_batch_ids = [report.review_batch_id]
            imported_people = report.counts["people"]
        else:
            ingested = memory.ingest_source(SourceIngestionRequest(
                source_uri=payload.source_uri,
                source_type=SourceType.FEISHU,
                media_type="text/markdown",
                content=payload.content,
                normalized_markdown=payload.normalized_markdown,
                retrieved_at=payload.observed_at,
                fetch_method=FetchMethod.FEISHU,
                metadata={
                    "integration": "openclaw",
                    "command_id": payload.command_id,
                    "actor_id": payload.actor_id,
                    "extraction_status": "pending",
                },
            ))
            source_version_id = ingested.source_version_id
            version_status = ingested.version_status

        profile_run = None
        if payload.create_person_profiles and imported_people:
            profile_run = PersonProfileService(memory).backfill(PersonProfileImportRequest(
                command_id=f"{payload.command_id}:profiles",
                source_version_ids=[source_version_id],
                include_all_existing=False,
                bootstrap_demo_fixture=False,
                created_by=payload.actor_id,
            ))
            review_batch_ids = list(dict.fromkeys(review_batch_ids + profile_run.review_batch_ids))
        status = (
            "unchanged"
            if version_status == "unchanged"
            else ("imported_for_review" if imported_people else "stored_pending_extraction")
        )
        result = OpenClawDocumentImportResponse(
            command_id=payload.command_id,
            selected_importer=selected,
            status=status,
            source_version_id=source_version_id,
            version_status=version_status,
            review_batch_ids=review_batch_ids,
            imported_people=imported_people,
            profile_import_run_id=profile_run.import_run_id if profile_run else None,
            person_keys=profile_run.person_keys if profile_run else [],
            next_actions_zh=(
                [
                    "读取 review_batch_id 的 Feishu delivery page。",
                    "向用户展示直接事实、派生推定和批量确认数量。",
                    "审核后再启用 SearchPlan 对应的周期扫描。",
                ]
                if imported_people
                else [
                    "原文已不可变保存，但通用结构尚无可靠人物边界。",
                    "请求用户选择专用导入器，或将该来源送入集中补抽取/审核。",
                    "不得从全局语义相似结果反推人物。",
                ]
            ),
        )
        publish(
            "openclaw.document.imported",
            "source",
            source_version_id,
            {
                "command_id": payload.command_id,
                "selected_importer": selected,
                "status": status,
                "review_batch_ids": review_batch_ids,
            },
        )
        return result

    @app.post(
        "/v1/openclaw/scheduler/tick",
        response_model=OpenClawSchedulerTickResponse,
        tags=["openclaw integration"],
    )
    def openclaw_scheduler_tick(
        request: Request,
        payload: OpenClawSchedulerTickRequest,
    ) -> OpenClawSchedulerTickResponse:
        """Evaluate due SearchPlan subscriptions and enqueue idempotent jobs."""
        memory = memory_for(request, write=not payload.dry_run)
        config = merge_person_profile_subscriptions(
            memory,
            load_connector_schedule(PROJECT_ROOT / "config" / "connector-schedule.json"),
        )
        result = ConnectorScheduleProducer(memory, config).run_once(
            now=payload.evaluated_at,
            dry_run=payload.dry_run,
        )
        response = OpenClawSchedulerTickResponse(
            command_id=payload.command_id,
            dry_run=payload.dry_run,
            subscription_count=result.subscription_count,
            due_count=result.due_count,
            enqueued_count=result.enqueued_count,
            existing_count=result.existing_count,
            disabled_count=result.disabled_count,
            connector_job_ids=result.connector_job_ids,
        )
        publish(
            "openclaw.scheduler.evaluated",
            "connector_schedule",
            payload.command_id,
            response.model_dump(mode="json"),
        )
        return response

    @app.get(
        "/v1/tracking-benchmark/definition",
        response_model=TrackingBenchmarkDefinition,
        tags=["tracking benchmark"],
    )
    def tracking_monitoring_benchmark_definition() -> TrackingBenchmarkDefinition:
        """Return the normalized 42-channel + 48-workflow adversarial contract."""
        return tracking_benchmark_definition()

    @app.get(
        "/v1/tracking-benchmark/evaluation",
        response_model=TrackingBenchmarkReport,
        tags=["tracking benchmark"],
    )
    def tracking_monitoring_benchmark_evaluation() -> TrackingBenchmarkReport:
        """Evaluate all 90 public-evidence cases over the three algorithm rounds."""
        return evaluate_tracking_benchmark()

    @app.get(
        "/v1/tracking-benchmark/live-smoke",
        response_model=LiveSmokeManifest,
        tags=["tracking benchmark"],
    )
    def tracking_monitoring_live_smoke() -> LiveSmokeManifest:
        """Return the latest low-frequency external connector smoke manifest."""
        manifest = load_live_smoke_manifest(
            PROJECT_ROOT / ".people_intel" / "tracking-benchmark-live-smoke.json"
        )
        if manifest is None:
            raise HTTPException(status_code=404, detail="live smoke has not been run")
        return manifest

    @app.get("/v1/system/manifest", response_model=SystemManifest)
    def system_manifest(request: Request) -> SystemManifest:
        memory = memory_for(request)
        environment = (
            ("live-write" if app.state.allow_live_writes else "live-readonly")
            if memory is app.state.live_memory
            else "sandbox"
        )
        session = app.state.demo.session if app.state.demo is not None else None
        return SystemManifest(
            app_version=APP_VERSION,
            environment=environment,
            ontology_version=memory.ontology.version,
            schema_hash=json_hash(app.openapi()),
            snapshot_root_hash=latest_snapshot_hash(PROJECT_ROOT),
            counts=ledger_counts(memory),
            metrics=ledger_metrics(memory),
            capabilities=CAPABILITIES,
            components=component_statuses(memory, app.state.live_memory is not None),
            live_available=app.state.live_memory is not None,
            live_writes_allowed=app.state.allow_live_writes,
            demo_session_id=session.session_id if session else None,
            verification=latest_verification(PROJECT_ROOT),
        )

    @app.get("/v1/system/topology")
    def system_topology(request: Request):
        memory = memory_for(request)
        components = component_statuses(memory, app.state.live_memory is not None)
        return {
            "components": components,
            "flows": [
                {"from": "object-store", "to": "ledger", "label": "manifest + evidence"},
                {"from": "ledger", "to": "neo4j", "label": "rebuildable projection"},
                {"from": "ledger", "to": "cognee", "label": "retrieval projection"},
                {"from": "ledger", "to": "feishu", "label": "working-view mirror"},
                {"from": "qiaomu", "to": "object-store", "label": "normalized source"},
            ],
            "authority_rule": "Only hashed source objects and the append-only ledger are authoritative.",
        }

    @app.get("/v1/system/events")
    async def system_events(request: Request, last_event_id: int | None = Header(default=None, alias="Last-Event-ID")):
        async def stream():
            cursor = last_event_id or 0
            while not await request.is_disconnected():
                events = broker.since(cursor)
                if events:
                    for event in events:
                        cursor = event.event_id
                        yield f"id: {event.event_id}\nevent: {event.event_type}\ndata: {event.model_dump_json()}\n\n"
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.75)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.get("/v1/ontology")
    def ontology(request: Request):
        return memory_for(request).ontology.definition

    @app.post("/v1/person-profile-import-runs", response_model=PersonProfileImportRun, status_code=201)
    def create_person_profile_import(request: Request, payload: PersonProfileImportRequest) -> PersonProfileImportRun:
        memory = memory_for(request, write=True)
        if payload.bootstrap_demo_fixture and not any(
            item.entity_type == "Person" for item in memory.ledger.list_entities()
        ):
            fixture = PROJECT_ROOT / "fixtures" / "icml_2026_people_excerpt.md"
            IcmlPeopleMarkdownImporter(memory).import_path(
                fixture,
                observed_at=utc_now(),
                created_by=payload.created_by,
            )
        result = PersonProfileService(memory).backfill(payload)
        publish(
            "person.profile.imported",
            "person_profile_import",
            result.import_run_id,
            {"people": result.counts.get("people", 0), "person_first": True},
        )
        return result

    @app.get("/v1/person-profile-import-runs", response_model=list[PersonProfileImportRun])
    def list_person_profile_import_runs(request: Request) -> list[PersonProfileImportRun]:
        return memory_for(request).ledger.list_person_profile_import_runs()

    @app.get("/v1/person-profiles")
    def list_person_profiles(
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        q: str | None = None,
    ):
        return collection_page(
            profiles_for(request).list_profiles(),
            offset=offset,
            limit=limit,
            query=q,
            search_value=lambda item: f"{item.canonical_name} {item.person_key} {' '.join(item.aliases)}",
        )

    @app.get("/v1/person-profiles/{person_key}", response_model=PersonProfileView)
    def get_person_profile(request: Request, person_key: str) -> PersonProfileView:
        try:
            return profiles_for(request).profile_view(person_key)
        except LedgerNotFound as exc:
            raise HTTPException(status_code=404, detail=f"person profile not found: {person_key}") from exc

    @app.get("/v1/person-profiles/{person_key}/revisions", response_model=list[PersonProfileRevision])
    def person_profile_revisions(request: Request, person_key: str) -> list[PersonProfileRevision]:
        return memory_for(request).ledger.list_person_profile_revisions(person_key)

    @app.get("/v1/person-profiles/{person_key}/sources")
    def person_profile_sources(request: Request, person_key: str):
        memory = memory_for(request)
        return [
            {
                "slice": item,
                "source": memory.ledger.get_source_version(item.source_version_id),
                "evidence": memory.ledger.get_evidence_span(item.evidence_span_id),
            }
            for item in memory.ledger.list_person_source_slices(person_key)
        ]

    @app.get("/v1/person-profiles/{person_key}/search-plan", response_model=PersonSearchPlanRevision)
    def get_person_search_plan(request: Request, person_key: str) -> PersonSearchPlanRevision:
        try:
            return profiles_for(request).latest_search_plan(person_key)
        except LedgerNotFound as exc:
            raise HTTPException(status_code=404, detail=f"search plan not found: {person_key}") from exc

    @app.get(
        "/v1/person-profiles/{person_key}/platform-accounts",
        response_model=list[PersonPlatformAccount],
    )
    def get_person_platform_accounts(request: Request, person_key: str) -> list[PersonPlatformAccount]:
        try:
            return profiles_for(request).latest_search_plan(person_key).platform_accounts
        except LedgerNotFound as exc:
            raise HTTPException(status_code=404, detail=f"platform accounts not found: {person_key}") from exc

    @app.post("/v1/person-profiles/{person_key}/search-plan", response_model=PersonSearchPlanRevision, status_code=201)
    def replace_person_search_plan(
        request: Request,
        person_key: str,
        payload: PersonSearchPlanRevision,
    ) -> PersonSearchPlanRevision:
        try:
            result = profiles_for(request, write=True).replace_search_plan(person_key, payload)
        except (LedgerNotFound, MemoryValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        publish("person.search_plan.revised", "person_profile", person_key, {"revision_id": result.search_plan_revision_id})
        return result

    @app.post("/v1/person-profiles/{person_key}/scan-runs", response_model=PersonProfileScanResponse, status_code=201)
    def run_person_profile_scan(
        request: Request,
        person_key: str,
        payload: PersonProfileScanRequest,
    ) -> PersonProfileScanResponse:
        try:
            result = profiles_for(request, write=True).scan(person_key, payload)
        except (LedgerNotFound, MemoryValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        publish(
            "person.profile.scan.completed",
            "person_profile_scan",
            result.scan_run.person_profile_scan_run_id,
            {"person_key": person_key, "status": result.scan_run.status, "bundle_id": result.scan_run.bundle_id},
        )
        return result

    @app.get("/v1/person-profile-scan-runs/{scan_run_id}", response_model=PersonProfileScanRun)
    def get_person_profile_scan(request: Request, scan_run_id: str) -> PersonProfileScanRun:
        result = next(
            (
                item for item in memory_for(request).ledger.list_person_profile_scan_runs()
                if item.person_profile_scan_run_id == scan_run_id
            ),
            None,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="person profile scan not found")
        return result

    @app.get("/v1/person-update-bundles/{bundle_id}", response_model=PersonUpdateBundle)
    def get_person_update_bundle(request: Request, bundle_id: str) -> PersonUpdateBundle:
        result = next(
            (item for item in memory_for(request).ledger.list_person_update_bundles() if item.bundle_id == bundle_id),
            None,
        )
        if result is None:
            raise HTTPException(status_code=404, detail="person update bundle not found")
        return result

    @app.get("/v1/person-update-bundles/{bundle_id}/export.md", response_class=PlainTextResponse)
    def export_person_update_bundle(request: Request, bundle_id: str) -> str:
        try:
            return profiles_for(request).bundle_markdown(bundle_id)
        except LedgerNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/person-digest-batches/latest", response_model=PersonDigestBatch)
    def latest_person_digest(request: Request) -> PersonDigestBatch:
        try:
            return profiles_for(request).latest_digest()
        except LedgerNotFound as exc:
            raise HTTPException(status_code=404, detail="no person digest has been generated") from exc

    @app.get("/v1/person-digest-batches/{digest_batch_id}/export.md", response_class=PlainTextResponse)
    def export_person_digest(request: Request, digest_batch_id: str) -> str:
        try:
            return profiles_for(request).digest_markdown(digest_batch_id)
        except LedgerNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/person-profile-review-batches/{review_batch_id}", response_model=ProfilePatchReviewBatchView)
    def get_person_profile_review(request: Request, review_batch_id: str) -> ProfilePatchReviewBatchView:
        try:
            return profiles_for(request).review_batch(review_batch_id)
        except LedgerNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/v1/person-profile-review-batches/{review_batch_id}/export.md", response_class=PlainTextResponse)
    def export_person_profile_review(request: Request, review_batch_id: str) -> str:
        try:
            return profiles_for(request).review_markdown(review_batch_id)
        except LedgerNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        "/v1/person-profile-review-batches/{review_batch_id}/decisions",
        response_model=ProfilePatchReviewBatchView,
    )
    def decide_person_profile_review(
        request: Request,
        review_batch_id: str,
        payload: ProfilePatchReviewDecisionRequest,
    ) -> ProfilePatchReviewBatchView:
        try:
            result = profiles_for(request, write=True).decide_review(review_batch_id, payload)
        except (LedgerNotFound, MemoryValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        publish("person.profile.reviewed", "person_profile_review", review_batch_id, {"action": payload.action})
        return result

    @app.get("/v1/person-profiles/{person_key}/graph-comparison", response_model=PersonGraphComparison)
    def compare_person_profile_graph(request: Request, person_key: str) -> PersonGraphComparison:
        try:
            return profiles_for(request).graph_comparison(person_key)
        except LedgerNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/person-profiles/{person_key}/cognee/project", response_model=PersonCogneeProjection)
    def project_person_profile_cognee(request: Request, person_key: str) -> PersonCogneeProjection:
        try:
            return profiles_for(request, write=True).project_cognee(person_key)
        except LedgerNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/v1/person-profiles/{person_key}/cognee/recall", response_model=PersonCogneeRecallResponse)
    def recall_person_profile_cognee(
        request: Request,
        person_key: str,
        payload: PersonCogneeRecallRequest,
    ) -> PersonCogneeRecallResponse:
        try:
            return profiles_for(request).recall_cognee(person_key, payload)
        except (LedgerNotFound, MemoryValidationError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/workflow/definition", response_model=WorkflowDefinition)
    def workflow_definition() -> WorkflowDefinition:
        """The single product-to-implementation contract rendered by both demo homes."""
        return WORKFLOW_DEFINITION

    @app.get("/v1/memory/architecture", response_model=MemoryArchitectureDefinition)
    def memory_architecture() -> MemoryArchitectureDefinition:
        """Canonical authority, Cognee and write-path contract rendered by the knowledge demo."""
        return MEMORY_ARCHITECTURE

    @app.get("/v1/system/connectors")
    def system_connectors():
        return connectors_for_runtime().capabilities()

    @app.get("/v1/system/connector-policy", response_model=list[ConnectorPolicyDefinition])
    def system_connector_policy() -> list[ConnectorPolicyDefinition]:
        return connector_policy_definitions()

    @app.get("/v1/system/connector-schedule", response_model=ConnectorScheduleConfig)
    def system_connector_schedule(request: Request) -> ConnectorScheduleConfig:
        return merge_person_profile_subscriptions(
            memory_for(request),
            load_connector_schedule(PROJECT_ROOT / "config" / "connector-schedule.json"),
        )

    @app.post("/v1/connectors/runs", response_model=ConnectorRunResponse, status_code=201)
    def run_connector(request: Request, payload: ConnectorRunRequest) -> ConnectorRunResponse:
        """Run one allowlisted source connector and append its auditable ledger records."""
        memory = memory_for(request, write=True)
        existing = memory.ledger.get_command_receipt(payload.command_id)
        if existing is None:
            publish(
                "connector.started",
                "connector_run",
                payload.command_id,
                {"channel": payload.channel, "trigger": payload.trigger},
            )
        result = execute_connector_run(memory, connectors_for_runtime(), payload)
        if existing is None:
            publish(
                "connector.completed",
                "connector_attempt",
                result.attempt.connector_attempt_id,
                {
                    "channel": payload.channel,
                    "status": result.attempt.status,
                    "scan_run_id": result.scan_run.scan_run_id,
                    "result_count": len(result.candidates),
                },
            )
            for source_version_id in result.source_version_ids:
                publish(
                    "source.versioned",
                    "source",
                    source_version_id,
                    {"scan_run_id": result.scan_run.scan_run_id, "channel": payload.channel},
                )
        return result

    @app.post("/v1/connector-jobs", response_model=ConnectorJobView, status_code=202)
    def enqueue_connector_job(request: Request, payload: ConnectorJobEnqueueRequest) -> ConnectorJobView:
        """Persist connector intent for an external worker; idempotent by command_id."""
        memory = memory_for(request, write=True)
        queue = ConnectorJobQueue(memory)
        existed = memory.ledger.find_connector_job_by_command_id(payload.request.command_id) is not None
        view = queue.enqueue(payload)
        if not existed:
            publish(
                "connector.job.queued",
                "connector_job",
                view.job.connector_job_id,
                {
                    "command_id": view.job.command_id,
                    "channel": view.job.request.channel,
                    "trigger": view.job.request.trigger,
                    "not_before": view.job.not_before.isoformat(),
                },
            )
        return view

    @app.get("/v1/connector-jobs")
    def list_connector_jobs(
        request: Request,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
        status: str | None = None,
    ):
        views = ConnectorJobQueue(memory_for(request)).list()
        if status:
            views = [item for item in views if item.status == status]
        return collection_page(
            views,
            offset=offset,
            limit=limit,
            sort_by="job.created_at",
            descending=True,
        )

    @app.get("/v1/connector-jobs/{connector_job_id}", response_model=ConnectorJobView)
    def get_connector_job(request: Request, connector_job_id: str) -> ConnectorJobView:
        return ConnectorJobQueue(memory_for(request)).get(connector_job_id)

    @app.get("/v1/connector-jobs/{connector_job_id}/events", response_model=list[ConnectorJobEvent])
    def get_connector_job_events(request: Request, connector_job_id: str) -> list[ConnectorJobEvent]:
        memory = memory_for(request)
        memory.ledger.get_connector_job(connector_job_id)
        return memory.ledger.list_connector_job_events(connector_job_id)

    @app.post("/v1/source-documents", response_model=SourceIngestionResponse, status_code=201)
    def ingest_source(request: Request, payload: SourceIngestionRequest) -> SourceIngestionResponse:
        response = memory_for(request, write=True).ingest_source(payload)
        publish("source.ingested", "source", response.source_version_id, {"version_status": response.version_status})
        return response

    @app.get("/v1/source-documents/{source_version_id}", response_model=SourceDocumentVersion)
    def get_source(request: Request, source_version_id: str) -> SourceDocumentVersion:
        return memory_for(request).get_source_version(source_version_id)

    @app.get("/v1/source-documents/{source_version_id}/content", response_class=PlainTextResponse)
    def get_source_content(request: Request, source_version_id: str, normalized: bool = True) -> str:
        return memory_for(request).get_source_text(source_version_id, normalized=normalized)

    @app.get("/v1/source-documents/{source_version_id}/spans")
    def get_spans(request: Request, source_version_id: str):
        memory = memory_for(request)
        memory.get_source_version(source_version_id)
        return memory.ledger.list_evidence_spans(source_version_id)

    @app.post("/v1/source-documents/{source_version_id}/cognify", response_model=CognifyResponse, status_code=202)
    def cognify(request: Request, source_version_id: str, payload: CognifyRequest) -> CognifyResponse:
        result = memory_for(request, write=True).cognify(source_version_id, payload)
        publish("extraction.requested", "extraction_run", result.extraction_run_id)
        publish(
            f"cognee.projection.{result.status}",
            "extraction_run",
            result.extraction_run_id,
            {"source_version_id": source_version_id, "authority": "projection_only"},
        )
        return result

    @app.get("/v1/cognee/status", response_model=CogneeRuntimeStatus)
    def cognee_status(request: Request) -> CogneeRuntimeStatus:
        return memory_for(request).cognee_status()

    @app.get("/v1/cognee/runs", response_model=list[ExtractionRun])
    def cognee_runs(request: Request) -> list[ExtractionRun]:
        return [item for item in memory_for(request).ledger.list_extraction_runs() if item.extractor == "cognee"]

    @app.post("/v1/cognee/project-all", response_model=CogneeBatchProjectionResponse)
    def cognee_project_all(request: Request, payload: CogneeBatchProjectionRequest) -> CogneeBatchProjectionResponse:
        result = memory_for(request, write=True).project_all_sources(payload)
        publish(
            "cognee.batch.completed",
            "cognee_dataset",
            result.dataset_name,
            {
                "total_sources": result.total_sources,
                "completed": result.completed,
                "failed": result.failed,
                "coverage_percent": result.coverage_percent,
                "authority": "projection_only",
            },
        )
        return result

    @app.post("/v1/cognee/recall", response_model=CogneeRecallResponse)
    def cognee_recall(request: Request, payload: CogneeRecallRequest) -> CogneeRecallResponse:
        result = memory_for(request).recall_cognee(payload)
        publish(
            "cognee.recall.completed",
            "cognee_dataset",
            result.dataset_name,
            {
                "query_type": result.query_type,
                "result_count": result.result_count,
                "evidence_linked_result_count": result.evidence_linked_result_count,
                "evidence_trace_rate_percent": result.evidence_trace_rate_percent,
                "duration_ms": result.duration_ms,
                "authority": result.authority,
            },
        )
        return result

    @app.post("/v1/initial-reviews/from-story/{session_id}", response_model=InitialReviewBatchView, status_code=201)
    def create_initial_review_from_story(session_id: str) -> InitialReviewBatchView:
        stories = stories_for_demo()
        try:
            session = stories.get_session(session_id)
            state = stories.state[session_id]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown story session") from exc
        existing_ids = {item.review_batch_id for item in app.state.memory.ledger.list_initial_review_batches()}
        result = app.state.memory.create_initial_review_batch(
            source_kind="story_session",
            source_ref=session_id,
            title_zh=f"{state['fixture']['subject_name']} 首次建档事实确认",
            assertion_ids=state.get("assertion_ids", []),
            source_version_ids=state.get("source_ids", []),
            created_by="demo-user",
        )
        if result.batch.review_batch_id not in existing_ids:
            publish("review.batch.created", "initial_review_batch", result.batch.review_batch_id, {"story_id": session.story_id, "item_count": len(result.items)})
        return result

    @app.post(
        "/v1/initial-knowledge/imports/icml-markdown",
        response_model=InitialKnowledgeImportResponse,
        status_code=201,
    )
    def import_initial_icml_knowledge(
        request: Request, payload: InitialKnowledgeImportRequest
    ) -> InitialKnowledgeImportResponse:
        memory = memory_for(request, write=True)
        existing_batch_ids = {item.review_batch_id for item in memory.ledger.list_initial_review_batches()}
        report = IcmlPeopleMarkdownImporter(memory).import_text(
            payload.content,
            source_uri=payload.source_uri,
            observed_at=payload.observed_at,
            created_by=payload.created_by,
            cohort_id=payload.cohort_id,
            media_type=payload.media_type,
            normalized_markdown=payload.normalized_markdown,
        )
        publish(
            "knowledge.initial_import.completed",
            "source",
            report.source_version_id,
            {
                "review_batch_id": report.review_batch_id,
                "version_status": report.version_status,
                "counts": report.counts,
            },
        )
        if report.review_batch_id not in existing_batch_ids:
            publish(
                "review.batch.created",
                "initial_review_batch",
                report.review_batch_id,
                {"source_kind": "file_import", "item_count": report.counts["review_items"]},
            )
        return InitialKnowledgeImportResponse(
            source_version_id=report.source_version_id,
            review_batch_id=report.review_batch_id,
            version_status=report.version_status,
            counts=report.counts,
            person_entity_ids=[item.entity_id for item in report.people],
            paper_entity_ids=report.paper_entity_ids,
            assertion_ids=report.assertion_ids,
            derived_assertion_ids=report.derived_assertion_ids,
            evidence_span_ids=report.evidence_span_ids,
        )

    @app.get("/v1/initial-reviews", response_model=list[InitialReviewBatchView])
    def initial_review_batches(request: Request) -> list[InitialReviewBatchView]:
        memory = memory_for(request)
        return [memory.initial_review_batch_view(item.review_batch_id) for item in memory.ledger.list_initial_review_batches()]

    @app.get("/v1/initial-reviews/{review_batch_id}", response_model=InitialReviewBatchView)
    def initial_review_batch(request: Request, review_batch_id: str) -> InitialReviewBatchView:
        return memory_for(request).initial_review_batch_view(review_batch_id)

    @app.get(
        "/v1/initial-reviews/{review_batch_id}/document",
        response_model=ConsolidatedReviewDocument,
    )
    def initial_review_document(request: Request, review_batch_id: str) -> ConsolidatedReviewDocument:
        return memory_for(request).consolidated_review_document(review_batch_id)

    @app.post(
        "/v1/initial-reviews/{review_batch_id}/bulk-decisions",
        response_model=ReviewBulkDecisionResponse,
    )
    def bulk_decide_initial_review(
        request: Request,
        review_batch_id: str,
        payload: ReviewBulkDecisionRequest,
    ) -> ReviewBulkDecisionResponse:
        result = memory_for(request, write=True).bulk_decide_initial_review(review_batch_id, payload)
        publish(
            "review.bulk_resolved",
            "initial_review_batch",
            review_batch_id,
            {"action": payload.action, "scope": payload.scope, "affected_count": len(result.affected_review_item_ids)},
        )
        return result

    @app.get("/v1/initial-reviews/{review_batch_id}/delivery", response_model=ReviewDeliveryPage)
    def initial_review_delivery(
        request: Request,
        review_batch_id: str,
        channel: Literal["web", "feishu", "markdown", "api"] = "feishu",
        cursor: int = Query(default=0, ge=0),
        page_size: int = Query(default=5, ge=1, le=20),
        pending_only: bool = False,
    ) -> ReviewDeliveryPage:
        return memory_for(request).initial_review_delivery(
            review_batch_id,
            channel=channel,
            cursor=cursor,
            page_size=page_size,
            pending_only=pending_only,
        )

    @app.post(
        "/v1/initial-reviews/{review_batch_id}/messages",
        response_model=ReviewMessageCommandResult,
    )
    def apply_initial_review_message(
        request: Request,
        review_batch_id: str,
        payload: ReviewMessageCommandRequest,
    ) -> ReviewMessageCommandResult:
        result = memory_for(request, write=True).apply_initial_review_message(review_batch_id, payload)
        publish(
            "review.message.processed",
            "initial_review_batch",
            review_batch_id,
            {
                "command_id": payload.command_id,
                "matched": result.matched,
                "action": result.action,
                "affected_count": len(result.affected_review_item_ids),
                "channel": payload.channel,
            },
        )
        return result

    @app.get("/v1/initial-reviews/{review_batch_id}/export.md", response_class=PlainTextResponse)
    def initial_review_export(request: Request, review_batch_id: str) -> str:
        return memory_for(request).initial_review_markdown(review_batch_id)

    @app.post("/v1/initial-review-items/{review_item_id}/decisions", response_model=InitialReviewDecision, status_code=201)
    def decide_initial_review(request: Request, review_item_id: str, payload: InitialReviewDecisionRequest) -> InitialReviewDecision:
        memory = memory_for(request, write=True)
        existing_ids = {item.review_decision_id for item in memory.ledger.list_initial_review_decisions(review_item_id)}
        result = memory.decide_initial_review_item(review_item_id, payload)
        if result.review_decision_id not in existing_ids:
            publish("review.item.resolved", "initial_review_item", review_item_id, {"action": result.action, "decision_id": result.review_decision_id})
        return result

    @app.post(
        "/v1/source-watches/directory/scan",
        response_model=DirectoryWatchScanResponse,
    )
    def scan_directory_source(request: Request, payload: DirectoryWatchScanRequest) -> DirectoryWatchScanResponse:
        result = DirectorySourceTracker(memory_for(request, write=True)).scan(payload)
        publish(
            "source.watch.scanned",
            "source",
            result.manifest_source_version_id,
            {
                "source_kind": "directory",
                "version_status": result.version_status,
                "changed_files": sum(item.status == "changed" for item in result.files),
                "new_files": sum(item.status == "new" for item in result.files),
                "removed_files": len(result.removed_paths),
            },
        )
        return result

    @app.post(
        "/v1/source-watches/feishu/scan",
        response_model=FeishuDocumentScanResponse,
    )
    def scan_feishu_document(request: Request, payload: FeishuDocumentScanRequest) -> FeishuDocumentScanResponse:
        memory = memory_for(request, write=True)
        if payload.importer == "apple_scholars":
            report = AppleScholarsMarkdownImporter(memory).import_text(
                payload.content,
                source_uri=payload.source_uri,
                observed_at=payload.observed_at,
                created_by=payload.created_by,
                normalized_markdown=payload.normalized_markdown,
            )
            result = FeishuDocumentScanResponse(
                source_version_id=report.source_version_id,
                version_status=report.version_status,
                review_batch_id=report.review_batch_id,
                imported_people=report.counts["people"],
            )
        else:
            ingested = memory.ingest_source(SourceIngestionRequest(
                source_uri=payload.source_uri,
                source_type=SourceType.FEISHU,
                media_type="text/markdown",
                content=payload.content,
                normalized_markdown=payload.normalized_markdown,
                retrieved_at=payload.observed_at,
                fetch_method=FetchMethod.FEISHU,
                metadata={"watch_kind": "feishu_document"},
            ))
            result = FeishuDocumentScanResponse(
                source_version_id=ingested.source_version_id,
                version_status=ingested.version_status,
            )
        publish(
            "feishu.document.versioned",
            "source",
            result.source_version_id,
            {"version_status": result.version_status, "review_batch_id": result.review_batch_id},
        )
        return result

    @app.get("/v1/assertions/{assertion_id}")
    def get_assertion(request: Request, assertion_id: str):
        memory = memory_for(request)
        assertion = memory.ledger.get_assertion(assertion_id)
        return {"assertion": assertion, "annotations": memory.ledger.list_annotations(assertion_id), "effective_status": memory.effective_status(assertion, utc_now())}

    @app.post("/v1/annotations", response_model=Annotation, status_code=201)
    def annotate(request: Request, payload: AnnotationRequest) -> Annotation:
        result = memory_for(request, write=True).annotate(payload)
        publish("annotation.appended", "annotation", result.annotation_id, {"action": result.action})
        return result

    @app.post("/v1/assertions/{assertion_id}/derive", status_code=201)
    def derive(request: Request, assertion_id: str, payload: DeriveRequest):
        if assertion_id not in payload.input_assertion_ids:
            raise HTTPException(status_code=422, detail="path assertion_id must be one of input_assertion_ids")
        result = memory_for(request, write=True).derive(payload)
        publish("assertion.derived", "assertion", result.assertion_id)
        return result

    @app.get("/v1/entities/{entity_id}/timeline")
    def timeline(request: Request, entity_id: str, known_at: datetime | None = None, include_derived: bool = True):
        return memory_for(request).timeline(entity_id, known_at=known_at, include_derived=include_derived)

    @app.get("/v1/entities/{entity_id}/working-view", response_model=WorkingView)
    def working_view(
        request: Request,
        entity_id: str,
        valid_at: datetime = Query(default_factory=utc_now),
        known_at: datetime = Query(default_factory=utc_now),
        predicates: list[str] = Query(default=[]),
        include_derived: bool = True,
    ) -> WorkingView:
        return memory_for(request).working_view(GraphQuery(subject_entity_id=entity_id, predicates=predicates, valid_at=valid_at, known_at=known_at, view=GraphView.WORKING, include_derived=include_derived))

    @app.post("/v1/graph/query")
    def graph_query(request: Request, query: GraphQuery):
        return memory_for(request).graph_query(query)

    @app.post("/v1/commands", response_model=CommandReceipt)
    def execute_command(request: Request, command: CommandEnvelope) -> CommandReceipt:
        memory = memory_for(request, write=True)
        processor = (
            app.state.commands
            if memory is app.state.memory
            else CommandProcessor(memory, scan_request_handler=app.state.make_scan_handler(memory))
        )
        result = processor.execute(command)
        publish("command.processed", "command", result.command_id, {"status": result.status})
        return result

    def page(request: Request, getter: Callable[[TemporalMemoryService], list[Any]], offset: int, limit: int, q: str | None, sort_by: str | None, sort_order: str):
        return collection_page(
            getter(memory_for(request)),
            offset=offset,
            limit=limit,
            query=q,
            search_value=lambda item: json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
            sort_by=sort_by,
            descending=sort_order == "desc",
        )

    @app.get("/v1/entities")
    def list_entities(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), q: str | None = None, sort_by: str | None = None, sort_order: Literal["asc", "desc"] = "asc"):
        return page(request, lambda m: m.ledger.list_entities(), offset, limit, q, sort_by, sort_order)

    @app.get("/v1/assertions")
    def list_assertions(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), q: str | None = None, sort_by: str | None = None, sort_order: Literal["asc", "desc"] = "asc"):
        return page(request, lambda m: m.ledger.list_assertions(), offset, limit, q, sort_by, sort_order)

    @app.get("/v1/sources")
    def list_sources(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), q: str | None = None, sort_by: str | None = None, sort_order: Literal["asc", "desc"] = "asc"):
        return page(request, lambda m: m.ledger.list_source_versions(), offset, limit, q, sort_by, sort_order)

    @app.get("/v1/episodes")
    def list_episodes(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), q: str | None = None, sort_by: str | None = None, sort_order: Literal["asc", "desc"] = "asc"):
        return page(request, lambda m: m.ledger.list_episodes(), offset, limit, q, sort_by, sort_order)

    @app.get("/v1/spans")
    def list_spans(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), q: str | None = None, sort_by: str | None = None, sort_order: Literal["asc", "desc"] = "asc"):
        return page(request, lambda m: m.ledger.list_evidence_spans(), offset, limit, q, sort_by, sort_order)

    @app.get("/v1/annotations")
    def list_annotations(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), q: str | None = None, sort_by: str | None = None, sort_order: Literal["asc", "desc"] = "asc"):
        return page(request, lambda m: m.ledger.list_annotations(), offset, limit, q, sort_by, sort_order)

    @app.get("/v1/assertion-relations")
    def list_relations(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), q: str | None = None, sort_by: str | None = None, sort_order: Literal["asc", "desc"] = "asc"):
        return page(request, lambda m: m.ledger.list_assertion_relations(), offset, limit, q, sort_by, sort_order)

    @app.get("/v1/extraction-runs")
    def list_runs(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), q: str | None = None, sort_by: str | None = None, sort_order: Literal["asc", "desc"] = "asc"):
        return page(request, lambda m: m.ledger.list_extraction_runs(), offset, limit, q, sort_by, sort_order)

    @app.get("/v1/signals")
    def signals(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), q: str | None = None, sort_by: str | None = None, sort_order: Literal["asc", "desc"] = "asc"):
        return page(request, lambda m: m.ledger.list_signals(), offset, limit, q, sort_by, sort_order)

    @app.get("/v1/candidates")
    def candidates(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), q: str | None = None, sort_by: str | None = None, sort_order: Literal["asc", "desc"] = "asc"):
        return page(request, lambda m: m.ledger.list_candidates(), offset, limit, q, sort_by, sort_order)

    @app.get("/v1/scan-runs")
    def scan_runs(request: Request, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500), q: str | None = None, sort_by: str | None = None, sort_order: Literal["asc", "desc"] = "desc"):
        return page(request, lambda m: m.ledger.list_scan_runs(), offset, limit, q, sort_by, sort_order)

    @app.get("/v1/scan-runs/{scan_run_id}", response_model=ScanRun)
    def scan_run(request: Request, scan_run_id: str) -> ScanRun:
        values = [item for item in memory_for(request).ledger.list_scan_runs() if item.scan_run_id == scan_run_id]
        if not values:
            raise HTTPException(status_code=404, detail="unknown scan run")
        return values[-1]

    @app.get("/v1/scan-runs/{scan_run_id}/attempts")
    def scan_run_attempts(request: Request, scan_run_id: str):
        scan_run(request, scan_run_id)
        return memory_for(request).ledger.list_connector_attempts(scan_run_id)

    @app.get("/v1/demo/stories")
    def demo_stories():
        return stories_for_demo().fixtures.stories()

    @app.post("/v1/demo/story-sessions", response_model=StorySession, status_code=201)
    def create_story_session(payload: StorySessionCreate) -> StorySession:
        try:
            session = stories_for_demo().create_session(payload)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown demo story") from exc
        publish("story.session.created", "story_session", session.session_id, {"story_id": session.story_id, "mode": session.mode})
        return session

    @app.get("/v1/demo/story-sessions/{session_id}", response_model=StorySession)
    def story_session(session_id: str) -> StorySession:
        try:
            return stories_for_demo().get_session(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown story session") from exc

    @app.post("/v1/demo/story-sessions/{session_id}/steps/{stage_id}/run", response_model=StoryStepResult)
    def run_story_stage(session_id: str, stage_id: str) -> StoryStepResult:
        try:
            return stories_for_demo().run_stage(session_id, stage_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown story session or stage") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/demo/story-sessions/{session_id}/reset", response_model=StorySession, status_code=201)
    def reset_story_session(session_id: str) -> StorySession:
        try:
            session = stories_for_demo().reset(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown story session") from exc
        publish("story.session.reset", "story_session", session.session_id, {"previous_session_id": session_id})
        return session

    @app.get("/v1/demo/story-sessions/{session_id}/artifacts", response_model=list[WorkflowArtifact])
    def story_artifacts(session_id: str, stage_id: str | None = None):
        try:
            return stories_for_demo().artifacts(session_id, stage_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown story session") from exc

    @app.get("/v1/demo/story-sessions/{session_id}/graph-delta")
    def story_graph_delta(session_id: str):
        try:
            return stories_for_demo().graph_delta(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown story session") from exc

    @app.get("/v1/demo/story-sessions/{session_id}/knowledge-journey", response_model=KnowledgeJourney)
    def story_knowledge_journey(session_id: str) -> KnowledgeJourney:
        try:
            return stories_for_demo().knowledge_journey(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown story session") from exc

    @app.get("/v1/demo/recordings")
    def demo_recordings():
        manifests = []
        for path in sorted((PROJECT_ROOT / ".people_intel" / "recordings").glob("*/manifest.json")):
            try:
                manifests.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return manifests

    @app.post("/v1/demo/sessions", response_model=DemoSession)
    def create_demo_session() -> DemoSession:
        return controller_for_demo().create_session()

    @app.post("/v1/demo/reset", response_model=DemoSession)
    def reset_demo() -> DemoSession:
        controller_for_demo()
        old_root = app.state.memory.object_store.root
        new_memory = build_sandbox_service(old_root)
        app.state.memory = new_memory
        app.state.commands = CommandProcessor(new_memory)
        app.state.demo = DemoController(new_memory, broker, PROJECT_ROOT)
        app.state.stories = StoryController(new_memory, broker, PROJECT_ROOT)
        session = app.state.demo.create_session()
        publish("demo.session.reset", "demo_session", session.session_id)
        return session

    @app.get("/v1/demo/scenarios")
    def demo_scenarios():
        return [SCENARIO]

    @app.post("/v1/demo/scenarios/{scenario_id}/run", response_model=ScenarioStepResult)
    def run_demo_step(scenario_id: str, payload: DemoRunRequest):
        if scenario_id != SCENARIO.scenario_id:
            raise HTTPException(status_code=404, detail="unknown demo scenario")
        try:
            return controller_for_demo().run(payload.step)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/v1/demo/session", response_model=DemoSession | None)
    def demo_session():
        return controller_for_demo().session

    @app.get("/v1/demo/results")
    def demo_results():
        demo = controller_for_demo()
        return [demo.results[key] for key in sorted(demo.results)]

    @app.get("/v1/demo/graph", response_model=MemoryGraph)
    def demo_graph(request: Request):
        return build_memory_graph(memory_for(request))

    if serve_frontend:
        dist = PROJECT_ROOT / "frontend" / "dist"
        assets = dist / "assets"
        reports = PROJECT_ROOT / "reports"
        if assets.exists():
            app.mount("/demo/assets", StaticFiles(directory=assets), name="demo-assets")
        if reports.exists():
            app.mount("/demo/reports", StaticFiles(directory=reports, html=True), name="demo-reports")

        @app.get("/demo", include_in_schema=False)
        @app.get("/demo/{frontend_path:path}", include_in_schema=False)
        def demo_frontend(frontend_path: str = ""):
            index = dist / "index.html"
            if not index.exists():
                raise HTTPException(status_code=503, detail="frontend is not built; run npm run build in frontend/")
            return FileResponse(index)

    return app


app = create_app(
    live_service=build_live_service(),
    allow_live_writes=os.environ.get("PEOPLE_INTEL_ALLOW_LIVE_WRITES", "").strip().lower()
    in {"1", "true", "yes"},
    enable_demo=os.environ.get("PEOPLE_INTEL_ENABLE_DEMO", "true").strip().lower()
    in {"1", "true", "yes"},
)
