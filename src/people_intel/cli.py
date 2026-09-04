from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from people_intel.api import build_default_service, build_live_service, build_sandbox_service, create_app
from people_intel.connector_runtime import execute_connector_run
from people_intel.connector_jobs import ConnectorWorker, ConnectorWorkerConfig
from people_intel.connector_scheduler import (
    ConnectorScheduleProducer,
    load_connector_schedule,
    merge_person_profile_subscriptions,
)
from people_intel.connectors import ConnectorExecutor
from people_intel.content_store import ContentAddressedStore
from people_intel.feishu_runtime import FeishuLongConnectionWorker
from people_intel.importers import IcmlPeopleMarkdownImporter
from people_intel.ontology import OntologyRegistry
from people_intel.platform_tracking import evaluate_adversarial_fixture
from people_intel.projections import ProjectionCoordinator
from people_intel.schemas import (
    AnnotationRequest,
    Assertion,
    CognifyRequest,
    CommandEnvelope,
    ConnectorRunRequest,
    ConnectorRunResponse,
    DemoSession,
    GraphQuery,
    MemoryGraph,
    ScenarioStepResult,
    ScanRun,
    Signal,
    SourceDocumentVersion,
    SourceIngestionRequest,
    StorySession,
    StoryStepResult,
    WorkflowArtifact,
    WorkflowDefinition,
    new_id,
)
from people_intel.workflow import StoryFixtureStore


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    parser = argparse.ArgumentParser(description="People intelligence temporal memory")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the FastAPI service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)

    demo = sub.add_parser("demo", help="Start the observable MVP cockpit")
    demo.add_argument("--host", default="127.0.0.1")
    demo.add_argument("--port", type=int, default=8787)
    demo.add_argument("--open", action=argparse.BooleanOptionalAction, default=True)
    demo.add_argument("--dev", action="store_true", help="Run Vite with hot reload")
    demo.add_argument("--allow-live-writes", action="store_true")

    init_db = sub.add_parser("init-db", help="Initialize PostgreSQL schema")
    init_db.add_argument("--database-url", default=os.environ.get("PEOPLE_INTEL_DATABASE_URL"))

    import_icml = sub.add_parser("import-icml", help="Import the supplied ICML Markdown dossier")
    import_icml.add_argument("path")
    import_icml.add_argument("--snapshot-dir", help="Export the resulting append-only ledger as JSONL")

    schemas = sub.add_parser("export-schemas", help="Export public JSON schemas")
    schemas.add_argument("--output-dir", default="docs/schemas")

    snapshot = sub.add_parser("snapshot", help="Export an audit snapshot from the configured ledger")
    snapshot.add_argument("--output-dir", required=True)

    rebuild = sub.add_parser("rebuild-neo4j", help="Rebuild the disposable Neo4j graph projection")
    rebuild.add_argument("--snapshot-dir", default=".people_intel/snapshots/latest")
    rebuild.add_argument("--uri", default=os.environ.get("PEOPLE_INTEL_NEO4J_URI"))
    rebuild.add_argument("--user", default=os.environ.get("PEOPLE_INTEL_NEO4J_USER", "neo4j"))
    rebuild.add_argument("--password", default=os.environ.get("PEOPLE_INTEL_NEO4J_PASSWORD"))

    export_openapi = sub.add_parser("export-openapi", help="Export the live OpenAPI contract")
    export_openapi.add_argument("--output", default="docs/openapi.json")

    capture_fixtures = sub.add_parser("capture-demo-fixtures", help="Validate and content-address a frozen public-evidence story fixture")
    capture_fixtures.add_argument("--story", required=True)

    record_demo = sub.add_parser("record-demo", help="Record a product story to WebM with a matching manifest")
    record_demo.add_argument("--story", default="homepage-change")
    record_demo.add_argument("--output", default=".people_intel/recordings")
    record_demo.add_argument("--base-url", default="http://127.0.0.1:8787")

    connector_run = sub.add_parser("connector-run", help="Run one auditable allowlisted source scan")
    connector_run.add_argument(
        "--channel",
        required=True,
        choices=("homepage", "github", "arxiv", "x", "wechat", "xhs", "news"),
    )
    connector_run.add_argument("--query", required=True)
    connector_run.add_argument("--source-uri")
    connector_run.add_argument("--subject-name", default="manual-source-scan")
    connector_run.add_argument("--trigger", choices=("manual", "cron", "feishu"), default="manual")
    connector_run.add_argument("--max-results", type=int, default=5)
    connector_run.add_argument("--command-id")

    xhs_api_bootstrap = sub.add_parser(
        "xhs-api-bootstrap",
        help="Copy existing Chrome XHS cookies into the isolated read-only API route",
    )
    xhs_api_bootstrap.add_argument("--cookie-source", choices=("chrome",), default="chrome")
    xhs_api_bootstrap.add_argument("--root", default=os.environ.get("PEOPLE_INTEL_XHS_API_ROOT"))
    xhs_api_bootstrap.add_argument("--output-home", default=os.environ.get("PEOPLE_INTEL_XHS_API_HOME"))

    connector_worker = sub.add_parser("connector-worker", help="Process durable connector jobs from PostgreSQL")
    connector_worker.add_argument("--once", action="store_true", help="Claim at most one ready job and exit")
    connector_worker.add_argument("--poll-seconds", type=float, default=5.0)
    connector_worker.add_argument("--worker-id")
    connector_worker.add_argument("--lease-seconds", type=int, default=180)
    connector_worker.add_argument("--base-backoff-seconds", type=int, default=60)
    connector_worker.add_argument("--max-backoff-seconds", type=int, default=3600)

    connector_scheduler = sub.add_parser("connector-scheduler", help="Enqueue due tracked-source subscriptions into PostgreSQL")
    connector_scheduler.add_argument("--config", default="config/connector-schedule.json")
    connector_scheduler.add_argument("--once", action="store_true", help="Evaluate subscriptions once and exit")
    connector_scheduler.add_argument("--dry-run", action="store_true")
    connector_scheduler.add_argument("--poll-seconds", type=float, default=60.0)

    tracked_web = sub.add_parser(
        "tracked-web-scan",
        help="Run one due-only deterministic webpage scan for host-agent review",
    )
    tracked_web.add_argument(
        "--state-db",
        default=os.environ.get(
            "PEOPLE_INTEL_TRACKER_DATABASE",
            ".people_intel/tracked-web.sqlite3",
        ),
    )
    tracked_web.add_argument("--cadence-days", type=int, default=7)
    tracked_web.add_argument("--workers", type=int, default=8)

    tracked_web_worker = sub.add_parser(
        "tracked-web-worker",
        help="Continuously synchronize person profiles and scan due public webpages",
    )
    tracked_web_worker.add_argument(
        "--state-db",
        default=os.environ.get(
            "PEOPLE_INTEL_TRACKER_DATABASE",
            ".people_intel/tracked-web.sqlite3",
        ),
    )
    tracked_web_worker.add_argument("--cadence-days", type=int, default=7)
    tracked_web_worker.add_argument("--workers", type=int, default=8)
    tracked_web_worker.add_argument("--poll-seconds", type=float, default=900.0)

    feishu_worker = sub.add_parser(
        "feishu-worker",
        help="Receive Feishu events through the official WebSocket client and append durable commands",
    )
    feishu_worker.add_argument(
        "--check",
        action="store_true",
        help="Validate credentials/allowlist without opening a connection",
    )

    sub.add_parser("verify-demo", help="Run backend, frontend, contract and production-build checks")
    platform_eval = sub.add_parser(
        "verify-platform-tracking",
        help="Replay the six X/XHS/GitHub adversarial cases through three algorithm rounds",
    )
    platform_eval.add_argument(
        "--fixture",
        default="fixtures/platform_adversarial_cases.json",
    )
    platform_eval.add_argument("--output")
    tracking_benchmark = sub.add_parser(
        "verify-tracking-benchmark",
        help="Evaluate 42 channel and 48 workflow cases over three algorithm rounds",
    )
    tracking_benchmark.add_argument("--output")

    args = parser.parse_args(argv)
    if args.command == "serve":
        import uvicorn

        uvicorn.run("people_intel.api:app", host=args.host, port=args.port, reload=False)
        return 0
    if args.command == "demo":
        import uvicorn

        project_root = Path(__file__).resolve().parents[2]
        frontend = project_root / "frontend"
        vite_process = None
        if args.dev:
            _ensure_frontend_dependencies(frontend)
            vite_process = subprocess.Popen(["npm", "run", "dev"], cwd=frontend)
            target_url = "http://127.0.0.1:5173/demo/"
            serve_frontend = False
        else:
            _build_frontend_if_needed(frontend)
            target_url = f"http://{args.host}:{args.port}/demo"
            serve_frontend = True
        cockpit = create_app(
            build_sandbox_service(project_root / ".people_intel" / "demo" / "objects"),
            live_service=build_live_service(),
            allow_live_writes=args.allow_live_writes,
            serve_frontend=serve_frontend,
        )
        if args.open:
            open_port = 5173 if args.dev else args.port
            threading.Thread(
                target=_open_when_ready,
                args=("127.0.0.1", open_port, target_url),
                daemon=True,
            ).start()
        try:
            uvicorn.run(cockpit, host=args.host, port=args.port)
        finally:
            if vite_process is not None:
                vite_process.terminate()
        return 0
    if args.command in {"tracked-web-scan", "tracked-web-worker"}:
        from people_intel.tracked_web_worker import run_forever, run_once

        service = build_live_service()
        if service is None:
            parser.error(
                f"{args.command} requires PEOPLE_INTEL_DATABASE_URL so imported "
                "person profiles and digests survive restarts"
            )
        if args.cadence_days < 1:
            parser.error("--cadence-days must be at least 1")
        if args.workers < 1 or args.workers > 32:
            parser.error("--workers must be between 1 and 32")
        if args.command == "tracked-web-scan":
            print(
                json.dumps(
                    run_once(
                        service,
                        state_database=args.state_db,
                        cadence_days=args.cadence_days,
                        workers=args.workers,
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        run_forever(
            service,
            state_database=args.state_db,
            cadence_days=args.cadence_days,
            workers=args.workers,
            poll_seconds=args.poll_seconds,
        )
        return 0
    if args.command == "init-db":
        if not args.database_url:
            parser.error("--database-url or PEOPLE_INTEL_DATABASE_URL is required")
        from people_intel.adapters.postgres import PostgresLedger

        ledger = PostgresLedger(args.database_url)
        ledger.initialize_schema()
        ledger.register_ontology(OntologyRegistry.default().definition)
        print(json.dumps({"status": "initialized", "database_url": _redact_url(args.database_url)}))
        return 0
    if args.command == "import-icml":
        service = build_default_service()
        report = IcmlPeopleMarkdownImporter(service).import_path(
            args.path,
            observed_at=datetime.now(timezone.utc),
        )
        result = _report_dict(report)
        if args.snapshot_dir:
            manifest = ProjectionCoordinator(service.ledger).export_snapshot(args.snapshot_dir)
            result["snapshot"] = {
                "output_dir": args.snapshot_dir,
                "root_hash": manifest.root_hash,
                "counts": manifest.counts,
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "export-schemas":
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        for model in (
            SourceDocumentVersion,
            SourceIngestionRequest,
            Assertion,
            AnnotationRequest,
            CognifyRequest,
            GraphQuery,
            Signal,
            CommandEnvelope,
            ConnectorRunRequest,
            ConnectorRunResponse,
            DemoSession,
            ScenarioStepResult,
            MemoryGraph,
            ScanRun,
            StorySession,
            StoryStepResult,
            WorkflowArtifact,
            WorkflowDefinition,
        ):
            path = output / f"{model.__name__}.schema.json"
            path.write_text(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": "exported", "output_dir": str(output)}))
        return 0
    if args.command == "snapshot":
        service = build_default_service()
        manifest = ProjectionCoordinator(service.ledger).export_snapshot(args.output_dir)
        print(json.dumps({"status": "exported", **manifest.__dict__}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "rebuild-neo4j":
        if not args.uri or not args.password:
            parser.error("Neo4j --uri/--password or PEOPLE_INTEL_NEO4J_* variables are required")
        from people_intel.adapters.neo4j import Neo4jProjector

        service = build_default_service()
        coordinator = ProjectionCoordinator(
            service.ledger,
            Neo4jProjector(args.uri, args.user, args.password),
        )
        report = coordinator.rebuild(args.snapshot_dir)
        print(
            json.dumps(
                {
                    "status": "rebuilt",
                    "snapshot_root_hash": report.snapshot.root_hash,
                    "graph_stats": report.graph_stats,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "export-openapi":
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        document = create_app(build_sandbox_service(), serve_frontend=False).openapi()
        output.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"status": "exported", "output": str(output), "paths": len(document["paths"])}))
        return 0
    if args.command == "capture-demo-fixtures":
        project_root = Path(__file__).resolve().parents[2]
        fixtures = StoryFixtureStore(project_root / "fixtures" / "demo_stories.json")
        if args.story not in fixtures.items:
            parser.error(f"unknown story: {args.story}")
        item = fixtures.items[args.story]
        payload = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        stored = ContentAddressedStore(project_root / ".people_intel" / "demo-fixtures").put_text(payload)
        print(json.dumps({
            "status": "captured",
            "story_id": args.story,
            "fixture_version": fixtures.fixture_version,
            "fixture_hash": fixtures.fixture_hash(args.story),
            "object_ref": stored.object_ref,
            "source_uri": item["source_uri"],
            "rights_scope": "public",
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "record-demo":
        project_root = Path(__file__).resolve().parents[2]
        frontend = project_root / "frontend"
        _ensure_frontend_dependencies(frontend)
        command = [
            "node", str(frontend / "scripts" / "record-demo.mjs"),
            "--story", args.story,
            "--output", str(Path(args.output).resolve()),
            "--base-url", args.base_url,
        ]
        return subprocess.run(command, cwd=frontend, check=False).returncode
    if args.command == "connector-run":
        service = build_default_service()
        request = ConnectorRunRequest(
            command_id=args.command_id or new_id("cmd_connector"),
            channel=args.channel,
            query=args.query,
            source_uri=args.source_uri,
            subject_name=args.subject_name,
            trigger=args.trigger,
            max_results=args.max_results,
        )
        result = execute_connector_run(service, ConnectorExecutor(service.object_store), request)
        print(result.model_dump_json(indent=2))
        return 0 if result.attempt.status == "completed" else 2
    if args.command == "xhs-api-bootstrap":
        project_root = Path(__file__).resolve().parents[2]
        root = Path(args.root or project_root / ".people_intel" / "evals" / "jackwener-xiaohongshu-cli").expanduser().resolve()
        output_home = Path(args.output_home or project_root / ".people_intel" / "xhs-api-home").expanduser().resolve()
        python = root / ".venv" / "bin" / "python"
        script = project_root / "scripts" / "bootstrap_xhs_api.py"
        if not python.exists():
            parser.error(f"xiaohongshu-cli environment is unavailable: {python}")
        return subprocess.run(
            [str(python), str(script), "--output-home", str(output_home)],
            cwd=project_root,
            check=False,
            env={**os.environ, "HOME": str(Path.home())},
        ).returncode
    if args.command == "connector-worker":
        service = build_live_service()
        if service is None:
            parser.error("connector-worker requires PEOPLE_INTEL_DATABASE_URL so jobs survive process restarts")
        worker = ConnectorWorker(
            service,
            ConnectorExecutor(service.object_store),
            ConnectorWorkerConfig(
                worker_id=args.worker_id or f"{socket.gethostname()}:{os.getpid()}",
                lease_seconds=max(1, args.lease_seconds),
                base_backoff_seconds=max(1, args.base_backoff_seconds),
                max_backoff_seconds=max(1, args.max_backoff_seconds),
            ),
        )
        while True:
            view = worker.run_once()
            if view is not None:
                print(view.model_dump_json(indent=2), flush=True)
            if args.once:
                return 0
            time.sleep(max(0.2, min(args.poll_seconds, 60.0)))
    if args.command == "connector-scheduler":
        service = build_live_service()
        if service is None:
            parser.error("connector-scheduler requires PEOPLE_INTEL_DATABASE_URL")
        config_path = Path(args.config)
        if not config_path.is_absolute():
            project_root = Path(
                os.environ.get("PEOPLE_INTEL_PROJECT_ROOT", Path(__file__).resolve().parents[2])
            )
            config_path = project_root / config_path
        producer = ConnectorScheduleProducer(
            service,
            merge_person_profile_subscriptions(service, load_connector_schedule(config_path)),
        )
        while True:
            result = producer.run_once(dry_run=args.dry_run)
            print(result.model_dump_json(indent=2), flush=True)
            if args.once or args.dry_run:
                return 0
            time.sleep(max(5.0, min(args.poll_seconds, 3600.0)))
    if args.command == "feishu-worker":
        from people_intel.feishu_runtime import FeishuRuntimeConfig

        config = FeishuRuntimeConfig.from_environment()
        if args.check:
            print(json.dumps({"status": "ready" if config.ready else "unconfigured", "missing": config.missing()}))
            return 0 if config.ready else 2
        service = build_live_service()
        if service is None:
            parser.error("feishu-worker requires PEOPLE_INTEL_DATABASE_URL")
        FeishuLongConnectionWorker(service, config).start()
        return 0
    if args.command == "verify-demo":
        return _verify_demo(Path(__file__).resolve().parents[2])
    if args.command == "verify-platform-tracking":
        project_root = Path(__file__).resolve().parents[2]
        fixture = Path(args.fixture)
        if not fixture.is_absolute():
            fixture = project_root / fixture
        report = evaluate_adversarial_fixture(fixture)
        payload = report.model_dump_json(indent=2)
        if args.output:
            output = Path(args.output)
            if not output.is_absolute():
                output = project_root / output
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        return 0 if report.final_release_passed else 2
    if args.command == "verify-tracking-benchmark":
        from people_intel.tracking_benchmark import evaluate_tracking_benchmark

        project_root = Path(__file__).resolve().parents[2]
        report = evaluate_tracking_benchmark()
        payload = report.model_dump_json(indent=2)
        if args.output:
            output = Path(args.output)
            if not output.is_absolute():
                output = project_root / output
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload + "\n", encoding="utf-8")
        print(payload)
        return 0 if report.final_release_passed else 2
    return 1


def _redact_url(url: str) -> str:
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


def _report_dict(report) -> dict:
    return {
        "source_version_id": report.source_version_id,
        "people_count": len(report.people),
        "paper_count": len(report.paper_entity_ids),
        "identity_count": len(report.identity_entity_ids),
        "people": [item.canonical_name for item in report.people],
    }


def _ensure_frontend_dependencies(frontend: Path) -> None:
    if not (frontend / "node_modules" / ".bin" / "vite").exists():
        subprocess.run(["npm", "install", "--include=dev"], cwd=frontend, check=True)


def _build_frontend_if_needed(frontend: Path) -> None:
    _ensure_frontend_dependencies(frontend)
    index = frontend / "dist" / "index.html"
    source_paths = [frontend / "package.json", *list((frontend / "src").rglob("*"))]
    newest_source = max((path.stat().st_mtime for path in source_paths if path.is_file()), default=0)
    if not index.exists() or index.stat().st_mtime < newest_source:
        subprocess.run(["npm", "run", "build"], cwd=frontend, check=True)


def _open_when_ready(host: str, port: int, url: str) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                webbrowser.open(url)
                return
        except OSError:
            time.sleep(0.2)


def _verify_demo(project_root: Path) -> int:
    frontend = project_root / "frontend"
    _ensure_frontend_dependencies(frontend)
    checks = [
        # Use the interpreter that owns the people-intel CLI.  Calling a bare
        # ``pytest`` can silently select another Conda/uv environment and make
        # the verification result depend on the user's PATH.
        ([sys.executable, "-m", "pytest", "-q"], project_root),
        (["npm", "run", "typecheck"], frontend),
        (["npm", "test"], frontend),
        (["npm", "run", "build"], frontend),
    ]
    for command, cwd in checks:
        result = subprocess.run(command, cwd=cwd, check=False)
        if result.returncode:
            return result.returncode
    committed = project_root / "docs" / "openapi.json"
    current = create_app(build_sandbox_service(), serve_frontend=False).openapi()
    expected = json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if not committed.exists() or committed.read_text(encoding="utf-8") != expected:
        print("OpenAPI drift detected. Run: people-intel export-openapi", flush=True)
        return 2
    with tempfile.TemporaryDirectory(prefix="people-intel-contract-") as directory:
        generated = Path(directory) / "api-schema.d.ts"
        result = subprocess.run(
            [str(frontend / "node_modules" / ".bin" / "openapi-typescript"), str(committed), "-o", str(generated)],
            cwd=frontend,
            check=False,
        )
        if result.returncode:
            return result.returncode
        checked_in = frontend / "src" / "api-schema.d.ts"
        if not checked_in.exists() or checked_in.read_text(encoding="utf-8") != generated.read_text(encoding="utf-8"):
            print("Generated TypeScript contract drift detected. Run: npm --prefix frontend run generate:api", flush=True)
            return 3
    verification = project_root / ".people_intel" / "demo-verification.json"
    verification.parent.mkdir(parents=True, exist_ok=True)
    verification.write_text(json.dumps({"status": "passed", "verified_at": datetime.now(timezone.utc).isoformat(), "checks": [item[0] for item in checks] + ["openapi-drift", "typescript-contract-drift", "capability-coverage"]}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "passed", "verification": str(verification)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
