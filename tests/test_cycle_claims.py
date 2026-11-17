import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from restaurant_bot.database import (
    ACTIVE_CYCLE_STATUSES, Cycle, Recommendation, User, Vote, WeeklyCycleClaim,
    claim_cycle, create_session_factory, create_tables,
)


class CycleClaimsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine, self.sessions = create_session_factory(
            f"sqlite+aiosqlite:///{Path(self.temp.name) / 'test.db'}"
        )
        self.monday = datetime(2026, 11, 16, 16, tzinfo=timezone.utc)
        self.week_start = self.monday.replace(hour=7)
        await create_tables(self.engine)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def claim(self, **overrides):
        values = dict(guild_id=1, channel_id=10, opened_at=self.monday,
                      deadline=self.monday + timedelta(hours=24))
        values.update(overrides)
        return await claim_cycle(self.sessions, **values)

    async def test_concurrent_automatic_openings_claim_only_once(self):
        results = await asyncio.gather(*[
            self.claim(week_start=self.week_start) for _ in range(4)
        ])
        self.assertEqual(sum(result is not None for result in results), 1)

    async def test_old_forced_cycle_blocks_monday_without_consuming_week(self):
        previous = await self.claim(opened_at=self.monday - timedelta(days=5),
                                    status="selected", deadline=None)
        self.assertIsNone(await self.claim(week_start=self.week_start))
        async with self.sessions.begin() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(WeeklyCycleClaim)), 0)
            await session.execute(update(Cycle).where(Cycle.id == previous.id).values(status="complete"))
        self.assertIsNotNone(await self.claim(week_start=self.week_start))

    async def test_force_replacement_is_atomic_and_preserves_old_rows(self):
        old = await self.claim()
        new = await self.claim(replace_active=True, status="selected", deadline=None)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Cycle, old.id)).status, "skipped")
            self.assertEqual((await session.get(Cycle, new.id)).status, "selected")
            count = await session.scalar(select(func.count()).select_from(Cycle).where(
                Cycle.status.in_(ACTIVE_CYCLE_STATUSES)))
            self.assertEqual(count, 1)

    async def test_completed_automatic_cycle_does_not_reopen_same_week(self):
        cycle = await self.claim(week_start=self.week_start)
        async with self.sessions.begin() as session:
            await session.execute(update(Cycle).where(Cycle.id == cycle.id).values(status="complete"))
        self.assertIsNone(await self.claim(week_start=self.week_start))

    async def test_same_week_manual_cycle_prevents_automatic_opening(self):
        cycle = await self.claim()
        async with self.sessions.begin() as session:
            await session.execute(update(Cycle).where(Cycle.id == cycle.id).values(status="skipped"))
        self.assertIsNone(await self.claim(week_start=self.week_start))

    async def test_other_servers_are_independent_and_direct_inserts_guarded(self):
        await self.claim()
        self.assertIsNotNone(await self.claim(guild_id=2))
        with self.assertRaises(IntegrityError):
            async with self.sessions.begin() as session:
                session.add(Cycle(guild_id=1, channel_id=10, opened_at=self.monday,
                                  status="rating", deadline=None))

    async def test_upgrade_keeps_newest_active_cycle_and_is_repeatable(self):
        async with self.engine.begin() as connection:
            await connection.exec_driver_sql("DROP INDEX uq_cycle_active_guild")
        older = await self.claim()
        newer = await self.claim(status="selected", deadline=None)
        await create_tables(self.engine)
        await create_tables(self.engine)
        async with self.sessions() as session:
            self.assertEqual((await session.get(Cycle, older.id)).status, "skipped")
            self.assertEqual((await session.get(Cycle, newer.id)).status, "selected")

    async def test_duplicate_start_does_not_send_second_announcement(self):
        from restaurant_bot.bot import RestaurantBot

        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
        client = SimpleNamespace(sessions=self.sessions,
                                 get_channel_messageable=AsyncMock(return_value=channel))
        first = await RestaurantBot.start_cycle(client, 1, 10, week_start=self.week_start)
        second = await RestaurantBot.start_cycle(client, 1, 10, week_start=self.week_start)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        channel.send.assert_awaited_once()

    async def test_invalid_force_vote_does_not_cancel_existing_cycle(self):
        import restaurant_bot.bot as app
        from discord import app_commands

        old = await self.claim()
        interaction = SimpleNamespace(guild_id=1, channel_id=10,
                                      response=SimpleNamespace(defer=AsyncMock()),
                                      followup=SimpleNamespace(send=AsyncMock()))
        with patch.object(app.bot, "sessions", self.sessions), patch.object(app, "require_admin_access", AsyncMock(return_value=True)):
            await app.force_open.callback(interaction, app_commands.Choice(name="Voting", value="voting"))
        async with self.sessions() as session:
            self.assertEqual((await session.get(Cycle, old.id)).status, "recommending")

    async def test_runoff_allows_one_choice_across_all_pages(self):
        import restaurant_bot.bot as app

        deadline = datetime.now(timezone.utc) + timedelta(hours=1)
        cycle = await self.claim(status="voting", deadline=deadline)
        async with self.sessions.begin() as session:
            saved = await session.get(Cycle, cycle.id)
            saved.runoff_round = 1
            session.add(User(id=99, display_name="Nominator"))
            options = [Recommendation(id=index, guild_id=1, name=f"Restaurant {index}",
                                      name_key=f"restaurant {index}", recommender_id=99,
                                      created_at=self.monday) for index in range(1, 27)]
            session.add_all(options)
            session.add(Vote(cycle_id=cycle.id, user_id=42, recommendation_id=26))

        view = app.VoteView(self.sessions, cycle.id, deadline, options, is_runoff=True)
        select_item = view.children[0]
        self.assertEqual(select_item.max_values, 1)
        select_item._values = ["1"]
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=42), message=None,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch.object(app, "has_voter_role", return_value=True):
            await select_item.callback(interaction)
        async with self.sessions() as session:
            votes = list((await session.scalars(select(Vote).where(
                Vote.cycle_id == cycle.id, Vote.user_id == 42))).all())
        self.assertEqual([vote.recommendation_id for vote in votes], [1])

    async def test_configured_admin_role_grants_access(self):
        import restaurant_bot.bot as app

        class FakeMember:
            def __init__(self, administrator=False, roles=()):
                self.guild_permissions = SimpleNamespace(administrator=administrator)
                self.roles = [SimpleNamespace(id=role) for role in roles]

        with patch.object(app.discord, "Member", FakeMember), patch.dict("os.environ", {"ADMIN_ROLE_ID": "123"}):
            self.assertTrue(app.has_admin_access(FakeMember(roles=[123])))
            self.assertTrue(app.has_admin_access(FakeMember(administrator=True)))
            self.assertFalse(app.has_admin_access(FakeMember(roles=[456])))


if __name__ == "__main__":
    unittest.main()
