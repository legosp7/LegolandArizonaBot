from __future__ import annotations

import os
import secrets
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .database import Cycle, CycleOption, Rating, Recommendation, RestaurantResult, User, Vote, create_session_factory, create_tables

load_dotenv()
UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def role_id() -> int | None:
    value = os.getenv("VOTER_ROLE_ID", "").strip()
    return int(value) if value else None


def has_voter_role(member: discord.abc.User) -> bool:
    required = role_id()
    return isinstance(member, discord.Member) and required is not None and any(role.id == required for role in member.roles)


def ping() -> str:
    required = role_id()
    return f"<@&{required}>" if required else "[voter role not configured]"


async def save_user(session: AsyncSession, member: discord.abc.User) -> None:
    user = await session.get(User, member.id)
    if user:
        user.display_name = member.display_name
    else:
        session.add(User(id=member.id, display_name=member.display_name))


async def build_vote_tally_embed(
    sessions: async_sessionmaker[AsyncSession],
    cycle_id: int,
    options: list[Recommendation],
    page: int,
    deadline: datetime,
) -> discord.Embed:
    page_count = max(1, (len(options) + 24) // 25)
    page = min(max(page, 0), page_count - 1)
    visible = options[page * 25:(page + 1) * 25]
    ids = [item.id for item in visible]
    async with sessions() as session:
        cycle = await session.get(Cycle, cycle_id)
        rows = (await session.execute(
            select(Vote.recommendation_id, func.count(Vote.id))
            .where(Vote.cycle_id == cycle_id, Vote.recommendation_id.in_(ids))
            .group_by(Vote.recommendation_id)
        )).all() if ids else []
    counts = {recommendation_id: count for recommendation_id, count in rows}
    embed = discord.Embed(
        title="Live Restaurant Runoff Tally" if cycle and cycle.runoff_round else "Live Restaurant Vote Tally",
        description="\n".join(
            f"**{item.name[:100]}** — {counts.get(item.id, 0)} vote{'s' if counts.get(item.id, 0) != 1 else ''}"
            for item in visible
        ) or "No restaurants are available on this page.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Voting closes", value=discord.utils.format_dt(deadline, "R"), inline=False)
    embed.set_footer(text=f"Live tally · Page {page + 1}/{page_count} · Updated as ballots are submitted")
    return embed


class VoteSelect(discord.ui.Select):
    def __init__(self, options: list[Recommendation]) -> None:
        self.page_ids = [item.id for item in options]
        super().__init__(
            custom_id="weekly-restaurant-vote",
            placeholder="Select every restaurant you would be happy with",
            min_values=1,
            max_values=len(options),
            options=[discord.SelectOption(label=item.name[:100], value=str(item.id)) for item in options],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, VoteView):
            return
        if not has_voter_role(interaction.user):
            await interaction.response.send_message("Only members with the configured voter role may vote.", ephemeral=True)
            return
        if now_utc() >= view.deadline:
            await interaction.response.send_message("Voting has closed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        choices = {int(value) for value in self.values}
        async with view.sessions.begin() as session:
            await session.execute(delete(Vote).where(Vote.cycle_id == view.cycle_id, Vote.user_id == interaction.user.id, Vote.recommendation_id.in_(self.page_ids)))
            session.add_all(Vote(cycle_id=view.cycle_id, user_id=interaction.user.id, recommendation_id=item) for item in choices)
        if interaction.message:
            embed = await build_vote_tally_embed(view.sessions, view.cycle_id, view.options, view.page, view.deadline)
            try:
                await interaction.message.edit(embed=embed, view=view)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
        await interaction.followup.send(f"Saved {len(choices)} choice{'s' if len(choices) != 1 else ''} on this page. Submit again to replace this page's choices.", ephemeral=True)


class VoteView(discord.ui.View):
    def __init__(self, sessions: async_sessionmaker[AsyncSession], cycle_id: int, deadline: datetime, options: list[Recommendation]) -> None:
        super().__init__(timeout=max(1, (deadline - now_utc()).total_seconds()))
        self.sessions, self.cycle_id, self.deadline = sessions, cycle_id, deadline
        self.options = options
        self.page = 0
        self.render_page()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.options) + 24) // 25)

    def render_page(self) -> None:
        self.clear_items()
        start = self.page * 25
        self.add_item(VoteSelect(self.options[start:start + 25]))
        if self.page_count > 1:
            previous = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=self.page == 0, custom_id="vote-page-previous")
            next_page = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=self.page >= self.page_count - 1, custom_id="vote-page-next")
            previous.callback = self.previous_page
            next_page.callback = self.next_page
            self.add_item(previous)
            self.add_item(next_page)

    async def previous_page(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self.render_page()
        embed = await build_vote_tally_embed(self.sessions, self.cycle_id, self.options, self.page, self.deadline)
        await interaction.response.edit_message(content=f"{ping()} Voting closes {discord.utils.format_dt(self.deadline, 'R')}. Page {self.page + 1}/{self.page_count}; selections are saved per page.", embed=embed, view=self)

    async def next_page(self, interaction: discord.Interaction) -> None:
        self.page = min(self.page_count - 1, self.page + 1)
        self.render_page()
        embed = await build_vote_tally_embed(self.sessions, self.cycle_id, self.options, self.page, self.deadline)
        await interaction.response.edit_message(content=f"{ping()} Voting closes {discord.utils.format_dt(self.deadline, 'R')}. Page {self.page + 1}/{self.page_count}; selections are saved per page.", embed=embed, view=self)


class RatingSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(custom_id="weekly-restaurant-rating", placeholder="Rate the restaurant (1–10)", options=[discord.SelectOption(label=f"{score}/10", value=str(score)) for score in range(1, 11)])

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, RatingView):
            return
        if not has_voter_role(interaction.user):
            await interaction.response.send_message("Only members with the configured voter role may rate.", ephemeral=True)
            return
        if now_utc() >= view.deadline:
            await interaction.response.send_message("Rating has closed.", ephemeral=True)
            return
        score = int(self.values[0])
        async with view.sessions.begin() as session:
            row = await session.scalar(select(Rating).where(Rating.cycle_id == view.cycle_id, Rating.user_id == interaction.user.id))
            if row:
                row.score = score
            else:
                session.add(Rating(cycle_id=view.cycle_id, user_id=interaction.user.id, score=score))
        await interaction.response.send_message(f"Saved your {score}/10 rating. Submit again to change it.", ephemeral=True)


