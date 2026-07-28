"""
Community Activity Tracker Bot for Telegram
=============================================
Tracks member activity in a group (messages, task submissions, games),
keeps a points-based leaderboard, and lets admins run reward tasks
(e.g. "like/comment/repost this post, send screenshot") with manual
approval, plus generates season reports (e.g. every 3 months).

Setup instructions are in README.md.
"""

import os
import re
import sqlite3
import logging
import datetime
from contextlib import contextmanager

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
# Comma-separated Telegram user IDs of people allowed to run admin commands,
# e.g. "123456789,987654321". Group admins are also treated as bot admins
# automatically (checked live via get_chat_member).
EXTRA_ADMIN_IDS = {
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
}

DB_PATH = os.environ.get("DB_PATH", "tracker.db")

POINTS_PER_MESSAGE = 1
MAX_MESSAGE_POINTS_PER_DAY = 20  # anti-spam cap on plain-message points

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                points INTEGER DEFAULT 0,
                message_count INTEGER DEFAULT 0,
                daily_message_points INTEGER DEFAULT 0,
                daily_reset_date TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                description TEXT,
                points_value INTEGER,
                active INTEGER DEFAULT 1,
                created_at TEXT,
                announcement_chat_id INTEGER,
                announcement_message_id INTEGER,
                proof_type TEXT DEFAULT 'either'
            )
        """)
        # Migration for databases created before these columns existed.
        for col, coltype in (
            ("announcement_chat_id", "INTEGER"),
            ("announcement_message_id", "INTEGER"),
            ("proof_type", "TEXT DEFAULT 'either'"),
        ):
            try:
                c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError:
                pass  # column already exists
        c.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                task_id INTEGER,
                status TEXT DEFAULT 'pending',
                submitted_at TEXT,
                reviewed_at TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS point_log (
                log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                points INTEGER,
                reason TEXT,
                timestamp TEXT
            )
        """)


def today_str():
    return datetime.date.today().isoformat()


def ensure_user(conn, user):
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id = ?", (user.id,))
    row = c.fetchone()
    if row is None:
        c.execute(
            "INSERT INTO users (user_id, username, first_name, daily_reset_date) "
            "VALUES (?, ?, ?, ?)",
            (user.id, user.username or "", user.first_name or "", today_str()),
        )
    else:
        # keep username/first_name fresh
        c.execute(
            "UPDATE users SET username = ?, first_name = ? WHERE user_id = ?",
            (user.username or "", user.first_name or "", user.id),
        )
        if row["daily_reset_date"] != today_str():
            c.execute(
                "UPDATE users SET daily_message_points = 0, daily_reset_date = ? "
                "WHERE user_id = ?",
                (today_str(), user.id),
            )


def add_points(conn, user_id, points, reason):
    c = conn.cursor()
    c.execute("UPDATE users SET points = points + ? WHERE user_id = ?", (points, user_id))
    c.execute(
        "INSERT INTO point_log (user_id, points, reason, timestamp) VALUES (?, ?, ?, ?)",
        (user_id, points, reason, datetime.datetime.now().isoformat()),
    )


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user_id = update.effective_user.id
    if user_id in EXTRA_ADMIN_IDS:
        return True
    chat = update.effective_chat
    if chat.type == "private":
        return user_id in EXTRA_ADMIN_IDS
    try:
        member = await context.bot.get_chat_member(chat.id, user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# User-facing commands
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👑 Welcome to Web3withOjoche's community bot! 🚀\n\n"
        "I track member activity — messages, task participation, and "
        "engagement — and keep a live leaderboard. Stay active and "
        "complete tasks to climb the ranks and win rewards every season.\n\n"
        "Commands:\n"
        "/leaderboard - Top active members\n"
        "/myscore - Your points and rank\n"
        "/tasks - Current active tasks\n\n"
        "To complete a task, reply to the task announcement with your "
        "screenshot as proof — an admin will review it."
    )


