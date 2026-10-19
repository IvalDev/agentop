"""Document blob storage abstraction.

Decision A4 puts raw files in MinIO (S3-compatible); that adapter and its
credentials wiring land in T12. The MVP and the test suite use
:class:`LocalDocumentStorage`. Keys follow ``{tenant_id}/{document_id}/
{filename}`` so a future bucket-per-tenant policy is a key-prefix change,
not a call-site change.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class DocumentStorage(Protocol):
    """Async byte-blob store keyed by storage key."""

    async def put(self, key: str, data: bytes) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class LocalDocumentStorage:
    """Filesystem-backed store used for the MVP and tests.

    File IO is synchronous inside the ``async`` methods: local disk
    operations are short and the MinIO adapter (T12) is the production
    path. Keys that would escape the base directory are rejected.
    """

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    def _resolve(self, key: str) -> Path:
        path = (self.base_dir / key).resolve()
        if not path.is_relative_to(self.base_dir.resolve()):
            raise ValueError(f"storage key escapes base directory: {key}")
        return path

    async def put(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    async def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)
