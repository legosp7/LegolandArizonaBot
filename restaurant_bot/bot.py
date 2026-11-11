from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .database import Invitation, Restaurant, Review, User, create_session_factory, create_tables


class NoteModal(discord.ui.Modal, title="Add your restaurant review"):
    note = discord.ui.TextInput(
        label="Note (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="What did you like or dislike?",
        required=False,
        max_length=1000,
    )

    def __init__(self, view: ReviewView, rating: int) -> None:
        super().__init__()
        self.review_view = view
        self.rating = rating

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await self.review_view.save_review(interaction.user, self.rating, str(self.note.value).strip() or None)
        except Exception:
            await interaction.followup.send("I could not save that review. Please try again.", ephemeral=True)
            raise
        await interaction.followup.send(
            f"Saved your **{self.rating}/10** review for **{self.review_view.restaurant_name}**.",
            ephemeral=True,
        )


class RatingSelect(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Choose a rating from 1 to 10…",
            options=[discord.SelectOption(label=f"{rating}/10", value=str(rating)) for rating in range(1, 11)],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ReviewView):
            return
        if interaction.user.id != view.reviewer_id:
            await interaction.response.send_message("This review request is for another user.", ephemeral=True)
            return
        if datetime.now(timezone.utc) >= view.expires_at:
            await interaction.response.send_message("This review request has expired.", ephemeral=True)
            return
        await interaction.response.send_modal(NoteModal(view, int(self.values[0])))


class ReviewView(discord.ui.View):
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        invitation_id: int,
        reviewer_id: int,
        restaurant_name: str,
        expires_at: datetime,
    ) -> None:
        remaining = max(1.0, (expires_at - datetime.now(timezone.utc)).total_seconds())
        super().__init__(timeout=remaining)
        self.sessions = sessions
        self.invitation_id = invitation_id
        self.reviewer_id = reviewer_id
        self.restaurant_name = restaurant_name
        self.expires_at = expires_at
        self.message: discord.Message | None = None
        self.add_item(RatingSelect())

    async def save_review(self, member: discord.abc.User, rating: int, note: str | None) -> None:
        now = datetime.now(timezone.utc)
        async with self.sessions.begin() as session:
            await session.execute(
                insert(User).values(id=member.id, display_name=member.display_name).on_conflict_do_update(
                    index_elements=[User.id], set_={"display_name": member.display_name}
                )
            )
            restaurant_id = await session.scalar(select(Restaurant.id).where(Restaurant.name == self.restaurant_name))
            if restaurant_id is None:
                result = await session.execute(
                    insert(Restaurant).values(name=self.restaurant_name).on_conflict_do_nothing().returning(Restaurant.id)
                )
                restaurant_id = result.scalar_one_or_none()
                if restaurant_id is None:
                    restaurant_id = await session.scalar(select(Restaurant.id).where(Restaurant.name == self.restaurant_name))
            statement = insert(Review).values(
                invitation_id=self.invitation_id,
                restaurant_id=restaurant_id,
                reviewer_id=member.id,
                rating=rating,
                note=note,
                created_at=now,
            )
            await session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_review_invitation_reviewer",
                    set_={"rating": rating, "note": note, "created_at": now},
                )
            )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class RestaurantBot(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.engine, self.sessions = create_session_factory()

    async def setup_hook(self) -> None:
        await create_tables(self.engine)
        guild_id = os.getenv("DISCORD_GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        await self.restore_views()

    async def close(self) -> None:
        await super().close()
        await self.engine.dispose()

    async def restore_views(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.sessions() as session:
            invitations = (await session.scalars(select(Invitation).where(Invitation.expires_at > now))).all()
        for invite in invitations:
            if not invite.channel_id or not invite.message_id:
                continue
            view = ReviewView(self.sessions, invite.id, invite.reviewer_id, invite.restaurant_name, invite.expires_at)
            try:
                channel = await self.fetch_channel(invite.channel_id)
                if isinstance(channel, discord.abc.Messageable):
                    view.message = await channel.fetch_message(invite.message_id)  # type: ignore[attr-defined]
                    await view.message.edit(view=view)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                continue


load_dotenv()
bot = RestaurantBot()


@bot.tree.command(name="request-review", description="Ask a user to review a restaurant")
@app_commands.describe(user="The person who should review", restaurant="Restaurant name", hours="Hours before this request expires")
async def request_review(
    interaction: discord.Interaction,
    user: discord.Member,
    restaurant: app_commands.Range[str, 1, 200],
    hours: app_commands.Range[int, 1, 168] = 24,
) -> None:
    restaurant_name = restaurant.strip()
    if not restaurant_name:
        await interaction.response.send_message("Restaurant name cannot be blank.", ephemeral=True)
        return
    await interaction.response.defer()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=hours)
    async with bot.sessions.begin() as session:
        invitation = Invitation(
            restaurant_name=restaurant_name,
            reviewer_id=user.id,
            creator_id=interaction.user.id,
            expires_at=expires_at,
            created_at=now,
        )
        session.add(invitation)
        await session.flush()
        invitation_id = invitation.id
    view = ReviewView(bot.sessions, invitation_id, user.id, restaurant_name, expires_at)
    embed = discord.Embed(title=f"Review: {restaurant_name}", color=discord.Color.green())
    embed.description = f"{user.mention}, choose a rating below, then add an optional note."
    embed.add_field(name="Rating", value="1 (lowest) — 10 (highest)")
    embed.set_footer(text=f"Open for {hours} hour{'s' if hours != 1 else ''}")
    message = await interaction.followup.send(content=user.mention, embed=embed, view=view, wait=True)
    view.message = message
    async with bot.sessions.begin() as session:
        saved = await session.get(Invitation, invitation_id)
        if saved:
            saved.channel_id = interaction.channel_id
            saved.message_id = message.id


def main() -> None:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    bot.run(token)


if __name__ == "__main__":
    main()
