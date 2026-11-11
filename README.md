# Restaurant Review Discord Bot

A Python Discord bot that posts restaurant review requests as rich embeds. The nominated reviewer chooses a **1–10 rating** from a dropdown and is then shown a text box for an optional note. Requests remain interactive for 24 hours by default, or for the duration supplied to the command. PostgreSQL stores users, restaurants, review invitations, ratings, and notes through SQLAlchemy.

## Requirements

- Python 3.11+
- PostgreSQL 14+
- A Discord application with a bot user

## Discord setup

1. Create an application in the [Discord Developer Portal](https://discord.com/developers/applications), open **Bot**, and create a bot token.
2. Under **OAuth2 → URL Generator**, select the `bot` and `applications.commands` scopes. Give the bot **View Channels**, **Send Messages**, **Embed Links**, and **Read Message History** permissions, then use the generated URL to invite it.
3. Never commit or share the bot token.

## Run locally

Create the database (adjust the owner/password as desired):

```bash
createdb restaurant_reviews
```

Create a virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```

Copy the sample configuration and edit it:

```bash
cp .env.example .env
```

```dotenv
DISCORD_TOKEN=your-token
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/restaurant_reviews
DISCORD_GUILD_ID=your-test-server-id
```

`DISCORD_GUILD_ID` is optional but recommended during development because commands registered to a test server appear immediately. If omitted, the command is registered globally and Discord may take time to display it. Database tables are created automatically at startup.

Start the bot:

```bash
python -m restaurant_bot.bot
```

## Usage

In Discord, enter:

```text
/request-review user:@Alex restaurant:Brickolini's hours:24
```

- `user` and `restaurant` are required.
- `hours` is optional, defaults to `24`, and accepts 1–168.
- Only the nominated user can submit that request. Selecting a rating opens the note dialog.
- Submitting again before expiry updates that user's existing review rather than creating a duplicate.
- On restart, the bot reconnects still-active controls whose message is still accessible.

## Database schema

SQLAlchemy creates four tables: `users` (Discord identity and the user's review relationship), `restaurants`, `invitations` (message IDs and expiry), and `reviews` (reviewer, restaurant, rating, note, and timestamp). For production schema changes, add a migration tool such as Alembic instead of relying only on `create_all`.

## Troubleshooting

- **Command not visible:** set `DISCORD_GUILD_ID`, restart, and confirm the bot was invited with `applications.commands`.
- **Database connection errors:** confirm PostgreSQL is running, the database exists, and `DATABASE_URL` uses the `postgresql+asyncpg://` prefix.
- **Old request does not restore:** the bot needs View Channel and Read Message History access to the original channel.
