from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from people_intel.schemas import CognifyRequest, SourceDocumentVersion


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class CognifyAdapterResult:
    status: str
    candidate_assertion_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CogneeSandboxConfig:
    root: Path
    enabled: bool = True
    dataset_name: str = "people-intel-sandbox"
    llm_provider: str = "gemini"
    llm_model: str = "gemini/gemini-3-flash-preview"
    llm_api_key: str = field(default="", repr=False, compare=False)
    llm_endpoint: str | None = None
    embedding_provider: str = "fastembed"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_dimensions: int = 384
    relational_db_provider: str = "sqlite"
    graph_db_provider: str = "kuzu"
    vector_db_provider: str = "lancedb"

    @classmethod
    def from_environment(cls, root: str | Path) -> "CogneeSandboxConfig":
        # Local secrets are loaded only into this process and never returned by
        # the public status API. Explicit process variables retain priority.
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        return cls(
            root=Path(root).resolve(),
            enabled=os.environ.get("PEOPLE_INTEL_COGNEE_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
            dataset_name=os.environ.get("PEOPLE_INTEL_COGNEE_DATASET", "people-intel-sandbox"),
            llm_provider=os.environ.get("PEOPLE_INTEL_COGNEE_LLM_PROVIDER", "gemini"),
            llm_model=os.environ.get("PEOPLE_INTEL_COGNEE_LLM_MODEL", "gemini/gemini-3-flash-preview"),
            llm_api_key=os.environ.get("PEOPLE_INTEL_COGNEE_LLM_API_KEY", ""),
            llm_endpoint=os.environ.get("PEOPLE_INTEL_COGNEE_LLM_ENDPOINT") or None,
            embedding_provider=os.environ.get("PEOPLE_INTEL_COGNEE_EMBEDDING_PROVIDER", "fastembed"),
            embedding_model=os.environ.get("PEOPLE_INTEL_COGNEE_EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
            embedding_dimensions=int(os.environ.get("PEOPLE_INTEL_COGNEE_EMBEDDING_DIMENSIONS", "384")),
        )

    @property
    def system_root(self) -> Path:
        return self.root / "system"

    @property
    def data_root(self) -> Path:
        return self.root / "data"

    @property
    def vector_db_path(self) -> Path:
        return self.system_root / "databases" / "people-intel.lancedb"


class CogneeAdapter:
    """Real Cognee projection/retrieval adapter for an isolated Sandbox.

    The Gemini LLM helps Cognee build and retrieve a temporal semantic
    projection. Embeddings and databases remain local. Output is candidate
    context only and never creates or confirms a people-intel Assertion.
    """

    def __init__(self, config: CogneeSandboxConfig):
        self.config = config
        self._configured = False
        # Cognee keeps asyncio primitives in module-level clients. Recreating
        # an event loop with asyncio.run() for every HTTP request makes the
        # second projection fail with "Lock ... bound to a different event
        # loop". A single Runner preserves loop identity; the lock also
        # serializes sync FastAPI workers against the non-thread-safe runner.
        self._runner = asyncio.Runner()
        self._runner_lock = threading.RLock()

    def runtime_status(self) -> dict[str, Any]:
        package_available = importlib.util.find_spec("cognee") is not None
        package_version = None
        if package_available:
            try:
                package_version = importlib.metadata.version("cognee")
            except importlib.metadata.PackageNotFoundError:
                package_version = "installed"
        credentials_configured = bool(self.config.llm_api_key)
        ready = bool(self.config.enabled and package_available and credentials_configured)
        if not self.config.enabled:
            status, detail = "unconfigured", "Cognee 已被 PEOPLE_INTEL_COGNEE_ENABLED 显式关闭。"
        elif not package_available:
            status, detail = "unavailable", "Cognee Python 依赖尚未安装。"
        elif not credentials_configured:
            status, detail = "degraded", "Cognee 已安装，但 Gemini API key 尚未配置。"
        else:
            status, detail = "ready", "Cognee 可真实运行：Gemini 负责抽取推理，FastEmbed 与 SQLite/Kuzu/LanceDB 保存在本地 Sandbox。"
        return {
            "status": status,
            "configured": self.config.enabled,
            "ready": ready,
            "package_version": package_version,
            "dataset_name": self.config.dataset_name,
            "llm_provider": self.config.llm_provider,
            "llm_model": self.config.llm_model,
            "llm_endpoint": self.config.llm_endpoint,
            "credentials_configured": credentials_configured,
            "embedding_provider": self.config.embedding_provider,
            "embedding_model": self.config.embedding_model,
            "embedding_dimensions": self.config.embedding_dimensions,
            "relational_db_provider": self.config.relational_db_provider,
            "graph_db_provider": self.config.graph_db_provider,
            "vector_db_provider": self.config.vector_db_provider,
            "system_root": str(self.config.system_root),
            "data_root": str(self.config.data_root),
            "vector_db_path": str(self.config.vector_db_path),
            "storage": self._storage_snapshot(),
            "detail_zh": detail,
            "authority": "projection_only",
        }

    def cognify(
        self,
        *,
        source: SourceDocumentVersion,
        content: str,
        request: CognifyRequest,
    ) -> CognifyAdapterResult:
        cognee = self._configure_runtime()
        before = self._storage_snapshot()
        started = time.perf_counter()
        dataset_name = request.dataset_name or self.config.dataset_name
        projection_documents = _projection_documents(content)

        async def run() -> Any:
            return await cognee.remember(
                projection_documents if len(projection_documents) > 1 else projection_documents[0],
                dataset_name=dataset_name,
                temporal_cognify=request.temporal,
                self_improvement=False,
            )

        result = self._run_async(run())
        return CognifyAdapterResult(
            status="completed",
            metadata={
                "source_version_id": source.source_version_id,
                "dataset_name": dataset_name,
                "content_hash": source.content_hash,
                "projection_document_count": len(projection_documents),
                "projection_document_sizes": [len(item) for item in projection_documents],
                "temporal_cognify": request.temporal,
                "self_improvement": False,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "result_type": type(result).__name__,
                "result_preview": _jsonable(result),
                "storage_before": before,
                "storage_after": self._storage_snapshot(),
                "runtime": self.runtime_status(),
                "authority": "projection_only",
            },
        )

    def recall(
        self,
        *,
        query_text: str,
        dataset_name: str | None = None,
        top_k: int = 10,
        query_type: str = "TEMPORAL",
    ) -> dict[str, Any]:
        cognee = self._configure_runtime()
        from cognee.api.v1.search import SearchType

        started = time.perf_counter()
        selected_dataset = dataset_name or self.config.dataset_name
        selected_type = SearchType(query_type)

        async def run() -> Any:
            return await cognee.recall(
                query_type=selected_type,
                query_text=query_text,
                datasets=[selected_dataset],
                top_k=top_k,
            )

        result = self._run_async(run())
        serialized = _jsonable(result)
        items = serialized if isinstance(serialized, list) else [serialized]
        return {
            "query_text": query_text,
            "dataset_name": selected_dataset,
            "top_k": top_k,
            "query_type": selected_type.value,
            "result_count": len(items),
            "results": items,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "authority": "retrieval_projection_only",
        }

    def _run_async(self, awaitable):
        with self._runner_lock:
            return self._runner.run(awaitable)

    def _configure_runtime(self):
        status = self.runtime_status()
        if not status["ready"]:
            raise RuntimeError(status["detail_zh"])
        self.config.system_root.mkdir(parents=True, exist_ok=True)
        self.config.data_root.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("TELEMETRY_DISABLED", "true")
        os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] = "false"
        os.environ["REQUIRE_AUTHENTICATION"] = "false"
        os.environ["SYSTEM_ROOT_DIRECTORY"] = str(self.config.system_root)
        os.environ["DATA_ROOT_DIRECTORY"] = str(self.config.data_root)
        os.environ["LLM_PROVIDER"] = self.config.llm_provider
        os.environ["LLM_MODEL"] = self.config.llm_model
        os.environ["LLM_API_KEY"] = self.config.llm_api_key
        if self.config.llm_provider == "gemini":
            os.environ["GEMINI_API_KEY"] = self.config.llm_api_key
            os.environ["GOOGLE_API_KEY"] = self.config.llm_api_key
        os.environ["EMBEDDING_PROVIDER"] = self.config.embedding_provider
        os.environ["EMBEDDING_MODEL"] = self.config.embedding_model
        os.environ["EMBEDDING_DIMENSIONS"] = str(self.config.embedding_dimensions)
        os.environ["GRAPH_DATABASE_PROVIDER"] = self.config.graph_db_provider
        os.environ["VECTOR_DB_PROVIDER"] = self.config.vector_db_provider
        os.environ["VECTOR_DB_URL"] = str(self.config.vector_db_path)
        import cognee

        if self._configured:
            return cognee
        cognee.config.set("system_root_directory", str(self.config.system_root))
        cognee.config.set("data_root_directory", str(self.config.data_root))
        cognee.config.set("llm_provider", self.config.llm_provider)
        cognee.config.set("llm_model", self.config.llm_model)
        cognee.config.set("llm_api_key", self.config.llm_api_key)
        if self.config.llm_endpoint:
            cognee.config.set("llm_endpoint", self.config.llm_endpoint)
        cognee.config.set("embedding_provider", self.config.embedding_provider)
        cognee.config.set("embedding_model", self.config.embedding_model)
        cognee.config.set("embedding_dimensions", self.config.embedding_dimensions)
        cognee.config.set_relational_db_config({"db_provider": self.config.relational_db_provider})
        cognee.config.set("graph_database_provider", self.config.graph_db_provider)
        cognee.config.set("vector_db_provider", self.config.vector_db_provider)
        cognee.config.set("vector_db_url", str(self.config.vector_db_path))
        self._configured = True
        return cognee

    def _storage_snapshot(self) -> dict[str, Any]:
        paths = [path for path in self.config.root.rglob("*") if path.is_file()] if self.config.root.exists() else []
        return {
            "file_count": len(paths),
            "total_bytes": sum(path.stat().st_size for path in paths),
            "sample_paths": [str(path.relative_to(self.config.root)) for path in sorted(paths)[:12]],
        }


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _projection_documents(content: str, *, max_chars: int = 7000) -> list[str]:
    """Split large dossiers before temporal extraction.

    Cognee's temporal pipeline asks the LLM for structured events. Sending a
    multi-person dossier as one document can create an enormous JSON response
    that is both expensive and brittle. Markdown person sections are therefore
    projected as separate documents, with the active year heading copied into
    each section. Smaller sources remain byte-for-byte unchanged.
    """
    if len(content) <= max_chars:
        return [content]
    headings = list(re.finditer(r"^(#{1,2})\s+.+$", content, flags=re.MULTILINE))
    year_heading = ""
    sections: list[str] = []
    for index, heading in enumerate(headings):
        if heading.group(1) == "#":
            year_heading = heading.group(0).strip()
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        section = content[heading.start():end].strip()
        if year_heading:
            section = f"{year_heading}\n\n{section}"
        sections.extend(_bounded_text_chunks(section, max_chars=max_chars))
    if sections:
        return sections
    return _bounded_text_chunks(content, max_chars=max_chars)


def _bounded_text_chunks(content: str, *, max_chars: int) -> list[str]:
    if len(content) <= max_chars:
        return [content]
    paragraphs = re.split(r"\n\s*\n", content)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(paragraph[start:start + max_chars] for start in range(0, len(paragraph), max_chars))
            continue
        proposed = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(proposed) > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = proposed
    if current:
        chunks.append(current)
    return [item for item in chunks if item]
