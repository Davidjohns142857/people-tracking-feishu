from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StoredObject:
    digest: str
    object_ref: str
    path: Path
    created: bool


class ContentAddressedStore:
    """Immutable SHA-256 object store.

    Existing objects are never rewritten. An object reference is stable across
    repeated ingestion and can be verified independently from database state.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    @staticmethod
    def digest_bytes(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def digest_text(content: str) -> str:
        return ContentAddressedStore.digest_bytes(content.encode("utf-8"))

    def put_bytes(self, content: bytes) -> StoredObject:
        digest = self.digest_bytes(content)
        relative = Path("sha256") / digest[:2] / digest[2:4] / digest
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".incoming-", delete=False) as handle:
                temporary = handle.name
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                # Hard-link publication is atomic and never replaces an
                # existing digest object, including under concurrent writers.
                os.link(temporary, path)
                created = True
            except FileExistsError:
                created = False
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)

        existing = path.read_bytes()
        if self.digest_bytes(existing) != digest:
            raise IOError(f"content-addressed object failed integrity check: {path}")
        return StoredObject(digest=digest, object_ref=relative.as_posix(), path=path, created=created)

    def put_text(self, content: str) -> StoredObject:
        return self.put_bytes(content.encode("utf-8"))

    def read_bytes(self, object_ref: str) -> bytes:
        path = self.root / object_ref
        content = path.read_bytes()
        expected = path.name
        actual = self.digest_bytes(content)
        if expected != actual:
            raise IOError(f"content hash mismatch for {object_ref}: expected {expected}, got {actual}")
        return content

    def read_text(self, object_ref: str) -> str:
        return self.read_bytes(object_ref).decode("utf-8")

    def verify(self, object_ref: str) -> bool:
        try:
            self.read_bytes(object_ref)
        except (OSError, UnicodeError):
            return False
        return True
