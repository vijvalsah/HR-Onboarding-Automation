from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone-aware UTC now. Used as a default for timestamp columns."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class TimestampMixin:
    """Adds created_at / updated_at to a model.

    updated_at refreshes on every UPDATE via onupdate, giving us free audit
    timestamps without touching every write site.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
