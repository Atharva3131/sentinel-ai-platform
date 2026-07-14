"""SQLAlchemy declarative metadata base."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base for future persistence mappings; contains no business behavior."""
