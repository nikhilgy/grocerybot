# GroceryBot

A personal Telegram bot that looks at photos of your fridge/pantry, figures out what's
missing for tomorrow's meals against a diet plan, and orders the missing items from
Swiggy Instamart via MCP. Also supports voice notes, quick text-based cart additions,
order history, weekly spend summaries, persistent multi-zone kitchen inventory, recipe
mode (typed, spoken, or imported from a YouTube video), and guest-count scaling.

Single-user, no auth. Cart state and multi-turn conversation state live in memory
(ephemeral by design); kitchen inventory is persisted in a local SQLite database;
the diet plan is a JSON file you edit by hand.

## Architecture

```
Telegram → FastAPI /webhook → handlers.py → orchestrator.py
                                                 ├─ vision/analyzer.py      (Claude Vision: photo → per-zone inventory + stock level)
                                                 ├─ planner/gap_analyzer.py (Claude: inventory + diet plan/recipe → shopping list)
                                                 ├─ nlu/intent_classifier.py (Claude: text → quick_add/recipe/guest/general)
                                                 ├─ recipes/recipe_generator.py (Claude: recipe name + servings → ingredients)
                                                 ├─ recipes/youtube.py      (transcript extraction + Claude recipe parsing)
                                                 ├─ db/queries.py           (SQLite: zones + inventory, persisted across scans)
                                                 ├─ instamart/product_mapper.py (Claude + Swiggy search → best SKU match)
                                                 ├─ instamart/cart_manager.py   (build/update/checkout cart)
                                                 └─ instamart/mcp_client.py     (Swiggy Instamart MCP tools)
```

## 1. Create the Telegram bot

1. Open a chat with [@BotFather](https://t.me/BotFather) on Telegram.
2. Send `/newbot` and follow the prompts (choose a name and a unique username).
3. BotFather returns a token like `123456789:AAF...` — this is your `BOT_TOKEN`.

## 2. Get a Claude API key

1. Sign up / log in at [console.anthropic.com](https://console.anthropic.com).
2. Create an API key under **Settings → API Keys**.
3. Set it as `ANTHROPIC_API_KEY`.

Voice notes are transcribed via Groq's Whisper endpoint — get a key from
[console.groq.com](https://console.groq.com) and set it as `GROQ_API_KEY`.

## 3. Configure environment variables

Copy `.env.example` to `.env` and fill in the values:

```bash
cp .env.example .env
```

- `BOT_TOKEN` — from BotFather (step 1)
- `WEBHOOK_URL` — your public HTTPS URL + `/webhook` (e.g. an ngrok tunnel while
  developing locally, or your Railway deployment URL in production)
- `ANTHROPIC_API_KEY` — from step 2
- `SWIGGY_MCP_ENDPOINT` — defaults to `https://mcp.swiggy.com/im`, no need to change
- `SWIGGY_ACCESS_TOKEN` / `SWIGGY_REFRESH_TOKEN` — leave blank on first run (see below)
- `DELIVERY_ADDRESS_ID` — leave blank until you've run `get_addresses` once (see below)
- `DATABASE_PATH` — defaults to `data/grocerybot.db`, no need to change

## 4. Run locally

This project uses [uv](https://docs.astral.sh/uv/) for dependency management. Install
it first if you don't have it (`curl -LsSf https://astral.sh/uv/install.sh | sh` or
`brew install uv`), then sync the environment:

```bash
uv sync
```

This creates a `.venv` and installs every dependency pinned in `uv.lock`. You don't
need to activate the venv — prefix commands with `uv run` instead.

Expose a local port with a tunnel (e.g. `ngrok http 8000`) and set `WEBHOOK_URL` in
`.env` to the tunnel's HTTPS URL + `/webhook`.

```bash
uv run uvicorn app.main:app --reload --port 8000
```

To add or update a dependency later: `uv add <package>` (or `uv remove <package>`),
which updates both `pyproject.toml` and `uv.lock`.

On first run, the Swiggy MCP client has no stored tokens. The `mcp` SDK triggers an
OAuth 2.1 + PKCE flow automatically the first time a Swiggy tool is called (e.g. the
first photo you send, or `/restock`) — it opens a browser for Swiggy phone + OTP
login against the `http://localhost/callback` redirect URI, which Swiggy whitelists
by default (no client ID setup needed, Dynamic Client Registration handles it).
Tokens are then cached in `token_store.json` and refreshed automatically (~5 day
token lifetime).

Once authenticated, call `get_addresses` (e.g. via a quick script using
`app.instamart.mcp_client.client`) to find your `address_id`, then set
`DELIVERY_ADDRESS_ID` in `.env` and restart.

## 5. Deploy to Railway

1. Push this repo to GitHub.
2. Create a new Railway project from the repo.
3. Set all the environment variables from `.env` in Railway's dashboard (Railway
   will assign `PORT` automatically — the `Procfile` already reads `$PORT`).
4. Set `WEBHOOK_URL` to `https://<your-railway-domain>/webhook`.
5. Deploy. On startup the app re-registers the Telegram webhook against
   `WEBHOOK_URL` automatically.
6. If `token_store.json` isn't present in the deployed container, complete the OAuth
   flow once against the deployed instance (or copy a locally-generated
   `token_store.json` over) so Swiggy tokens persist across restarts — otherwise set
   `SWIGGY_ACCESS_TOKEN` / `SWIGGY_REFRESH_TOKEN` directly as env vars to seed it.

## 6. Customize the diet plan

Edit `app/planner/diet_plan.json` directly:

- `config` — daily calorie/protein targets, diet type, `shop_ahead_days`
- `pantry_staples` — items never added to the shopping list (salt, oil, spices, etc.)
- `weekly_plan` — one entry per day (`monday`..`sunday`), each with `breakfast`,
  `lunch`, `snack`, `dinner`. Each meal has a `name` and an `ingredients` list of
  `{"item": ..., "qty": ...}`.

The file is read fresh on each request, but changes only take effect after a
redeploy/restart in production.

## Commands

- `/start` — welcome message
- `/help` — list of commands
- `/restock` — order everything needed for tomorrow's meals, no photo needed
- `/history` — last 5-10 Instamart orders
- `/spend` — weekly spend summary
- `/zones` — list kitchen zones with last-scanned staleness
- `/inventory [zone_id]` — combined kitchen inventory by zone, or a single zone's full list
- `/addzone <name>` / `/removezone <zone_id>` — manage custom zones beyond the six defaults
- `/recipe <name or YouTube link>` — cook a specific recipe
- Send photos — analyzes each zone visible (fridge, masala rack, etc. in one batch), saves
  to SQLite, and orders what's missing for tomorrow using inventory across all zones
- Send a voice note — transcribed and classified the same way as text; recipe/guest
  requests get a quick "did I hear that right?" confirmation first
- Type "make palak paneer for 4", "6 guests for dinner tonight", or paste a YouTube
  recipe link — routed automatically
- Any other text — quick add to cart (e.g. "add 1kg oats and curd")

## Kitchen zones & inventory (SQLite)

Inventory is stored in `data/grocerybot.db` (gitignored, path configurable via
`DATABASE_PATH`), created automatically on first run with six default zones (fridge,
freezer, pantry, dal shelf, masala rack, countertop). Each zone scan fully replaces
that zone's prior inventory. Before ordering off stored data that wasn't just
freshly photographed (recipe mode, YouTube import, guest mode, or a photo batch that
doesn't cover every zone), the bot shows a confirmation card with per-zone staleness
so you can catch outdated data before it orders on it.