async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT username, first_name, points FROM users "
            "ORDER BY points DESC LIMIT 10"
        )
        rows = c.fetchall()

    if not rows:
        await update.message.reply_text("No activity tracked yet.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 *Leaderboard* 🏆\n"]
    for i, row in enumerate(rows):
        name = f"@{row['username']}" if row["username"] else row["first_name"]
        rank_icon = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{rank_icon} {name} — {row['points']} pts")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_myscore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    with get_db() as conn:
        ensure_user(conn, user)
        c = conn.cursor()
        c.execute("SELECT points, message_count FROM users WHERE user_id = ?", (user.id,))
        row = c.fetchone()
        c.execute(
            "SELECT COUNT(*) + 1 as rank FROM users WHERE points > "
            "(SELECT points FROM users WHERE user_id = ?)",
            (user.id,),
        )
        rank = c.fetchone()["rank"]

    await update.message.reply_text(
        f"📊 Your stats:\nPoints: {row['points']}\n"
        f"Messages sent: {row['message_count']}\nRank: #{rank}"
    )


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT task_id, description, points_value, proof_type FROM tasks "
            "WHERE active = 1 ORDER BY created_at DESC"
        )
        rows = c.fetchall()

    if not rows:
        await update.message.reply_text("No active tasks right now.")
        return

    icons = {"photo": "📸", "text": "💬", "either": "✅"}
    lines = ["📋 *Active tasks*\n"]
    for row in rows:
        icon = icons.get(row["proof_type"] or "either", "✅")
        lines.append(
            f"{icon} #{row['task_id']} ({row['points_value']} pts): {row['description']}"
        )
    lines.append(
        "\n📸 = screenshot required · 💬 = message only · ✅ = either works\n"
        "\nTo submit: reply directly to that task's announcement message, "
        "or tag your message/screenshot with its number (e.g. \"#3\")."
    )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Shared helper: figure out which active task a message is proof for
# ---------------------------------------------------------------------------

def match_task_id(conn, message):
    """Returns the active task_id a message is replying to / tagged with,
    or None if it can't be determined from a reply or a '#N' tag.
    Does NOT fall back to 'the only active task' — callers decide that."""
    c = conn.cursor()
    task_id = None

    if message.reply_to_message:
        replied_id = message.reply_to_message.message_id
        c.execute(
            "SELECT task_id FROM tasks WHERE announcement_message_id = ? AND active = 1",
            (replied_id,),
        )
        row = c.fetchone()
        if row:
            task_id = row["task_id"]

    if task_id is None:
        text_to_check = message.caption or message.text or ""
        match = re.search(r"#(\d+)", text_to_check)
        if match:
            c.execute(
                "SELECT task_id FROM tasks WHERE task_id = ? AND active = 1",
                (int(match.group(1)),),
            )
            row = c.fetchone()
            if row:
                task_id = row["task_id"]

    return task_id


def create_submission(conn, user_id, task_id):
    c = conn.cursor()
    c.execute(
        "INSERT INTO submissions (user_id, task_id, status, submitted_at) "
        "VALUES (?, ?, 'pending', ?)",
        (user_id, task_id, datetime.datetime.now().isoformat()),
    )
    return c.lastrowid


# ---------------------------------------------------------------------------
# Passive tracking: every text message earns a small, capped amount of points.
# If the text is also a reply to a task (or tagged #N), it's logged as a
# submission too — so tasks that don't need a screenshot (e.g. "say hi",
# "share one word") still get tracked automatically.
# ---------------------------------------------------------------------------

async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_chat.type not in ("group", "supergroup"):
        return
    user = update.effective_user
    if user.is_bot:
        return

    submission_task_id = None
    submission_id = None

    with get_db() as conn:
        ensure_user(conn, user)
        c = conn.cursor()
        c.execute(
            "SELECT daily_message_points FROM users WHERE user_id = ?", (user.id,)
        )
        current_daily = c.fetchone()["daily_message_points"]

        c.execute(
            "UPDATE users SET message_count = message_count + 1 WHERE user_id = ?",
            (user.id,),
        )

        if current_daily < MAX_MESSAGE_POINTS_PER_DAY:
            add_points(conn, user.id, POINTS_PER_MESSAGE, "message activity")
            c.execute(
                "UPDATE users SET daily_message_points = daily_message_points + ? "
                "WHERE user_id = ?",
                (POINTS_PER_MESSAGE, user.id),
            )

        # Only log a submission if this is an explicit reply to a task
        # announcement or tagged with #N — NOT for every ordinary message.
        task_id = match_task_id(conn, update.message)
        if task_id is not None:
            submission_task_id = task_id
            submission_id = create_submission(conn, user.id, task_id)

    if submission_task_id is not None:
        await update.message.reply_text(
            f"✅ Got your submission for task #{submission_task_id}! "
            f"Submission #{submission_id} is pending admin review."
        )


