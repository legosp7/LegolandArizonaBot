from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, inspect
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(AsyncAttrs, DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))


class Recommendation(Base):
    __tablename__ = "recommendations"
    __table_args__ = (UniqueConstraint("guild_id", "name_key", name="uq_recommendation_guild_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    name: Mapped[str] = mapped_column(String(200))
    name_key: Mapped[str] = mapped_column(String(200))
    recommender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Cycle(Base):
    __tablename__ = "cycles"
    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(20), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    winner_id: Mapped[int | None] = mapped_column(ForeignKey("recommendations.id"))
    event_id: Mapped[int | None] = mapped_column(BigInteger)
    runoff_round: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class CycleOption(Base):
    __tablename__ = "cycle_options"
    __table_args__ = (UniqueConstraint("cycle_id", "recommendation_id", name="uq_cycle_option"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("recommendations.id"))
    recommendation: Mapped[Recommendation] = relationship()


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("cycle_id", "user_id", "recommendation_id", name="uq_vote"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("recommendations.id"))


class Rating(Base):
    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("cycle_id", "user_id", name="uq_cycle_rating"), CheckConstraint("score BETWEEN 1 AND 10", name="ck_rating_score"))
    id: Mapped[int] = mapped_column(primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("cycles.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    score: Mapped[int] = mapped_column(Integer)


class RestaurantResult(Base):
    __tablename__ = "restaurant_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, index=True)
    restaurant_name: Mapped[str] = mapped_column(String(200), index=True)
    recommender_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    average_score: Mapped[float] = mapped_column(Float)
    rating_count: Mapped[int] = mapped_column(Integer)
    visited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    manual: Mapped[bool] = mapped_column(Boolean, default=False)
    recommender: Mapped[User] = relationship()


def create_session_factory(database_url: str | None = None) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    configured_path = os.getenv("DB_PATH", "").strip()
    configured_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        url: str | URL = database_url
    elif configured_path:
        db_path = Path(configured_path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        url = URL.create("sqlite+aiosqlite", database=str(db_path))
    elif configured_url:
        url = configured_url
    else:
        db_path = Path("bot.db")
        url = URL.create("sqlite+aiosqlite", database=str(db_path))
    engine = create_async_engine(url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

        def add_missing_columns(sync_connection: object) -> None:
            column_names = {column["name"] for column in inspect(sync_connection).get_columns("cycles")}  # type: ignore[arg-type]
            if "runoff_round" not in column_names:
                sync_connection.exec_driver_sql("ALTER TABLE cycles ADD COLUMN runoff_round INTEGER NOT NULL DEFAULT 0")  # type: ignore[attr-defined]

        await connection.run_sync(add_missing_columns)