class RatingView(discord.ui.View):
    def __init__(self, sessions: async_sessionmaker[AsyncSession], cycle_id: int, deadline: datetime) -> None:
        super().__init__(timeout=max(1, (deadline - now_utc()).total_seconds()))
        self.sessions, self.cycle_id, self.deadline = sessions, cycle_id, deadline
        self.add_item(RatingSelect())


class RemoveSelect(discord.ui.Select):
    def __init__(self, bot: RestaurantBot, owner_id: int, recommendations: list[Recommendation]) -> None:
        self.bot, self.owner_id = bot, owner_id
        super().__init__(placeholder="Choose a recommendation to remove", options=[discord.SelectOption(label=item.name[:100], value=str(item.id)) for item in recommendations])

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This menu belongs to another user.", ephemeral=True)
            return
        recommendation_id = int(self.values[0])
        async with self.bot.sessions.begin() as session:
            item = await session.get(Recommendation, recommendation_id)
            if not item or item.recommender_id != self.owner_id:
                await interaction.response.send_message("That recommendation no longer exists.", ephemeral=True)
                return
            name = item.name
            await session.delete(item)
        await interaction.response.edit_message(content=f"Removed **{name}**.", view=None)


class RestaurantBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.engine, self.sessions = create_session_factory()
        self.tz = ZoneInfo(os.getenv("BOT_TIMEZONE", "America/Phoenix"))

    async def setup_hook(self) -> None:
        await create_tables(self.engine)
        guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        await self.restore_views()
        self.scheduler.start()

    async def close(self) -> None:
        self.scheduler.cancel()
        await super().close()
        await self.engine.dispose()

    async def get_channel_messageable(self, channel_id: int) -> discord.abc.Messageable | None:
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        return channel if isinstance(channel, discord.abc.Messageable) else None

    async def restore_views(self) -> None:
        async with self.sessions() as session:
            cycles = (await session.scalars(select(Cycle).where(Cycle.status.in_(["voting", "rating"])))).all()
            for cycle in cycles:
                deadline = aware(cycle.deadline)
                if not deadline or deadline <= now_utc() or not cycle.message_id:
                    continue
                if cycle.status == "voting":
                    options = (await session.scalars(select(Recommendation).join(CycleOption, CycleOption.recommendation_id == Recommendation.id).where(CycleOption.cycle_id == cycle.id).order_by(Recommendation.name))).all()
                    if options:
                        view: discord.ui.View = VoteView(self.sessions, cycle.id, deadline, list(options))
                    else:
                        continue
                else:
                    view = RatingView(self.sessions, cycle.id, deadline)
                channel = await self.get_channel_messageable(cycle.channel_id)
                if channel and hasattr(channel, "fetch_message"):
                    try:
                        message = await channel.fetch_message(cycle.message_id)  # type: ignore[attr-defined]
                        if cycle.status == "voting" and isinstance(view, VoteView):
                            embed = await build_vote_tally_embed(self.sessions, cycle.id, view.options, view.page, deadline)
                            await message.edit(embed=embed, view=view)
                        else:
                            await message.edit(view=view)
                    except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                        pass

    @tasks.loop(minutes=1)
    async def scheduler(self) -> None:
        await self.advance_expired()
        await self.open_monday_cycles()

    @scheduler.before_loop
    async def before_scheduler(self) -> None:
        await self.wait_until_ready()

    async def open_monday_cycles(self) -> None:
        local = datetime.now(self.tz)
        if local.weekday() != 0 or local.hour < int(os.getenv("MONDAY_HOUR", "9")):
            return
        channel_env = os.getenv("ANNOUNCEMENT_CHANNEL_ID", "").strip()
        for guild in self.guilds:
            monday = (local - timedelta(days=local.weekday())).date()
            start = datetime.combine(monday, time.min, self.tz).astimezone(UTC)
            async with self.sessions() as session:
                exists = await session.scalar(select(Cycle.id).where(Cycle.guild_id == guild.id, Cycle.opened_at >= start))
                previous_channel = await session.scalar(select(Cycle.channel_id).where(Cycle.guild_id == guild.id).order_by(Cycle.id.desc()).limit(1))
            if exists:
                continue
            channel_id = int(channel_env) if channel_env else previous_channel
            if channel_id:
                await self.start_cycle(guild.id, channel_id)

    async def start_cycle(self, guild_id: int, channel_id: int, duration: timedelta = timedelta(hours=24)) -> Cycle | None:
        channel = await self.get_channel_messageable(channel_id)
        if not channel:
            return None
        opened, deadline = now_utc(), now_utc() + duration
        async with self.sessions.begin() as session:
            cycle = Cycle(guild_id=guild_id, channel_id=channel_id, status="recommending", opened_at=opened, deadline=deadline)
            session.add(cycle)
            await session.flush()
            cycle_id = cycle.id
        await channel.send(f"{ping()} Restaurant recommendations are open for {discord.utils.format_dt(deadline, 'R')}! Use `/recommend` (maximum two active recommendations each).", allowed_mentions=discord.AllowedMentions(roles=True))
        async with self.sessions() as session:
            return await session.get(Cycle, cycle_id)

    async def advance_expired(self) -> None:
        async with self.sessions() as session:
            cycles = (await session.scalars(select(Cycle).where(Cycle.deadline.is_not(None), Cycle.deadline <= now_utc(), Cycle.status.in_(["recommending", "voting", "rating"])))).all()
            pending = [(cycle.id, cycle.status) for cycle in cycles]
        for cycle_id, status in pending:
            if status == "recommending":
                await self.open_vote(cycle_id)
            elif status == "voting":
                await self.close_vote(cycle_id)
            else:
                await self.close_rating(cycle_id)

    async def open_vote(self, cycle_id: int, duration: timedelta = timedelta(hours=36)) -> None:
        async with self.sessions.begin() as session:
            cycle = await session.get(Cycle, cycle_id)
            if not cycle or cycle.status != "recommending":
                return
            options = list((await session.scalars(select(Recommendation).where(Recommendation.guild_id == cycle.guild_id).order_by(Recommendation.name))).all())
            channel_id = cycle.channel_id
            if not options:
                cycle.status, cycle.deadline = "complete", None
                message = f"{ping()} No restaurants were recommended this week, so the cycle is closed."
                view = None
            else:
                session.add_all(CycleOption(cycle_id=cycle.id, recommendation_id=item.id) for item in options)
                cycle.status, cycle.deadline = "voting", now_utc() + duration
                pages = max(1, (len(options) + 24) // 25)
                message = f"{ping()} Voting closes {discord.utils.format_dt(cycle.deadline, 'R')}. Page 1/{pages}; select as many restaurants as you want. Selections are saved per page."
                view = VoteView(self.sessions, cycle.id, cycle.deadline, options)
        channel = await self.get_channel_messageable(channel_id)
        if channel:
            embed = await build_vote_tally_embed(self.sessions, cycle_id, options, 0, view.deadline) if isinstance(view, VoteView) else None
            if embed:
                sent = await channel.send(message, embed=embed, view=view, allowed_mentions=discord.AllowedMentions(roles=True))
            else:
                sent = await channel.send(message, view=view, allowed_mentions=discord.AllowedMentions(roles=True))
            if view:
                async with self.sessions.begin() as session:
                    saved = await session.get(Cycle, cycle_id)
                    if saved:
                        saved.message_id = sent.id

    async def close_vote(self, cycle_id: int) -> None:
        async with self.sessions.begin() as session:
            cycle = await session.get(Cycle, cycle_id)
            if not cycle or cycle.status != "voting":
                return
            channel_id, message_id = cycle.channel_id, cycle.message_id
            rows = (await session.execute(select(Recommendation, func.count(Vote.id)).join(CycleOption, CycleOption.recommendation_id == Recommendation.id).outerjoin(Vote, (Vote.recommendation_id == Recommendation.id) & (Vote.cycle_id == cycle.id)).where(CycleOption.cycle_id == cycle.id).group_by(Recommendation.id).order_by(func.count(Vote.id).desc(), Recommendation.created_at))).all()
            if not rows:
                cycle.status, cycle.deadline = "complete", None
                outcome = "empty"
                winner = None
                winning_votes = 0
                runoff_options: list[Recommendation] = []
                decided_by_coin = False
            else:
                winning_votes = rows[0][1]
                tied = [restaurant for restaurant, count in rows if count == winning_votes]
                decided_by_coin = len(rows) == 2 and len(tied) == 2 and (cycle.runoff_round or 0) >= 1
                if len(tied) == 1 or decided_by_coin:
                    winner = tied[0] if len(tied) == 1 else secrets.choice(tied)
                    cycle.winner_id, cycle.status, cycle.deadline = winner.id, "selected", None
                    outcome = "winner"
                    runoff_options = []
                else:
                    winner = None
                    outcome = "runoff"
                    runoff_options = tied
                    await session.execute(delete(Vote).where(Vote.cycle_id == cycle.id))
                    await session.execute(delete(CycleOption).where(CycleOption.cycle_id == cycle.id))
                    session.add_all(CycleOption(cycle_id=cycle.id, recommendation_id=item.id) for item in tied)
                    cycle.runoff_round = (cycle.runoff_round or 0) + 1
                    cycle.deadline = now_utc() + timedelta(hours=1)
                    cycle.message_id = None
            guild_id = cycle.guild_id

        channel = await self.get_channel_messageable(channel_id)
        if channel and message_id and hasattr(channel, "fetch_message"):
            try:
                voting_message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
                await voting_message.delete()
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
        if outcome == "empty":
            if channel:
                await channel.send(f"{ping()} Voting closed without any available restaurants.", allowed_mentions=discord.AllowedMentions(roles=True))
            return
        if outcome == "runoff":
            if not channel:
                return
            async with self.sessions() as session:
                saved_cycle = await session.get(Cycle, cycle_id)
                runoff_deadline = aware(saved_cycle.deadline) if saved_cycle else None
                runoff_round = saved_cycle.runoff_round if saved_cycle else 1
            if not runoff_deadline:
                return
            view = VoteView(self.sessions, cycle_id, runoff_deadline, runoff_options)
            embed = await build_vote_tally_embed(self.sessions, cycle_id, runoff_options, 0, runoff_deadline)
            shown = runoff_options[:15]
            names = ", ".join(f"**{item.name[:80]}**" for item in shown)
            if len(runoff_options) > len(shown):
                names += f", and {len(runoff_options) - len(shown)} more"
            message = await channel.send(
                f"{ping()} The vote tied between {names}. Runoff round {runoff_round} is open for one hour.",
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
            async with self.sessions.begin() as session:
                saved_cycle = await session.get(Cycle, cycle_id)
                if saved_cycle and saved_cycle.status == "voting":
                    saved_cycle.message_id = message.id
            return
        event_text = await self.create_event(guild_id, winner.name)
        if channel:
            method = " The runoff remained tied, so the winner was chosen by coin flip." if decided_by_coin else ""
            await channel.send(f"{ping()} **{winner.name}** won with **{winning_votes}** vote{'s' if winning_votes != 1 else ''}!{method}{event_text}", allowed_mentions=discord.AllowedMentions(roles=True))

    async def refresh_vote_message(self, cycle_id: int) -> None:
        async with self.sessions() as session:
            cycle = await session.get(Cycle, cycle_id)
            if not cycle or cycle.status != "voting" or not cycle.message_id:
                return
            options = list((await session.scalars(
                select(Recommendation)
                .join(CycleOption, CycleOption.recommendation_id == Recommendation.id)
                .where(CycleOption.cycle_id == cycle.id)
                .order_by(Recommendation.name)
            )).all())
            deadline = aware(cycle.deadline)
            channel_id, message_id = cycle.channel_id, cycle.message_id
        if not options or not deadline:
            await self.close_vote(cycle_id)
            return
        channel = await self.get_channel_messageable(channel_id)
        if not channel or not hasattr(channel, "fetch_message"):
            return
        view = VoteView(self.sessions, cycle_id, deadline, options)
        embed = await build_vote_tally_embed(self.sessions, cycle_id, options, 0, deadline)
        try:
            message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
            await message.edit(
                content=f"{ping()} Voting closes {discord.utils.format_dt(deadline, 'R')}. Page 1/{view.page_count}; selections are saved per page.",
                embed=embed,
                view=view,
                allowed_mentions=discord.AllowedMentions(roles=True),
            )
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass

    async def create_event(self, guild_id: int, restaurant: str) -> str:
        guild = self.get_guild(guild_id)
        if not guild:
            return ""
        local = datetime.now(self.tz)
        day = int(os.getenv("EVENT_DAY", "4"))
        days = (day - local.weekday()) % 7
        event_hour = int(os.getenv("EVENT_HOUR", "18"))
        event_minute = int(os.getenv("EVENT_MINUTE", "0"))
        if days == 0 and (local.hour, local.minute) >= (event_hour, event_minute):
            days = 7
        start_local = (local + timedelta(days=days)).replace(hour=event_hour, minute=event_minute, second=0, microsecond=0)
        try:
            event = await guild.create_scheduled_event(name=f"Fat Friday @ {restaurant}"[:100], description=f"This week's winning restaurant: {restaurant}", start_time=start_local.astimezone(UTC), end_time=(start_local + timedelta(hours=2)).astimezone(UTC), entity_type=discord.EntityType.external, privacy_level=discord.PrivacyLevel.guild_only, location=os.getenv("EVENT_LOCATION", restaurant))
            return f" Event created: {event.url}"
        except (discord.Forbidden, discord.HTTPException):
            return " (I could not create the event; check Manage Events permission.)"

    async def begin_rating(self, cycle: Cycle, duration: timedelta = timedelta(hours=24)) -> None:
        async with self.sessions() as session:
            winner = await session.get(Recommendation, cycle.winner_id)
        if not winner:
            return
        deadline = now_utc() + duration
        view = RatingView(self.sessions, cycle.id, deadline)
        channel = await self.get_channel_messageable(cycle.channel_id)
        if not channel:
            return
        message = await channel.send(f"{ping()} Rate **{winner.name}** from 1–10. Ratings close {discord.utils.format_dt(deadline, 'R')}.", view=view, allowed_mentions=discord.AllowedMentions(roles=True))
        async with self.sessions.begin() as session:
            saved = await session.get(Cycle, cycle.id)
            if saved and saved.status == "selected":
                saved.status, saved.deadline, saved.message_id = "rating", deadline, message.id

    async def close_rating(self, cycle_id: int) -> None:
        async with self.sessions.begin() as session:
            cycle = await session.get(Cycle, cycle_id)
            if not cycle or cycle.status != "rating":
                return
            channel_id, message_id = cycle.channel_id, cycle.message_id
            winner = await session.get(Recommendation, cycle.winner_id)
            if not winner:
                cycle.status, cycle.deadline = "complete", None
                result = None
            else:
                average, count = (await session.execute(select(func.avg(Rating.score), func.count(Rating.id)).where(Rating.cycle_id == cycle.id))).one()
                average = float(average or 0)
                recommender = await session.get(User, winner.recommender_id)
                session.add(RestaurantResult(guild_id=cycle.guild_id, restaurant_name=winner.name, recommender_id=winner.recommender_id, average_score=average, rating_count=count, visited_at=now_utc(), manual=False))
                await session.execute(delete(Vote).where(Vote.cycle_id == cycle.id))
                await session.execute(delete(CycleOption).where(CycleOption.cycle_id == cycle.id))
                cycle.winner_id = None
                await session.delete(winner)
                cycle.status, cycle.deadline = "complete", None
                result = (winner.name, recommender.display_name if recommender else str(winner.recommender_id), average, count)
        channel = await self.get_channel_messageable(channel_id)
        if channel and message_id and hasattr(channel, "fetch_message"):
            try:
                rating_message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
                await rating_message.delete()
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
        if channel and result:
            restaurant, name, average, count = result
            await channel.send(f"Rating closed: **{restaurant}** averaged **{average:.2f}/10** from {count} rating{'s' if count != 1 else ''}. Recommended by **{name}**.")


bot = RestaurantBot()


async def current_cycle(guild_id: int, *statuses: str) -> Cycle | None:
    async with bot.sessions() as session:
        return await session.scalar(select(Cycle).where(Cycle.guild_id == guild_id, Cycle.status.in_(statuses)).order_by(Cycle.id.desc()).limit(1))


@bot.tree.command(description="Recommend a restaurant during the weekly recommendation window")
async def recommend(interaction: discord.Interaction, restaurant: app_commands.Range[str, 1, 200]) -> None:
    if not interaction.guild_id or not has_voter_role(interaction.user):
        await interaction.response.send_message("Only members with the configured voter role may recommend restaurants.", ephemeral=True)
        return
    cycle = await current_cycle(interaction.guild_id, "recommending")
    if not cycle or aware(cycle.deadline) <= now_utc():
        await interaction.response.send_message("The recommendation window is not open.", ephemeral=True)
        return
    name = " ".join(restaurant.strip().split())
    async with bot.sessions.begin() as session:
        await save_user(session, interaction.user)
        count = await session.scalar(select(func.count()).select_from(Recommendation).where(Recommendation.guild_id == interaction.guild_id, Recommendation.recommender_id == interaction.user.id))
        total = await session.scalar(select(func.count()).select_from(Recommendation).where(Recommendation.guild_id == interaction.guild_id))
        duplicate = await session.scalar(select(Recommendation.id).where(Recommendation.guild_id == interaction.guild_id, Recommendation.name_key == name.casefold()))
        if duplicate:
            response = "That restaurant is already in the recommendation pool."
        elif count >= 2:
            response = "You already have two active recommendations. Use `/remove` first."
        elif total >= 25:
            response = "The recommendation pool is full (Discord menus support at most 25 choices)."
        else:
            session.add(Recommendation(guild_id=interaction.guild_id, name=name, name_key=name.casefold(), recommender_id=interaction.user.id, created_at=now_utc()))
            response = f"Added **{name}** to the recommendation pool."
    await interaction.response.send_message(response, ephemeral=True)


@bot.tree.command(name="remove", description="Remove one of your active restaurant recommendations")
async def remove_recommendation(interaction: discord.Interaction) -> None:
    if not interaction.guild_id:
        return
    async with bot.sessions() as session:
        items = list((await session.scalars(select(Recommendation).where(Recommendation.guild_id == interaction.guild_id, Recommendation.recommender_id == interaction.user.id))).all())
    if not items:
        await interaction.response.send_message("You have no active recommendations.", ephemeral=True)
        return
    view = discord.ui.View(timeout=120)
    view.add_item(RemoveSelect(bot, interaction.user.id, items))
    await interaction.response.send_message("Which recommendation should I remove?", view=view, ephemeral=True)


@bot.tree.command(name="list", description="View top restaurant results")
@app_commands.describe(num="Number of results to show (1–25)")
async def list_results(interaction: discord.Interaction, num: app_commands.Range[int, 1, 25] = 10) -> None:
    if not interaction.guild_id:
        return
    async with bot.sessions() as session:
        rows = (await session.execute(select(RestaurantResult, User).join(User, User.id == RestaurantResult.recommender_id).where(RestaurantResult.guild_id == interaction.guild_id).order_by(RestaurantResult.average_score.desc(), RestaurantResult.visited_at.desc()).limit(num))).all()
    chunks = [rows[i:i + 10] for i in range(0, len(rows), 10)] or [[]]
    pages: list[discord.Embed] = []
    for page_number, chunk in enumerate(chunks, 1):
        embed = discord.Embed(title="Top Restaurant Results", color=discord.Color.green())
        if chunk:
            offset = (page_number - 1) * 10
            embed.description = "\n\n".join(
                f"**{offset + index}. {result.restaurant_name}**\nScore: **{result.average_score:.2f}/10** · {result.rating_count} rating{'s' if result.rating_count != 1 else ''}\nRecommended by **{user.display_name}**"
                for index, (result, user) in enumerate(chunk, 1)
            )
        else:
            embed.description = "No restaurant scores have been recorded yet."
        embed.set_footer(text=f"Page {page_number}/{len(chunks)} · Showing {len(rows)} result{'s' if len(rows) != 1 else ''}")
        pages.append(embed)
    if len(pages) > 1:
        await interaction.response.send_message(embed=pages[0], view=EmbedPages(pages, interaction.user.id))
    else:
        await interaction.response.send_message(embed=pages[0])


class EmbedPages(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], owner: int) -> None:
        super().__init__(timeout=180)
        self.pages, self.owner, self.index = pages, owner, 0

    @discord.ui.button(label="Previous", emoji="⬅️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.owner:
            return await interaction.response.send_message("This paginator belongs to another user.", ephemeral=True)
        self.index = (self.index - 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Next", emoji="➡️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.owner:
            return await interaction.response.send_message("This paginator belongs to another user.", ephemeral=True)
        self.index = (self.index + 1) % len(self.pages)
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)


@bot.tree.command(name="viewballot", description="View a snapshot of the current restaurant vote results")
async def view_ballot(interaction: discord.Interaction) -> None:
    if not interaction.guild_id:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return
    cycle = await current_cycle(interaction.guild_id, "voting")
    if not cycle:
        await interaction.response.send_message("There is no active restaurant vote.", ephemeral=True)
        return
    async with bot.sessions() as session:
        rows = (await session.execute(
            select(Recommendation.name, func.count(Vote.id))
            .join(CycleOption, CycleOption.recommendation_id == Recommendation.id)
            .outerjoin(Vote, (Vote.recommendation_id == Recommendation.id) & (Vote.cycle_id == cycle.id))
            .where(CycleOption.cycle_id == cycle.id)
            .group_by(Recommendation.id)
            .order_by(func.count(Vote.id).desc(), Recommendation.name)
        )).all()
    chunks = [rows[i:i + 10] for i in range(0, len(rows), 10)] or [[]]
    pages: list[discord.Embed] = []
    deadline = aware(cycle.deadline)
    for page_number, chunk in enumerate(chunks, 1):
        title = f"Current Runoff Ballot · Round {cycle.runoff_round}" if cycle.runoff_round else "Current Restaurant Ballot"
        embed = discord.Embed(title=title, color=discord.Color.blurple())
        if chunk:
            offset = (page_number - 1) * 10
            embed.description = "\n\n".join(
                f"**{offset + index}. {name}** — {count} vote{'s' if count != 1 else ''}"
                for index, (name, count) in enumerate(chunk, 1)
            )
        else:
            embed.description = "No restaurants are available in the active ballot."
        if deadline:
            embed.add_field(name="Voting closes", value=discord.utils.format_dt(deadline, "R"), inline=False)
        embed.set_footer(text=f"Snapshot · Page {page_number}/{len(chunks)} · Run /viewballot again to refresh")
        pages.append(embed)
    if len(pages) > 1:
        await interaction.response.send_message(embed=pages[0], view=EmbedPages(pages, interaction.user.id))
    else:
        await interaction.response.send_message(embed=pages[0])


@bot.tree.command(name="votingrecord", description="Privately view who selected each restaurant in the active vote")
@app_commands.default_permissions(administrator=True)
async def voting_record(interaction: discord.Interaction) -> None:
    if not interaction.guild_id or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("This command must be used by an administrator in a server.", ephemeral=True)
        return
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("Only administrators can view voting records.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    cycle = await current_cycle(interaction.guild_id, "voting")
    if not cycle:
        await interaction.followup.send("There is no active restaurant vote.", ephemeral=True)
        return
    async with bot.sessions() as session:
        rows = (await session.execute(
            select(Recommendation.name, Vote.user_id)
            .join(CycleOption, CycleOption.recommendation_id == Recommendation.id)
            .outerjoin(Vote, (Vote.recommendation_id == Recommendation.id) & (Vote.cycle_id == cycle.id))
            .where(CycleOption.cycle_id == cycle.id)
            .order_by(Recommendation.name, Vote.user_id)
        )).all()
    records: dict[str, list[int]] = {}
    for restaurant_name, voter_id in rows:
        records.setdefault(restaurant_name, [])
        if voter_id is not None:
            records[restaurant_name].append(voter_id)

    fields: list[tuple[str, str]] = []
    for restaurant_name, voter_ids in records.items():
        mentions = [f"<@{voter_id}>" for voter_id in voter_ids]
        if not mentions:
            fields.append((restaurant_name[:256], "No votes"))
            continue
        chunks: list[str] = []
        current = ""
        for mention in mentions:
            candidate = f"{current}, {mention}" if current else mention
            if len(candidate) > 900:
                chunks.append(current)
                current = mention
            else:
                current = candidate
        if current:
            chunks.append(current)
        for index, voter_chunk in enumerate(chunks):
            suffix = "" if index == 0 else " (continued)"
            fields.append((f"{restaurant_name[:244]}{suffix}", voter_chunk))

    field_pages = [fields[i:i + 5] for i in range(0, len(fields), 5)] or [[]]
    pages: list[discord.Embed] = []
    for page_number, page_fields in enumerate(field_pages, 1):
        title = f"Private Runoff Voting Record · Round {cycle.runoff_round}" if cycle.runoff_round else "Private Voting Record"
        embed = discord.Embed(title=title, color=discord.Color.dark_gold())
        embed.description = "Only you can see this administrator audit snapshot."
        for field_name, field_value in page_fields:
            embed.add_field(name=field_name, value=field_value, inline=False)
        if not page_fields:
            embed.add_field(name="Ballot", value="No voting options are available.", inline=False)
        embed.set_footer(text=f"Page {page_number}/{len(field_pages)} · Run /votingrecord again to refresh")
        pages.append(embed)
    if len(pages) > 1:
        await interaction.followup.send(embed=pages[0], view=EmbedPages(pages, interaction.user.id), ephemeral=True)
    else:
        await interaction.followup.send(embed=pages[0], ephemeral=True)


@bot.tree.command(name="recommendations", description="View all restaurants currently in the voting pool")
async def active_recommendations(interaction: discord.Interaction) -> None:
    if not interaction.guild_id:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return
    async with bot.sessions() as session:
        rows = (await session.execute(
            select(Recommendation, User)
            .join(User, User.id == Recommendation.recommender_id)
            .where(Recommendation.guild_id == interaction.guild_id)
            .order_by(Recommendation.name)
        )).all()
    chunks = [rows[i:i + 10] for i in range(0, len(rows), 10)] or [[]]
    pages: list[discord.Embed] = []
    for page_number, chunk in enumerate(chunks, 1):
        embed = discord.Embed(title="Active Restaurant Recommendations", color=discord.Color.blurple())
        if chunk:
            offset = (page_number - 1) * 10
            embed.description = "\n\n".join(
                f"**{offset + index}. {recommendation.name}**\nRecommended by **{user.display_name}**"
                for index, (recommendation, user) in enumerate(chunk, 1)
            )
        else:
            embed.description = "There are no active restaurant recommendations."
        embed.set_footer(text=f"Page {page_number}/{len(chunks)} · {len(rows)} active recommendation{'s' if len(rows) != 1 else ''}")
        pages.append(embed)
    if len(pages) > 1:
        await interaction.response.send_message(embed=pages[0], view=EmbedPages(pages, interaction.user.id))
    else:
        await interaction.response.send_message(embed=pages[0])


@bot.tree.command(description="View average restaurant scores grouped by recommender")
async def userscores(interaction: discord.Interaction) -> None:
    if not interaction.guild_id:
        return
    async with bot.sessions() as session:
        rows = (await session.execute(select(User.display_name, func.avg(RestaurantResult.average_score), func.count(RestaurantResult.id)).join(RestaurantResult, RestaurantResult.recommender_id == User.id).where(RestaurantResult.guild_id == interaction.guild_id).group_by(User.id).order_by(func.avg(RestaurantResult.average_score).desc()))).all()
    pages: list[discord.Embed] = []
    chunks = [rows[i:i + 10] for i in range(0, len(rows), 10)] or [[]]
    for page_number, chunk in enumerate(chunks, 1):
        embed = discord.Embed(title="Recommender Leaderboard", color=discord.Color.gold())
        if chunk:
            start = (page_number - 1) * 10
            embed.description = "\n\n".join(
                f"**{start + index}. {name}**\nAverage: **{average:.2f}/10** · {count} scored restaurant{'s' if count != 1 else ''}"
                for index, (name, average, count) in enumerate(chunk, 1)
            )
        else:
            embed.description = "No user scores have been recorded yet."
        embed.set_footer(text=f"Page {page_number}/{len(chunks)} · Ranked by average recommendation score")
        pages.append(embed)
    if len(pages) > 1:
        await interaction.response.send_message(embed=pages[0], view=EmbedPages(pages, interaction.user.id))
    else:
        await interaction.response.send_message(embed=pages[0])


@bot.tree.command(name="user", description="View a user's recommendation and score information")
async def user_info(interaction: discord.Interaction, user: discord.Member) -> None:
    if not interaction.guild_id:
        return
    async with bot.sessions() as session:
        active = list((await session.scalars(select(Recommendation.name).where(Recommendation.guild_id == interaction.guild_id, Recommendation.recommender_id == user.id))).all())
        results = list((await session.scalars(select(RestaurantResult).where(RestaurantResult.guild_id == interaction.guild_id, RestaurantResult.recommender_id == user.id).order_by(RestaurantResult.average_score.desc(), RestaurantResult.visited_at.desc()))).all())
    average = sum(item.average_score for item in results) / len(results) if results else None
    chunks = [results[i:i + 10] for i in range(0, len(results), 10)] or [[]]
    pages: list[discord.Embed] = []
    for page_number, chunk in enumerate(chunks, 1):
        embed = discord.Embed(title=f"{user.display_name}'s Restaurant Profile", color=user.color)
        embed.set_thumbnail(url=user.display_avatar.url)
        if page_number == 1:
            active_text = "\n".join(f"• {name}" for name in active) if active else "None"
            embed.add_field(name=f"Active Recommendations ({len(active)})", value=active_text[:1024], inline=False)
            average_text = f"**{average:.2f}/10** across {len(results)} scored restaurant{'s' if len(results) != 1 else ''}" if average is not None else "No scored recommendations yet"
            embed.add_field(name="Average Recommendation Rating", value=average_text, inline=False)
        if chunk:
            offset = (page_number - 1) * 10
            history = "\n".join(
                f"**{offset + index}. {item.restaurant_name}** — {item.average_score:.2f}/10 ({item.rating_count} rating{'s' if item.rating_count != 1 else ''})"
                for index, item in enumerate(chunk, 1)
            )
        else:
            history = "No recommended restaurants have been scored yet."
        embed.add_field(name="Recommended Restaurants & Scores", value=history[:1024], inline=False)
        embed.set_footer(text=f"Page {page_number}/{len(chunks)}")
        pages.append(embed)
    if len(pages) > 1:
        await interaction.response.send_message(embed=pages[0], view=EmbedPages(pages, interaction.user.id))
    else:
        await interaction.response.send_message(embed=pages[0])


@bot.tree.command(description="Skip the active weekly process")
@app_commands.default_permissions(administrator=True)
async def skip(interaction: discord.Interaction) -> None:
    if not interaction.guild_id:
        return
    cycle = await current_cycle(interaction.guild_id, "recommending", "voting", "selected", "rating")
    if not cycle:
        await interaction.response.send_message("There is no active weekly process.", ephemeral=True)
        return
    async with bot.sessions.begin() as session:
        saved = await session.get(Cycle, cycle.id)
        saved.status, saved.deadline = "skipped", None
    await interaction.response.send_message("This week's process has been skipped.")


@bot.tree.command(name="end-process", description="Immediately end the current recommendation, voting, or rating phase")
@app_commands.default_permissions(administrator=True)
async def end_process(interaction: discord.Interaction) -> None:
    if not interaction.guild_id:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return
    cycle = await current_cycle(interaction.guild_id, "recommending", "voting", "rating", "selected")
    if not cycle:
        await interaction.response.send_message("There is no active process to end.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    if cycle.status == "recommending":
        await bot.open_vote(cycle.id)
        result = "The recommendation phase ended early. Voting has now opened."
    elif cycle.status == "voting":
        await bot.close_vote(cycle.id)
        result = "Voting ended early and the winner was processed."
    elif cycle.status == "rating":
        await bot.close_rating(cycle.id)
        result = "Rating ended early and the result was processed."
    else:
        async with bot.sessions.begin() as session:
            saved = await session.get(Cycle, cycle.id)
            if saved:
                saved.status, saved.deadline = "complete", None
        result = "The selected phase was ended. No rating was recorded."
    await interaction.followup.send(result, ephemeral=True)


@bot.tree.command(name="admin-remove", description="Remove a restaurant credited to a specified user")
@app_commands.describe(user="User whose recommendation should be removed", restaurant="Exact restaurant name to remove")
@app_commands.default_permissions(administrator=True)
async def admin_remove(interaction: discord.Interaction, user: discord.Member, restaurant: app_commands.Range[str, 1, 200]) -> None:
    if not interaction.guild_id:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return
    name_key = " ".join(restaurant.strip().split()).casefold()
    await interaction.response.defer(ephemeral=True)
    async with bot.sessions.begin() as session:
        item = await session.scalar(select(Recommendation).where(
            Recommendation.guild_id == interaction.guild_id,
            Recommendation.recommender_id == user.id,
            Recommendation.name_key == name_key,
        ))
        if not item:
            removed_name = None
            active_cycle_ids: list[int] = []
            blocked = False
        else:
            blocked = bool(await session.scalar(select(Cycle.id).where(
                Cycle.winner_id == item.id,
                Cycle.status.in_(["selected", "rating"]),
            ).limit(1)))
            if blocked:
                removed_name = None
                active_cycle_ids = []
            else:
                active_cycle_ids = list((await session.scalars(
                    select(Cycle.id)
                    .join(CycleOption, CycleOption.cycle_id == Cycle.id)
                    .where(CycleOption.recommendation_id == item.id, Cycle.status == "voting")
                    .distinct()
                )).all())
                removed_name = item.name
                await session.execute(delete(Vote).where(Vote.recommendation_id == item.id))
                await session.execute(delete(CycleOption).where(CycleOption.recommendation_id == item.id))
                await session.delete(item)
    if blocked:
        await interaction.followup.send("That restaurant has already won and cannot be removed while selection or rating is active. End the process first.", ephemeral=True)
        return
    if removed_name is None:
        await interaction.followup.send(f"**{user.display_name}** does not have that active recommendation.", ephemeral=True)
        return
    for cycle_id in active_cycle_ids:
        await bot.refresh_vote_message(cycle_id)
    await interaction.followup.send(f"Removed **{removed_name}** from **{user.display_name}**'s active recommendations.", ephemeral=True)


@bot.tree.command(name="force-clear", description="Permanently clear all bot data (administrator only)")
@app_commands.describe(confirm="Type CLEAR exactly to confirm the permanent reset")
@app_commands.default_permissions(administrator=True)
async def force_clear(interaction: discord.Interaction, confirm: str) -> None:
    if confirm != "CLEAR":
        await interaction.response.send_message("Nothing was deleted. Run the command again with `confirm: CLEAR`.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    async with bot.sessions() as session:
        active_messages = (await session.execute(select(Cycle.channel_id, Cycle.message_id).where(Cycle.message_id.is_not(None), Cycle.status.in_(["voting", "rating"])))).all()
    for channel_id, message_id in active_messages:
        channel = await bot.get_channel_messageable(channel_id)
        if channel and hasattr(channel, "fetch_message"):
            try:
                message = await channel.fetch_message(message_id)  # type: ignore[attr-defined]
                await message.edit(view=None)
            except (discord.HTTPException, discord.Forbidden, discord.NotFound):
                pass
    async with bot.sessions.begin() as session:
        for model in (Rating, Vote, CycleOption, RestaurantResult, Cycle, Recommendation, User):
            await session.execute(delete(model))
    await interaction.followup.send("The bot database has been permanently cleared. Existing vote/rating components were disabled.", ephemeral=True)


@bot.tree.command(name="force-open", description="Force open a workflow period for testing (administrator only)")
@app_commands.describe(phase="Period to open", minutes="How long the test period remains open", restaurant="Optional restaurant to rate; defaults to the oldest recommendation")
@app_commands.choices(phase=[
    app_commands.Choice(name="Recommendations", value="recommendations"),
    app_commands.Choice(name="Voting", value="voting"),
    app_commands.Choice(name="Rating", value="rating"),
])
@app_commands.default_permissions(administrator=True)
async def force_open(
    interaction: discord.Interaction,
    phase: app_commands.Choice[str],
    minutes: app_commands.Range[int, 1, 10080] = 5,
    restaurant: str | None = None,
) -> None:
    if not interaction.guild_id or not interaction.channel_id:
        await interaction.response.send_message("This command must be used in a server channel.", ephemeral=True)
        return
    duration = timedelta(minutes=minutes)
    await interaction.response.defer(ephemeral=True)

    active = await current_cycle(interaction.guild_id, "recommending", "voting", "selected", "rating")
    if active:
        async with bot.sessions.begin() as session:
            saved = await session.get(Cycle, active.id)
            if saved:
                saved.status, saved.deadline = "skipped", None

    if phase.value == "recommendations":
        await bot.start_cycle(interaction.guild_id, interaction.channel_id, duration)
        result = f"Forced the recommendation period open for {minutes} minute{'s' if minutes != 1 else ''}."
    elif phase.value == "voting":
        async with bot.sessions() as session:
            count = await session.scalar(select(func.count()).select_from(Recommendation).where(Recommendation.guild_id == interaction.guild_id))
        if not count:
            await interaction.followup.send("Add at least one recommendation before forcing voting open.", ephemeral=True)
            return
        async with bot.sessions.begin() as session:
            cycle = Cycle(guild_id=interaction.guild_id, channel_id=interaction.channel_id, status="recommending", opened_at=now_utc(), deadline=now_utc())
            session.add(cycle)
            await session.flush()
            cycle_id = cycle.id
        await bot.open_vote(cycle_id, duration)
        result = f"Forced voting open for {minutes} minute{'s' if minutes != 1 else ''}."
    else:
        async with bot.sessions() as session:
            query = select(Recommendation).where(Recommendation.guild_id == interaction.guild_id)
            if restaurant:
                query = query.where(Recommendation.name_key == " ".join(restaurant.strip().split()).casefold())
            winner = await session.scalar(query.order_by(Recommendation.created_at).limit(1))
        if not winner:
            await interaction.followup.send("That restaurant is not in the recommendation pool." if restaurant else "Add at least one recommendation before forcing rating open.", ephemeral=True)
            return
        async with bot.sessions.begin() as session:
            cycle = Cycle(guild_id=interaction.guild_id, channel_id=interaction.channel_id, status="selected", opened_at=now_utc(), deadline=None, winner_id=winner.id)
            session.add(cycle)
            await session.flush()
            cycle_id = cycle.id
        async with bot.sessions() as session:
            saved_cycle = await session.get(Cycle, cycle_id)
            await bot.begin_rating(saved_cycle, duration)
        result = f"Forced rating for **{winner.name}** open for {minutes} minute{'s' if minutes != 1 else ''}."
    await interaction.followup.send(result, ephemeral=True)


@bot.tree.command(description="Manually add a scored restaurant result")
@app_commands.default_permissions(administrator=True)
async def add(interaction: discord.Interaction, user: discord.Member, restaurant: app_commands.Range[str, 1, 200], score: app_commands.Range[float, 1, 10]) -> None:
    if not interaction.guild_id:
        return
    async with bot.sessions.begin() as session:
        await save_user(session, user)
        session.add(RestaurantResult(guild_id=interaction.guild_id, restaurant_name=" ".join(restaurant.strip().split()), recommender_id=user.id, average_score=float(score), rating_count=1, visited_at=now_utc(), manual=True))
    await interaction.response.send_message(f"Added **{restaurant}** at **{score:.2f}/10**, recommended by {user.mention}.")


@bot.tree.command(name="forceadd", description="Add a recommendation without user or pool limits")
@app_commands.describe(restaurant="Restaurant to add to the voting pool", user="User credited with the recommendation")
@app_commands.default_permissions(administrator=True)
async def forceadd(interaction: discord.Interaction, restaurant: app_commands.Range[str, 1, 200], user: discord.Member) -> None:
    if not interaction.guild_id:
        await interaction.response.send_message("This command must be used in a server.", ephemeral=True)
        return
    name = " ".join(restaurant.strip().split())
    async with bot.sessions.begin() as session:
        duplicate = await session.scalar(select(Recommendation.id).where(Recommendation.guild_id == interaction.guild_id, Recommendation.name_key == name.casefold()))
        if duplicate:
            response = "That restaurant is already in the voting pool."
        else:
            await save_user(session, user)
            session.add(Recommendation(guild_id=interaction.guild_id, name=name, name_key=name.casefold(), recommender_id=user.id, created_at=now_utc()))
            response = f"Force-added **{name}** to the voting pool, credited to {user.mention}."
    await interaction.response.send_message(response, ephemeral=True)


@bot.tree.command(description="Open the 24-hour rating for this week's winning restaurant")
@app_commands.default_permissions(administrator=True)
async def rate(interaction: discord.Interaction) -> None:
    if not interaction.guild_id:
        return
    if datetime.now(bot.tz).weekday() < 4:
        await interaction.response.send_message("`/rate` is available starting Friday.", ephemeral=True)
        return
    cycle = await current_cycle(interaction.guild_id, "selected")
    if not cycle:
        await interaction.response.send_message("There is no selected restaurant waiting to be rated.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    await bot.begin_rating(cycle)
    await interaction.followup.send("The 24-hour rating is now open.", ephemeral=True)


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    bot.run(token)


if __name__ == "__main__":
    main()
