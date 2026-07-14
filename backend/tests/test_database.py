"""Database infrastructure tests."""

from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import SecretStr
from sqlalchemy import Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from backend.configuration.settings import PostgresSettings
from backend.db.engine import create_postgres_engine
from backend.db.health import PostgresHealthCheck
from backend.db.repositories.base import SQLAlchemyRepositoryBase
from backend.db.retry import RetryPolicy
from backend.db.session import DatabaseSessionManager


class Base(DeclarativeBase):
    """Test-only ORM base."""


class Widget(Base):
    """Test-only mapped model."""

    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)


class DummyResult:
    """Minimal SQLAlchemy result stub."""

    def __init__(self, rows: list[Widget]) -> None:
        self._rows = rows

    def scalars(self) -> DummyResult:
        return self

    def all(self) -> list[Widget]:
        return self._rows

    def one_or_none(self) -> Widget | None:
        return self._rows[0] if self._rows else None


class DummyTransaction:
    """Async transaction context for tests."""

    def __init__(self) -> None:
        self.entered = False
        self.exited = False
        self.failed = False

    async def __aenter__(self) -> DummyTransaction:
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> bool:
        self.exited = True
        self.failed = exc is not None
        return False


class DummySession:
    """Session double that records operations."""

    def __init__(self, row: Widget | None = None) -> None:
        self.added: list[Widget] = []
        self.deleted: list[Widget] = []
        self.flushed = False
        self.committed = False
        self.rolled_back = False
        self.refreshed: list[Widget] = []
        self.row = row or Widget(id=1, name="example")
        self.transaction = DummyTransaction()

    async def __aenter__(self) -> DummySession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object | None,
    ) -> bool:
        return False

    def begin(self) -> DummyTransaction:
        return self.transaction

    def add(self, instance: Widget) -> None:
        self.added.append(instance)

    async def delete(self, instance: Widget) -> None:
        self.deleted.append(instance)

    async def flush(self) -> None:
        self.flushed = True

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def refresh(self, instance: Widget) -> None:
        self.refreshed.append(instance)

    async def get(self, model: type[Widget], identity: object) -> Widget | None:
        return self.row if identity == self.row.id else None

    async def execute(self, statement: object) -> DummyResult:
        return DummyResult([self.row])


class WidgetRepository(SQLAlchemyRepositoryBase[Widget]):
    """Test repository built on the base class."""


@pytest.mark.asyncio
async def test_retry_policy_retries_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient failures should be retried with exponential backoff."""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    attempts = 0

    async def flaky_operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise ValueError("retry")
        return "ok"

    policy = RetryPolicy(
        max_attempts=3,
        initial_delay_seconds=0.1,
        max_delay_seconds=1.0,
        backoff_factor=2.0,
        retryable_exceptions=(ValueError,),
        sleep=fake_sleep,
    )

    result = await policy.run(flaky_operation)

    assert result == "ok"
    assert attempts == 2
    assert delays == [0.1]


@pytest.mark.asyncio
async def test_session_manager_provides_session_and_transaction_contexts() -> None:
    """The session manager should yield managed sessions and transactions."""
    session = DummySession()

    def factory() -> DummySession:
        return session

    manager = DatabaseSessionManager(
        engine=object(),  # type: ignore[arg-type]
        session_factory=factory,  # type: ignore[arg-type]
    )

    async with manager.session() as active_session:
        assert cast(DummySession, active_session) is session

    async with manager.transaction() as active_session:
        assert cast(DummySession, active_session) is session

    assert session.transaction.entered is True
    assert session.transaction.exited is True


@pytest.mark.asyncio
async def test_session_manager_transaction_marks_failure_on_exception() -> None:
    """Transaction context should surface failures to the underlying transaction."""
    session = DummySession()

    def factory() -> DummySession:
        return session

    manager = DatabaseSessionManager(
        engine=object(),  # type: ignore[arg-type]
        session_factory=factory,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError):
        async with manager.transaction():
            raise RuntimeError("boom")

    assert session.transaction.failed is True


@pytest.mark.asyncio
async def test_repository_base_wraps_sqlalchemy_session_operations() -> None:
    """Repository helpers should delegate to the underlying SQLAlchemy session."""
    widget = Widget(id=7, name="seven")
    session = DummySession(row=widget)
    repository = WidgetRepository(cast(AsyncSession, session), Widget)

    created = await repository.add(widget)
    fetched = await repository.get(7)
    listed = await repository.list()
    located = await repository.one_or_none(select(Widget))
    await repository.refresh(widget)
    await repository.delete(widget)
    await repository.commit()
    await repository.rollback()

    assert created is widget
    assert fetched is session.row
    assert listed == [session.row]
    assert located is session.row
    assert widget in session.added
    assert widget in session.deleted
    assert session.flushed is True
    assert session.committed is True
    assert session.rolled_back is True
    assert widget in session.refreshed


@pytest.mark.asyncio
async def test_postgres_health_check_retries_ping() -> None:
    """Postgres readiness should retry transient failures before succeeding."""
    attempts = 0

    class _Manager:
        async def ping(self) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 2:
                raise ValueError("transient")

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    policy = RetryPolicy(
        max_attempts=3,
        initial_delay_seconds=0.05,
        max_delay_seconds=1.0,
        retryable_exceptions=(ValueError,),
        sleep=fake_sleep,
    )
    check = PostgresHealthCheck(cast(DatabaseSessionManager, _Manager()), policy)

    await check.check()

    assert attempts == 2
    assert delays == [0.05]


def test_engine_factory_applies_pooling_settings() -> None:
    """The engine factory should translate typed settings into pool options."""
    settings = PostgresSettings(
        host="db.internal",
        port=5432,
        database="sentinel",
        username="svc_user",
        password=SecretStr("pg-pass"),
        pool_size=7,
        max_overflow=11,
        pool_timeout_seconds=4.5,
        pool_recycle_seconds=900,
    )

    engine = create_postgres_engine(settings)

    assert engine.url.render_as_string(hide_password=False) == settings.sqlalchemy_url
    pool = cast(Any, engine.sync_engine.pool)
    assert pool._pre_ping is True
    assert pool._pool.maxsize == 7
    assert pool._max_overflow == 11
    assert pool._timeout == 4.5
    assert pool._recycle == 900
    engine.sync_engine.dispose()
