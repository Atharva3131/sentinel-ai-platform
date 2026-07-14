"""Generic repository base classes."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession


class RepositoryBase:
    """Common repository behavior shared by all SQLAlchemy repositories."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        """Expose the underlying SQLAlchemy session to subclasses."""
        return self._session

    async def flush(self) -> None:
        """Flush pending changes to the database."""
        await self._session.flush()

    async def commit(self) -> None:
        """Commit the active transaction."""
        await self._session.commit()

    async def rollback(self) -> None:
        """Rollback the active transaction."""
        await self._session.rollback()

    async def refresh(self, instance: Any) -> None:
        """Refresh an ORM instance from the database."""
        await self._session.refresh(instance)


class SQLAlchemyRepositoryBase[ModelT](RepositoryBase):
    """Generic repository with common SQLAlchemy persistence helpers."""

    def __init__(self, session: AsyncSession, model: type[ModelT]) -> None:
        super().__init__(session)
        self.model = model

    async def add(self, instance: ModelT) -> ModelT:
        """Stage an instance for persistence and flush it."""
        self.session.add(instance)
        await self.flush()
        return instance

    async def delete(self, instance: ModelT) -> None:
        """Remove an instance from the current session."""
        await self.session.delete(instance)

    async def get(self, identity: Any) -> ModelT | None:
        """Load one entity by primary key."""
        return await self.session.get(self.model, identity)

    async def list(
        self,
        statement: Select[tuple[ModelT]] | None = None,
    ) -> list[ModelT]:
        """Return all rows for the given statement or model."""
        query = statement if statement is not None else select(self.model)
        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def one_or_none(
        self,
        statement: Select[tuple[ModelT]],
    ) -> ModelT | None:
        """Return a single row when the query is expected to be unique."""
        result = await self.session.execute(statement)
        return result.scalars().one_or_none()
