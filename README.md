# Weekly Restaurant Discord Bot

A persistent Discord bot for weekly restaurant recommendations, approval voting, events, ratings, and leaderboards.

## Install

Python 3.11–3.13 is recommended. The default database is SQLite, using `aiosqlite`; it does not compile `asyncpg` and therefore avoids the “Failed to build installable wheels … asyncpg” error.

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`, then run:

```powershell
.venv\Scripts\python -m restaurant_bot.bot
```

The bot needs the `bot` and `applications.commands` OAuth scopes plus View Channels, Send Messages, Embed Links, Read Message History, and Manage Events permissions. Set `DISCORD_GUILD_ID` during development for immediate slash-command updates.

## Required configuration

- `DISCORD_TOKEN`: bot token.
- `VOTER_ROLE_ID`: the role allowed to recommend, vote, and rate. It is deliberately blank in the example.
- `ANNOUNCEMENT_CHANNEL_ID`: channel for the first automatic Monday post. Later weeks can reuse the most recently stored channel.
- `BOT_TIMEZONE` and `MONDAY_HOUR`: local weekly opening time.
- `EVENT_DAY`, `EVENT_HOUR`, and `EVENT_LOCATION`: scheduled-event defaults. Monday is day `0`; Friday is day `4`.

`DATABASE_URL` defaults to `sqlite+aiosqlite:///restaurant_bot.db`. PostgreSQL is still possible, but install a database driver that publishes a wheel for your exact Python version and use its matching SQLAlchemy URL.

## Weekly flow

1. On Monday, a 24-hour recommendation window opens and pings the configured role.
2. Eligible users use `/recommend restaurant`; each may keep up to two active entries. `/remove` opens a private selection menu.
3. The bot opens a 36-hour multi-select vote containing the persistent recommendation pool (Discord component limit: 25).
4. The winner is announced, an external Discord event is created, and the winning recommendation waits for Friday.
5. An administrator runs `/rate` on or after Friday. The role receives a 1–10 selector for 24 hours.
6. The bot averages and announces the score and nominator, stores the result, and removes only the winning restaurant. All other recommendations carry into the next week.

All deadlines and responses are stored in the database and active voting/rating controls are restored after a restart.

## Commands

- `/recommend restaurant` — add a recommendation during the open window (voter role).
- `/remove` — select one of your recommendations to remove.
- `/list [num]` — show 1–25 top scored visits (default 10).
- `/userscores` — rank all nominators by average result, 10 per page.
- `/user user` — show a member’s active recommendations and score history.
- `/rate` — start the post-Friday rating window (administrator).
- `/skip` — stop the current weekly cycle (administrator).
- `/add user restaurant score` — store a manual historical score (administrator).
- `/forceadd restaurant user` — add a restaurant to the voting pool for a user without applying the per-user or pool-size limits (administrator).
- `/force-open phase [minutes] [restaurant]` — immediately open recommendations, voting, or rating for debugging. Test periods default to five minutes; forced rating uses the oldest queued restaurant unless one is named (administrator).
- `/force-clear confirm:CLEAR` — permanently delete all bot data and disable active vote/rating components (administrator).

Normal recommendations stop at 25 active restaurants, but administrator force-added entries are unlimited. Voting automatically paginates them into groups of 25 and saves choices independently on each page. Ratings are upserts, and voting again on a page replaces that user's choices on that page.
