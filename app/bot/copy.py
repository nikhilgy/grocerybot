"""Static bot copy shared between command handlers and intent-routed replies.

Lives in its own module (rather than in handlers.py) so orchestrator.py can use
HELP_TEXT for the "help" intent without a circular import (handlers.py already
imports orchestrator).
"""
from __future__ import annotations

HELP_TEXT = """<b>GroceryBot commands</b>

📸 Send fridge/pantry photos — I'll figure out what's missing for tomorrow and order it.
🎙️ Send a voice note — same as texting me a request.
💬 Just type, e.g. "add 1kg oats", "make palak paneer for 4", "6 guests for dinner tonight", or paste a YouTube recipe link.

/start — Welcome message
/help — This list
/restock — Order everything needed for tomorrow's meals (no photo needed)
/history — Recent Instamart orders
/spend — Weekly spending summary
/zones — List your kitchen zones and when each was last scanned
/inventory — Show everything currently stored, by zone
/addzone &lt;name&gt; — Add a custom kitchen zone
/removezone &lt;zone_id&gt; — Remove a zone
/recipe &lt;name or YouTube link&gt; — Cook a specific recipe
"""

START_TEXT = """👋 Hi! I'm <b>GroceryBot</b>.

Send me photos of your fridge/pantry and I'll check them against your diet plan, figure out what's missing for tomorrow's meals, and order it from Swiggy Instamart.

You can also:
🎙️ send a voice note
💬 type something like "add 1kg oats"
🍳 say "make palak paneer for 4" or paste a YouTube recipe link
🎉 mention guests coming and I'll scale the order
⚡ use /restock to order tomorrow's ingredients without a photo

Type /help to see all commands.
"""
