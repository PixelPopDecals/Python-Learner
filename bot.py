import logging
import sqlite3
import json
import os
import random
import asyncio
import requests
import hashlib
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Generator

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMember
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
    TypeHandler
)
from textblob import TextBlob
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======================
# SECURE CONFIGURATION
# ======================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Token now ONLY loads from environment variables (CRITICAL SECURITY CHANGE)
TOKEN = os.getenv("TELEGRAM_TOKEN")  # No hardcoded fallback
if not TOKEN:
    raise ValueError("TELEGRAM_TOKEN environment variable is required")

DB_PATH = "/data/codecoach.db" if os.path.exists("/data") else "codecoach.db"
PISTON_API = "https://emkc.org/api/v2/piston/execute"
MAX_ATTEMPTS = 5
REQUEST_TIMEOUT = 15
REQUIRED_CHANNEL = "@PixelPopDecals"

# Ensure /data directory exists
if "/data" in DB_PATH:
    os.makedirs("/data", exist_ok=True)

# ======================
# ENHANCED DATABASE LAYER
# ======================
class DatabaseManager:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance
    
    def _initialize(self):
        self.conn = sqlite3.connect(
            DB_PATH,
            timeout=30,
            check_same_thread=False,
            isolation_level=None
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()
        self._seed_challenges_from_json()
    
    def _init_tables(self):
        with self.conn:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS challenges (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL CHECK(length(title) <= 100),
                description TEXT NOT NULL CHECK(length(description) <= 500),
                difficulty INTEGER NOT NULL CHECK(difficulty BETWEEN 1 AND 5),
                category TEXT NOT NULL CHECK(length(category) <= 50),
                pre_lesson TEXT CHECK(length(pre_lesson) <= 1000),
                hints TEXT NOT NULL CHECK(length(hints) <= 2000),
                solution TEXT NOT NULL CHECK(length(solution) <= 2000),
                test_cases TEXT NOT NULL CHECK(length(test_cases) <= 5000),
                common_mistakes TEXT CHECK(length(common_mistakes) <= 2000),
                popularity INTEGER DEFAULT 0 CHECK(popularity >= 0)
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT CHECK(length(username) <= 64),
                xp INTEGER DEFAULT 0 CHECK(xp >= 0),
                level INTEGER DEFAULT 1 CHECK(level >= 1),
                streak INTEGER DEFAULT 0 CHECK(streak >= 0),
                last_active DATE,
                preferred_topics TEXT CHECK(length(preferred_topics) <= 500),
                daily_completed BOOLEAN DEFAULT FALSE,
                tokens_used INTEGER DEFAULT 0 CHECK(tokens_used >= 0),
                last_attempt TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_progress (
                user_id INTEGER NOT NULL,
                challenge_id INTEGER NOT NULL,
                completed BOOLEAN DEFAULT FALSE,
                attempts INTEGER DEFAULT 0 CHECK(attempts >= 0),
                last_attempt TEXT,
                current_hint_level INTEGER DEFAULT 0 CHECK(current_hint_level >= 0),
                PRIMARY KEY (user_id, challenge_id),
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (challenge_id) REFERENCES challenges(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_challenges_difficulty ON challenges(difficulty);
            CREATE INDEX IF NOT EXISTS idx_user_progress_user ON user_progress(user_id);
            """)
    
    def _seed_challenges_from_json(self):
        """Load challenges from challenges.json file"""
        try:
            with self.conn:
                if self.conn.execute("SELECT COUNT(*) FROM challenges").fetchone()[0] == 0:
                    if not os.path.exists("challenges.json"):
                        raise FileNotFoundError("challenges.json not found in working directory")
                    
                    with open("challenges.json", 'r', encoding='utf-8') as f:
                        challenges = json.load(f)
                    
                    # Prepare data for insertion
                    challenge_data = []
                    for challenge in challenges:
                        challenge_data.append((
                            challenge['id'],
                            challenge['title'],
                            challenge['description'],
                            challenge['difficulty'],
                            challenge['category'],
                            challenge.get('pre_lesson', ''),
                            json.dumps(challenge.get('hints', [])),
                            challenge['solution'],
                            json.dumps(challenge.get('test_cases', [])),
                            json.dumps(challenge.get('common_mistakes', [])),
                            challenge.get('popularity', 0)
                        ))
                    
                    self.conn.executemany("""
                    INSERT INTO challenges VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """, challenge_data)
                    logger.info(f"Successfully loaded {len(challenge_data)} challenges from JSON")
        except Exception as e:
            logger.error(f"Failed to seed challenges from JSON: {e}")
            raise

    def get_db_connection(self) -> sqlite3.Connection:
        return self.conn

# [Rest of the code remains EXACTLY THE SAME as original]
# [PythonCoachBot class and all its methods stay identical]
# [main() function stays identical]

class PythonCoachBot:
    def __init__(self):
        self.db = DatabaseManager()
        self.scheduler = AsyncIOScheduler()
        self.user_attempts = {}
        self.setup_schedulers()
    
    def setup_schedulers(self):
        self.scheduler.add_job(
            self.send_daily_challenge,
            'cron',
            hour=9,
            minute=0,
            timezone='UTC'
        )
        self.scheduler.add_job(
            self.reset_daily_status,
            'cron',
            hour=0,
            minute=1,
            timezone='UTC'
        )
        self.scheduler.add_job(
            self.cleanup_attempts,
            'interval',
            minutes=5
        )
        self.scheduler.start()
    
    async def cleanup_attempts(self):
        now = datetime.now()
        self.user_attempts = {
            user_id: count 
            for user_id, (count, timestamp) in self.user_attempts.items()
            if (now - timestamp).total_seconds() < 300
        }
    
    async def reset_daily_status(self):
        with self.db.get_db_connection() as conn:
            conn.execute("UPDATE users SET daily_completed = FALSE")
            conn.commit()
    
    async def check_rate_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        user_id = update.effective_user.id
        now = datetime.now()
        
        if user_id not in self.user_attempts:
            self.user_attempts[user_id] = (1, now)
        else:
            count, _ = self.user_attempts[user_id]
            self.user_attempts[user_id] = (count + 1, now)
        
        if self.user_attempts[user_id][0] > 10:
            await update.message.reply_text(
                "⏳ Please wait a few minutes before making more requests."
            )
            return False
        return True
    
    async def check_channel_membership(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Check if user is a member of the required channel"""
        try:
            user_id = update.effective_user.id
            member = await context.bot.get_chat_member(REQUIRED_CHANNEL.lower(), user_id)
            return member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]
        except Exception as e:
            logger.error(f"Failed to check channel membership: {e}")
            return False

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enhanced /start command with channel verification"""
        user = update.effective_user
        
        if not await self.check_channel_membership(update, context):
            await update.message.reply_text(
                f"👋 Welcome {user.first_name}!\n\n"
                "📢 To use this bot, please join our official channel first:\n"
                f"👉 {REQUIRED_CHANNEL}\n\n"
                "After joining, press /start again or tap 'I Joined' below.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Join Channel", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}"),
                        InlineKeyboardButton("I Joined", callback_data="verify_join")
                    ]
                ])
            )
            return

        try:
            with self.db.get_db_connection() as conn:
                conn.execute("""
                INSERT INTO users (user_id, username, last_active)
                VALUES (?, ?, date('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    last_active = excluded.last_active
                """, (user.id, user.username))
                
                level, xp = conn.execute("""
                SELECT level, xp FROM users WHERE user_id = ?
                """, (user.id,)).fetchone()
                
            keyboard = [
                [InlineKeyboardButton("🎯 Start Learning", callback_data="new_challenge")],
                [InlineKeyboardButton("📊 My Progress", callback_data="progress")],
                [InlineKeyboardButton("⚔️ Coding Battle", callback_data="battle")]
            ]
            
            await update.message.reply_text(
                f"🐍 *Welcome to Python Coach, {user.first_name}!*\n\n"
                f"📊 *Your Level:* {level}\n"
                f"✨ *XP:* {xp}\n\n"
                "Choose an option to begin your Python mastery journey:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
            
        except Exception as e:
            logger.error(f"Start command failed: {e}")
            await update.message.reply_text(
                "😞 We're having technical difficulties. Please try again later."
            )

    async def handle_verify_join(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle 'I Joined' button presses"""
        query = update.callback_query
        await query.answer()
        
        if await self.check_channel_membership(update, context):
            await self.start(update, context)
        else:
            await query.edit_message_text(
                "❌ Couldn't verify your membership. Please:\n"
                "1. Make sure you joined @PixelPopDecals\n"
                "2. Wait a minute\n"
                "3. Try again",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton("Join Channel", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}"),
                        InlineKeyboardButton("Try Again", callback_data="verify_join")
                    ]
                ])
            )

    async def send_daily_challenge(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send personalized daily challenge"""
        if not await self.check_channel_membership(update, context):
            await self.prompt_channel_join(update)
            return

        user = update.effective_user
        try:
            with self.db.get_db_connection() as conn:
                completed = conn.execute("""
                SELECT daily_completed FROM users WHERE user_id = ?
                """, (user.id,)).fetchone()[0]
                
                if completed:
                    await update.message.reply_text(
                        "🎉 You've already completed today's challenge! "
                        "Come back tomorrow for more."
                    )
                    return
                
                level, topics = conn.execute("""
                SELECT level, preferred_topics FROM users WHERE user_id = ?
                """, (user.id,)).fetchone()
                
                topics_filter = ""
                params = [level]
                
                if topics:
                    try:
                        topics_list = json.loads(topics)
                        placeholders = ",".join(["?"] * len(topics_list))
                        topics_filter = f" AND category IN ({placeholders})"
                        params.extend(topics_list)
                    except json.JSONDecodeError:
                        logger.error(f"Invalid topics JSON for user {user.id}")
                
                query = f"""
                SELECT * FROM challenges 
                WHERE difficulty <= ? {topics_filter}
                ORDER BY RANDOM()
                LIMIT 1
                """
                
                challenge = conn.execute(query, params).fetchone()
                
                if challenge:
                    try:
                        test_case = json.loads(challenge[8])[0]
                        message = (
                            f"📅 *Daily Python Challenge*\n\n"
                            f"🏷️ **{challenge[1]}** (Level {challenge[3]})\n\n"
                            f"📚 *Concept:* {challenge[5]}\n\n"
                            f"💻 *Task:* {challenge[2]}\n\n"
                            f"⚡ *Example:* `{test_case['input']}` → "
                            f"`{test_case['output']}`"
                        )
                    except (json.JSONDecodeError, KeyError, IndexError) as e:
                        logger.error(f"Invalid test case format for challenge {challenge[0]}: {e}")
                        message = (
                            f"📅 *Daily Python Challenge*\n\n"
                            f"🏷️ **{challenge[1]}** (Level {challenge[3]})\n\n"
                            f"📚 *Concept:* {challenge[5]}\n\n"
                            f"💻 *Task:* {challenge[2]}"
                        )
                    
                    await update.message.reply_text(
                        message,
                        parse_mode="Markdown"
                    )
                else:
                    await update.message.reply_text(
                        "No challenges available matching your preferences. "
                        "Update your topics with /topics"
                    )
                    
        except Exception as e:
            logger.error(f"Daily challenge failed: {e}")
            await update.message.reply_text(
                "Failed to load daily challenge. Please try again later."
            )

    async def prompt_channel_join(self, update: Update):
        """Show channel join prompt"""
        await update.message.reply_text(
            "🔒 Please join our channel to access this feature:\n"
            f"👉 {REQUIRED_CHANNEL}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")]
            ])
        )

    async def handle_code_submission(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process and evaluate Python code submissions"""
        if not await self.check_channel_membership(update, context):
            await self.prompt_channel_join(update)
            return

        user = update.effective_user
        code = update.message.text
        
        try:
            with self.db.get_db_connection() as conn:
                challenge = conn.execute("""
                SELECT c.*, up.attempts 
                FROM user_progress up
                JOIN challenges c ON up.challenge_id = c.id
                WHERE up.user_id = ? AND up.completed = FALSE
                """, (user.id,)).fetchone()
                
                if not challenge:
                    await update.message.reply_text(
                        "No active challenge! Start with /challenge or /daily"
                    )
                    return
                
                try:
                    test_cases = json.loads(challenge[8])
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid test cases for challenge {challenge[0]}: {e}")
                    await update.message.reply_text(
                        "This challenge has invalid test cases. Please try another one."
                    )
                    return
                
                all_passed = True
                failed_case = None
                
                for case in test_cases:
                    if not await self._execute_test_case(code, case):
                        all_passed = False
                        failed_case = case
                        break
                
                if all_passed:
                    xp_earned = challenge[3] * 10
                    
                    conn.execute("""
                    UPDATE user_progress 
                    SET completed = TRUE, 
                        attempts = attempts + 1,
                        last_attempt = ?
                    WHERE user_id = ? AND challenge_id = ?
                    """, (datetime.now().isoformat(), user.id, challenge[0]))
                    
                    conn.execute("""
                    UPDATE users 
                    SET xp = xp + ?,
                        daily_completed = TRUE,
                        last_active = date('now'),
                        streak = streak + 1
                    WHERE user_id = ?
                    """, (xp_earned, user.id))
                    
                    new_level = conn.execute("""
                    SELECT level FROM users WHERE user_id = ?
                    """, (user.id,)).fetchone()[0]
                    
                    response = (
                        f"✅ *Correct Solution!*\n\n"
                        f"✨ +{xp_earned} XP\n"
                        f"📈 Level: {new_level}\n\n"
                        f"💡 *Official Solution:*\n"
                        f"```python\n{challenge[7]}\n```"
                    )
                    
                    await update.message.reply_text(
                        response,
                        parse_mode="Markdown"
                    )
                    
                    keyboard = [
                        [InlineKeyboardButton("➡️ Next Challenge", callback_data="next_challenge")]
                    ]
                    await update.message.reply_text(
                        "What would you like to do next?",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    
                else:
                    attempts = challenge[-1] + 1
                    conn.execute("""
                    UPDATE user_progress 
                    SET attempts = ?,
                        last_attempt = ?
                    WHERE user_id = ? AND challenge_id = ?
                    """, (attempts, datetime.now().isoformat(), user.id, challenge[0]))
                    
                    if attempts >= MAX_ATTEMPTS:
                        response = (
                            f"❌ *Solution Not Quite Right*\n\n"
                            f"You've reached the max attempts. Here's the solution:\n"
                            f"```python\n{challenge[7]}\n```\n\n"
                            f"Try to understand it and attempt similar challenges!"
                        )
                    else:
                        try:
                            hints = json.loads(challenge[6])
                            current_hint = hints[min(attempts - 1, len(hints) - 1)]
                        except (json.JSONDecodeError, IndexError) as e:
                            logger.error(f"Invalid hints for challenge {challenge[0]}: {e}")
                            current_hint = "Try reviewing the problem statement"
                        
                        response = (
                            f"❌ *Test Case Failed*\n\n"
                            f"Input: `{failed_case['input']}`\n"
                            f"Expected: `{failed_case['output']}`\n\n"
                            f"💡 Hint: {current_hint}\n\n"
                            f"Attempts: {attempts}/{MAX_ATTEMPTS}"
                        )
                    
                    await update.message.reply_text(
                        response,
                        parse_mode="Markdown"
                    )
                    
        except Exception as e:
            logger.error(f"Code submission failed: {e}")
            await update.message.reply_text(
                "An error occurred while evaluating your code. Please try again."
            )

    async def _execute_test_case(self, code: str, test_case: Dict) -> bool:
        try:
            payload = {
                "language": "python",
                "version": "3.10",
                "files": [{"content": code}],
                "stdin": test_case['input']
            }
            
            response = requests.post(
                PISTON_API,
                json=payload,
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            
            return response.json()["run"]["output"].strip() == test_case['output']
            
        except Exception as e:
            logger.error(f"Code execution failed: {e}")
            return False

    async def show_progress(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user's learning progress"""
        if not await self.check_channel_membership(update, context):
            await self.prompt_channel_join(update)
            return

        user = update.effective_user
        
        try:
            with self.db.get_db_connection() as conn:
                stats = conn.execute("""
                SELECT level, xp, streak, 
                       (SELECT COUNT(*) FROM user_progress 
                        WHERE user_id = ? AND completed = TRUE) as completed_challenges
                FROM users
                WHERE user_id = ?
                """, (user.id, user.id)).fetchone()
                
                if stats:
                    level, xp, streak, completed = stats
                    xp_needed = level * 100
                    progress = min(100, (xp % xp_needed) / xp_needed * 100) if xp_needed > 0 else 0
                    
                    progress_bar = "🟩" * int(progress / 10) + "⬜" * (10 - int(progress / 10))
                    
                    await update.message.reply_text(
                        f"📊 *Your Learning Progress*\n\n"
                        f"🏅 Level: {level}\n"
                        f"✨ XP: {xp} ({progress:.1f}% to next level)\n"
                        f"{progress_bar}\n\n"
                        f"🔥 Streak: {streak} days\n"
                        f"✅ Challenges Completed: {completed}",
                        parse_mode="Markdown"
                    )
                else:
                    await update.message.reply_text(
                        "No progress data found. Start with /daily to begin learning!"
                    )
                    
        except Exception as e:
            logger.error(f"Progress check failed: {e}")
            await update.message.reply_text(
                "Failed to load progress data. Please try again later."
            )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Process inline keyboard interactions"""
        query = update.callback_query
        await query.answer()
        
        if query.data == "new_challenge":
            await self.send_daily_challenge(update, context)
        elif query.data == "progress":
            await self.show_progress(update, context)
        elif query.data == "next_challenge":
            await self.send_daily_challenge(update, context)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Global error handler"""
        logger.error("Exception while handling update:", exc_info=context.error)
        
        if update and hasattr(update, 'message'):
            await update.message.reply_text(
                "😞 An unexpected error occurred. Our engineers have been notified.\n"
                "Please try your request again later."
            )

def main():
    bot = PythonCoachBot()
    
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(TypeHandler(Update, bot.check_rate_limit), group=-1)
    
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("daily", bot.send_daily_challenge))
    application.add_handler(CommandHandler("progress", bot.show_progress))
    
    application.add_handler(CallbackQueryHandler(bot.handle_callback))
    application.add_handler(CallbackQueryHandler(bot.handle_verify_join, pattern="^verify_join$"))
    
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        bot.handle_code_submission
    ))
    
    application.add_error_handler(bot.error_handler)
    
    application.run_polling()

if __name__ == "__main__":
    main()