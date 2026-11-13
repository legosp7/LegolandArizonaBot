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
- `EVENT_DAY`, `EVENT_HOUR`, `EVENT_MINUTE`, and `EVENT_LOCATION`: scheduled-event defaults. Monday is day `0`; Friday is day `4`. For example, `EVENT_HOUR=18` and `EVENT_MINUTE=30` means 6:30 PM.

`DB_PATH` defaults to `bot.db`. If `DB_PATH` is set, it takes priority over `DATABASE_URL`. PostgreSQL remains possible through `DATABASE_URL` when `DB_PATH` is unset, but its matching async driver must be installed separately.

## Docker on Raspberry Pi

The included Compose configuration stores SQLite outside the container at `./data/bot.db`. Inside the container, the same file is available as `/app/data/bot.db` through `DB_PATH`.

On the Pi:

```bash
cd /mnt/server-data/docker-apps/discord-bots/YOUR-REPO
findmnt /mnt/server-data
mkdir -p data
cp .env.example .env
# Edit .env with the Discord IDs and token, then start the bot.
docker compose up -d --build
```

The resulting storage path is:

```text
/mnt/server-data/docker-apps/discord-bots/YOUR-REPO/data/bot.db
```

Useful commands:

```bash
docker compose logs -f restaurant-bot
docker compose restart restaurant-bot
docker compose down
```

`docker compose down` does not delete `data/bot.db`. Avoid `docker compose down -v` if the volume configuration is changed to a named volume later.

## Weekly flow

1. On Monday, a 24-hour recommendation window opens and pings the configured role.
2. Eligible users use `/recommend restaurant`; each may keep up to two active entries. `/remove` opens a private selection menu.
3. The bot opens a 36-hour multi-select vote containing the persistent recommendation pool. Its attached embed shows a live per-restaurant vote tally and refreshes after every ballot (25 restaurants per page).
4. The winner is announced, an external Discord event is created, and the winning recommendation waits for Friday. A tie opens a fresh one-hour runoff containing only the tied leaders. If a runoff remains tied between two restaurants, the bot chooses the winner by coin flip; larger ties continue to another one-hour runoff.
5. An administrator runs `/rate` on or after Friday. The role receives a 1–10 selector for 24 hours.
6. The bot averages and announces the score and nominator, stores the result, and removes only the winning restaurant. All other recommendations carry into the next week.

All deadlines and responses are stored in the database and active voting/rating controls are restored after a restart.

## Commands

- `/recommend restaurant` — add a recommendation during the open window (voter role).
- `/remove` — select one of your recommendations to remove.
- `/recommendations` — view every active restaurant in the voting pool and its recommender, 10 per page.
- `/viewballot` — view a paginated snapshot of the currently active vote tally; run it again to refresh.
- `/votingrecord` — privately show an administrator who voted for every restaurant in the active ballot; the response is ephemeral and visible only to the administrator who invoked it.
- `/list [num]` — show 1–25 top scored visits (default 10).
- `/userscores` — rank all nominators by average result, 10 per page.
- `/user user` — show a member’s active recommendations and score history.
- `/rate` — start the post-Friday rating window (administrator).
- `/skip` — stop the current weekly cycle (administrator).
- `/end-process` — immediately end the current phase: recommendations advance to voting, voting selects its winner, and rating calculates its result (administrator).
- `/admin-remove user restaurant` — remove a restaurant from a specified user's active list; an active voting message and tally are refreshed automatically (administrator).
- `/add user restaurant score` — store a manual historical score (administrator).
- `/forceadd restaurant user` — add a restaurant to the voting pool for a user without applying the per-user or pool-size limits (administrator).
- `/force-open phase [minutes] [restaurant]` — immediately open recommendations, voting, or rating for debugging. Test periods default to five minutes; forced rating uses the oldest queued restaurant unless one is named (administrator).
- `/force-clear confirm:CLEAR` — permanently delete all bot data and disable active vote/rating components (administrator).

Normal recommendations stop at 25 active restaurants, but administrator force-added entries are unlimited. Voting automatically paginates them into groups of 25 and saves choices independently on each page. Ratings are upserts, and voting again on a page replaces that user's choices on that page.