async def handle_photo_submission(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """When a member sends a photo in the group, figure out which active task
    it's proof for, and log it as a pending submission.

    Matching order:
      1. If it's a reply to a task's announcement message, use that task.
      2. If the caption contains a task tag like '#3', use that task.
      3. If there's exactly one active task, fall back to it.
      4. Otherwise, ask the member to clarify.
    """
    if not update.message or update.effective_chat.type not in ("group", "supergroup"):
        return
    user = update.effective_user
    if user.is_bot:
        return

    with get_db() as conn:
        ensure_user(conn, user)
        c = conn.cursor()
        task_id = match_task_id(conn, update.message)

        if task_id is None:
            c.execute("SELECT task_id FROM tasks WHERE active = 1")
            active_tasks = c.fetchall()
            if len(active_tasks) == 1:
                task_id = active_tasks[0]["task_id"]
            elif len(active_tasks) == 0:
                return  # no active task at all, nothing to attach this to
            else:
                await update.message.reply_text(
                    "I couldn't tell which task this screenshot is for, since "
                    "there's more than one active task. Please reply directly to "
                    "the task's announcement message, or add its number in your "
                    "caption (e.g. \"#3\"). Use /tasks to see active task numbers."
                )
                return

        submission_id = create_submission(conn, user.id, task_id)

    await update.message.reply_text(
        f"✅ Got your screenshot for task #{task_id}! Submission #{submission_id} "
        "is pending admin review."
    )


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------

async def cmd_newtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Admins only.")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /newtask <points> [photo|text|either] <description>\n\n"
            "The proof type is optional (defaults to 'either'):\n"
            "• photo — must reply with a screenshot\n"
            "• text — just reply with a message, no screenshot needed\n"
            "• either — reply with either one\n\n"
            "Examples:\n"
            "/newtask 10 photo Like, comment and repost our latest post\n"
            "/newtask 5 text Share one word that describes Web3 to you"
        )
        return
    try:
        points = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Points must be a number.")
        return

    proof_type = "either"
    remaining_args = context.args[1:]
    if remaining_args and remaining_args[0].lower() in ("photo", "text", "either", "screenshot"):
        flag = remaining_args[0].lower()
        proof_type = "photo" if flag == "screenshot" else flag
        remaining_args = remaining_args[1:]

    description = " ".join(remaining_args)
    if not description:
        await update.message.reply_text("Please include a task description.")
        return

    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO tasks (description, points_value, active, created_at, proof_type) "
            "VALUES (?, ?, 1, ?, ?)",
            (description, points, datetime.datetime.now().isoformat(), proof_type),
        )
        task_id = c.lastrowid

    if proof_type == "photo":
        proof_note = "📸 This task needs a screenshot as proof. Reply to THIS message with your screenshot!"
    elif proof_type == "text":
        proof_note = "💬 Just reply to THIS message with your answer — no screenshot needed!"
    else:
        proof_note = "Reply to THIS message with your answer or a screenshot — either works!"

    sent = await update.message.reply_text(
        f"📢 New task #{task_id} created ({points} pts):\n{description}\n\n"
        f"{proof_note}\n"
        f"(Can't reply directly? Caption/tag your message with #{task_id} instead.)"
    )

    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE tasks SET announcement_chat_id = ?, announcement_message_id = ? "
            "WHERE task_id = ?",
            (sent.chat_id, sent.message_id, task_id),
        )


async def cmd_endtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /endtask <task_id>")
        return
    task_id = context.args[0]
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE tasks SET active = 0 WHERE task_id = ?", (task_id,))
    await update.message.reply_text(f"Task #{task_id} closed.")


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Admins only.")
        return
    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT s.submission_id, u.username, u.first_name, t.description, t.points_value
            FROM submissions s
            JOIN users u ON u.user_id = s.user_id
            JOIN tasks t ON t.task_id = s.task_id
            WHERE s.status = 'pending'
            ORDER BY s.submitted_at ASC
        """)
        rows = c.fetchall()

    if not rows:
        await update.message.reply_text("No pending submissions.")
        return

    lines = ["🕓 *Pending submissions*\n"]
    for row in rows:
        name = f"@{row['username']}" if row["username"] else row["first_name"]
        lines.append(
            f"#{row['submission_id']}: {name} — \"{row['description']}\" "
            f"({row['points_value']} pts)"
        )
    lines.append("\nUse /approve <id> or /reject <id> to review.")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /approve <submission_id>")
        return
    submission_id = context.args[0]

    with get_db() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT s.user_id, t.points_value, t.description, s.status
            FROM submissions s JOIN tasks t ON t.task_id = s.task_id
            WHERE s.submission_id = ?
        """, (submission_id,))
        row = c.fetchone()
        if row is None:
            await update.message.reply_text("Submission not found.")
            return
        if row["status"] != "pending":
            await update.message.reply_text(f"Submission already {row['status']}.")
            return

        add_points(conn, row["user_id"], row["points_value"], f"task: {row['description']}")
        c.execute(
            "UPDATE submissions SET status = 'approved', reviewed_at = ? "
            "WHERE submission_id = ?",
            (datetime.datetime.now().isoformat(), submission_id),
        )

    await update.message.reply_text(f"✅ Approved #{submission_id}, points awarded.")


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /reject <submission_id>")
        return
    submission_id = context.args[0]

    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE submissions SET status = 'rejected', reviewed_at = ? "
            "WHERE submission_id = ? AND status = 'pending'",
            (datetime.datetime.now().isoformat(), submission_id),
        )
        if c.rowcount == 0:
            await update.message.reply_text("Submission not found or already reviewed.")
            return

    await update.message.reply_text(f"❌ Rejected #{submission_id}.")


