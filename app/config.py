"""All environment configuration in one place."""
import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# Telegram
BOT_TOKEN = _require("BOT_TOKEN")
WEBHOOK_URL = _require("WEBHOOK_URL")

# Claude
ANTHROPIC_API_KEY = _require("ANTHROPIC_API_KEY")
CLAUDE_VISION_MODEL = "claude-sonnet-4-6"
CLAUDE_TEXT_MODEL = "claude-sonnet-4-6"

# Voice transcription (Groq Whisper)
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# Swiggy Instamart MCP
SWIGGY_MCP_ENDPOINT = os.getenv("SWIGGY_MCP_ENDPOINT", "https://mcp.swiggy.com/im")
SWIGGY_ACCESS_TOKEN = os.getenv("SWIGGY_ACCESS_TOKEN")
SWIGGY_REFRESH_TOKEN = os.getenv("SWIGGY_REFRESH_TOKEN")

TOKEN_STORE_PATH = os.getenv("TOKEN_STORE_PATH", "token_store.json")

# Local OAuth callback (Swiggy whitelists http://localhost and
# http://localhost/callback as redirect URIs for the PKCE flow).
SWIGGY_OAUTH_REDIRECT_PORT = int(os.getenv("SWIGGY_OAUTH_REDIRECT_PORT", "8765"))
SWIGGY_OAUTH_REDIRECT_URI = os.getenv(
    "SWIGGY_OAUTH_REDIRECT_URI", f"http://localhost:{SWIGGY_OAUTH_REDIRECT_PORT}/callback"
)

# Diet plan
DIET_PLAN_PATH = os.path.join(
    os.path.dirname(__file__), "planner", "diet_plan.json"
)

# Kitchen inventory database
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/grocerybot.db")
