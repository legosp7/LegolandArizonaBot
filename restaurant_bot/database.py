from __future__ import annotations

import os
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))
    reviews: Mapped[list[Review]] = relationship(back_populates="reviewer")


class Restaurant(Base):
    __tablename__ = "restaurants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    reviews: Mapped[list[Review]] = relationship(back_populates="restaurant")


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_name: Mapped[str] = mapped_column(String(200))
    reviewer_id: Mapped[int] = mapped_column(BigInteger)
    creator_id: Mapped[int] = mapped_column(BigInteger)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reviews: Mapped[list[Review]] = relationship(back_populates="invitation")


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("invitation_id", "reviewer_id", name="uq_review_invitation_reviewer"),
        CheckConstraint("rating BETWEEN 1 AND 10", name="ck_review_rating_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invitation_id: Mapped[int] = mapped_column(ForeignKey("invitations.id", ondelete="CASCADE"))
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"))
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    rating: Mapped[int] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    invitation: Mapped[Invitation] = relationship(back_populates="reviews")
    restaurant: Mapped[Restaurant] = relationship(back_populates="reviews")
    reviewer: Mapped[User] = relationship(back_populates="reviews")


def create_session_factory(database_url: str | None = None) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    url = database_url or os.environ["DATABASE_URL"]
    engine = create_async_engine(url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