async def cmd_addpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Admins only.")
        return
    target_id, points, reason = await _parse_points_args(update, context)
    if target_id is None:
        return
    with get_db() as conn:
        add_points(conn, target_id, points, reason)
    await update.message.reply_text(f"Added {points} pts to user {target_id} ({reason}).")


async def cmd_removepoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Admins only.")
        return
    target_id, points, reason = await _parse_points_args(update, context)
    if target_id is None:
        return
    with get_db() as conn:
        add_points(conn, target_id, -points, f"deduction: {reason}")
    await update.message.reply_text(f"Removed {points} pts from user {target_id} ({reason}).")


async def _parse_points_args(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Supports: reply to a user's message with /addpoints <points> <reason>,
    or /addpoints <user_id> <points> <reason>."""
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
        if len(context.args) < 1:
            await update.message.reply_text("Usage (as reply): /addpoints <points> <reason>")
            return None, None, None
        points = int(context.args[0])
        reason = " ".join(context.args[1:]) or "admin adjustment"
    else:
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /addpoints <user_id> <points> <reason>, or reply to a "
                "user's message with /addpoints <points> <reason>"
            )
            return None, None, None
        target_id = int(context.args[0])
        points = int(context.args[1])
        reason = " ".join(context.args[2:]) or "admin adjustment"
    return target_id, points, reason


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Admins only.")
        return
    with get_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT username, first_name, points, message_count FROM users "
            "ORDER BY points DESC"
        )
        rows = c.fetchall()

    if not rows:
        await update.message.reply_text("No data yet.")
        return

    lines = [f"📈 Full activity report ({today_str()})\n"]
    for i, row in enumerate(rows, start=1):
        name = f"@{row['username']}" if row["username"] else row["first_name"]
        lines.append(f"{i}. {name} — {row['points']} pts, {row['message_count']} messages")
    text = "\n".join(lines)

    # Telegram messages cap at 4096 chars; send as a file if it's long.
    if len(text) > 3500:
        path = "/tmp/activity_report.txt"
        with open(path, "w") as f:
            f.write(text)
        await update.message.reply_document(document=open(path, "rb"), filename="activity_report.txt")
    else:
        await update.message.reply_text(text)


async def cmd_resetseason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Archives current standings implicitly via /report, then zeroes points
    for a fresh cycle (e.g. new 3-month season). Message counts are kept."""
    if not await is_admin(update, context):
        await update.message.reply_text("⛔ Admins only.")
        return
    await update.message.reply_text("Generating final report before reset...")
    await cmd_report(update, context)
    with get_db() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET points = 0, daily_message_points = 0")
    await update.message.reply_text("🔄 New season started — all points reset to 0.")


# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Set the BOT_TOKEN environment variable to your bot token from BotFather."
        )

    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    app.add_handler(CommandHandler("myscore", cmd_myscore))
    app.add_handler(CommandHandler("tasks", cmd_tasks))

    app.add_handler(CommandHandler("newtask", cmd_newtask))
    app.add_handler(CommandHandler("endtask", cmd_endtask))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("addpoints", cmd_addpoints))
    app.add_handler(CommandHandler("removepoints", cmd_removepoints))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("resetseason", cmd_resetseason))

    # Photos are treated as task-proof submissions
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_submission))
    # Plain text messages earn passive activity points
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_message))

    logger.info("Bot starting (polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()

