from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path

from people_intel.importers import AppleScholarsMarkdownImporter
from people_intel.schemas import (
    DirectoryWatchScanRequest,
    DirectoryWatchScanResponse,
    FetchMethod,
    SourceIngestionRequest,
    SourceType,
    TrackedFileResult,
)
from people_intel.service import MemoryValidationError, TemporalMemoryService


class DirectorySourceTracker:
    """Content-addressed directory tracking for local dossier bundles.

    Every file, including image attachments, is copied into the immutable
    object store. A stable JSON manifest becomes a normal SourceVersion, so a
    subsequent scan can distinguish changed, unchanged and removed paths.
    """

    def __init__(self, service: TemporalMemoryService):
        self.service = service

    def scan(self, request: DirectoryWatchScanRequest) -> DirectoryWatchScanResponse:
        root = Path(request.directory_path).expanduser().resolve()
        if not root.is_dir():
            raise MemoryValidationError(f"tracked directory does not exist: {root}")
        directory_uri = root.as_uri() + "/"
        previous = self.service.ledger.find_source_versions(directory_uri)
        old_files: dict[str, dict] = {}
        if previous:
            try:
                document = json.loads(self.service.object_store.read_text(previous[-1].raw_object_ref))
                old_files = {item["relative_path"]: item for item in document.get("files", [])}
            except (OSError, ValueError, KeyError):
                old_files = {}

        paths = root.rglob("*") if request.recursive else root.glob("*")
        files: list[TrackedFileResult] = []
        manifest_files: list[dict] = []
        markdown_paths: list[Path] = []
        for path in sorted((item for item in paths if item.is_file()), key=lambda item: item.relative_to(root).as_posix()):
            raw = path.read_bytes()
            stored = self.service.object_store.put_bytes(raw)
            relative = path.relative_to(root).as_posix()
            media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            prior = old_files.get(relative)
            status = "new" if prior is None else ("unchanged" if prior.get("content_hash") == stored.digest else "changed")
            item = TrackedFileResult(
                relative_path=relative,
                media_type=media_type,
                size_bytes=len(raw),
                content_hash=stored.digest,
                object_ref=stored.object_ref,
                status=status,
            )
            files.append(item)
            manifest_files.append(item.model_dump(mode="json", exclude={"status"}))
            if path.suffix.lower() in {".md", ".markdown"}:
                markdown_paths.append(path)

        manifest = json.dumps(
            {"directory_uri": directory_uri, "recursive": request.recursive, "files": manifest_files},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        manifest_response = self.service.ingest_source(SourceIngestionRequest(
            source_uri=directory_uri,
            source_type=SourceType.FILE,
            media_type="application/vnd.people-intel.directory-manifest+json",
            content=manifest,
            normalized_markdown=manifest,
            retrieved_at=request.observed_at,
            fetch_method=FetchMethod.UPLOAD,
            metadata={"watch_kind": "directory", "recursive": request.recursive},
        ))
        review_batch_ids: list[str] = []
        imported_people = 0
        if request.importer == "apple_scholars":
            if len(markdown_paths) != 1:
                raise MemoryValidationError(
                    f"apple_scholars directory must contain exactly one Markdown dossier; found {len(markdown_paths)}"
                )
            report = AppleScholarsMarkdownImporter(self.service).import_path(
                markdown_paths[0], observed_at=request.observed_at, created_by=request.created_by
            )
            review_batch_ids.append(report.review_batch_id)
            imported_people += report.counts["people"]
        current_paths = {item.relative_path for item in files}
        return DirectoryWatchScanResponse(
            directory_uri=directory_uri,
            manifest_source_version_id=manifest_response.source_version_id,
            version_status=manifest_response.version_status,
            files=files,
            removed_paths=sorted(set(old_files) - current_paths),
            review_batch_ids=review_batch_ids,
            imported_people=imported_people,
        )


__all__ = ["DirectorySourceTracker"]
