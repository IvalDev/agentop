"""Dual-backend vector column: pgvector on PostgreSQL, JSON elsewhere.

Decision A3 targets PostgreSQL + pgvector, but the test suite runs on
SQLite, which cannot ``CREATE EXTENSION vector`` (baseline migrations are
T12's). :class:`EmbeddingVector` switches implementation per dialect
through ``load_dialect_impl``: ``Vector(dim)`` on PostgreSQL, ``JSON``
everywhere else — on SQLite that is a TEXT column holding a JSON list of
floats, round-tripped by SQLAlchemy's JSON codecs.

The DDL dimension must match ``settings.embedding_dim``; the column's
default (768) equals the settings default, and the production DDL lives
in T12's baseline migration.
"""

from sqlalchemy import JSON, TypeDecorator
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeEngine

DEFAULT_EMBEDDING_DIM = 768


class EmbeddingVector(TypeDecorator):
    """Vector storage that adapts to the executing dialect."""

    impl = JSON
    cache_ok = True

    def __init__(self, dim: int = DEFAULT_EMBEDDING_DIM) -> None:
        super().__init__()
        self.dim = dim

    @property
    def python_type(self) -> type:
        return list

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine:
        if dialect.name == "postgresql":
            # Lazy import: SQLite-only environments never resolve pgvector.
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector(self.dim))
        return dialect.type_descriptor(JSON())
