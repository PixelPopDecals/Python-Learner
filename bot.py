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
DB_PATH = "/data/codecoach.db" if os.path.exists("/data") else "codecoach.db"
PISTON_API = "https://emkc.org/api/v2/piston/execute"
MAX_ATTEMPTS = 5
REQUEST_TIMEOUT = 15
REQUIRED_CHANNEL = "@PixelPopDecals"

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
        self._seed_challenges()
    
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
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (channel_id) REFERENCES challenges(id)
            );
            CREATE TABLE IF NOT EXISTS code_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                code TEXT NOT NULL CHECK(length(code) <= 10000),
                feedback TEXT NOT NULL CHECK(length(feedback) <= 2000),
                score INTEGER CHECK(score BETWEEN 0 AND 100),
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_challenges_difficulty ON challenges(difficulty);
            CREATE INDEX IF NOT EXISTS idx_user_progress_user ON user_progress(user_id);
            """)
    
    def _seed_challenges(self):
        try:
            with self.conn:
                if self.conn.execute("SELECT COUNT(*) FROM challenges").fetchone()[0] == 0:
                    challenges = self._generate_challenges(1000)
                    self.conn.executemany("""
                    INSERT INTO challenges VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """, challenges)
        except Exception as e:
            logger.error(f"Failed to seed challenges: {e}")
            raise

    def _generate_challenges(self, count: int) -> List[tuple]:
        categories = [
            "Python Basics", "Algorithms", "Data Structures",
            "OOP", "Functional Programming", "File Handling",
            "Error Handling", "Web Scraping", "APIs", "Concurrency"
        ]
        
        base_challenges = [
            (1, "Sum Two Numbers", "Write function sum(a,b) that returns a+b", 1, "Python Basics",
             "Functions use def and return", json.dumps(["Use + operator", "Remember to return the result"]), 
             "def sum(a,b):\n    return a+b", 
             json.dumps([{"input":"1,2","output":"3"}, {"input":"-5,5","output":"0"}]), 
             json.dumps([{"pattern":"print(","message":"Use return not print"}]), 0),
            
            (2, "String Reverser", "Write function reverse(s) that returns reversed string", 2, "Python Basics",
             "Strings can be sliced", json.dumps(["Try s[::-1]", "Consider using ''.join(reversed(s))"]),
             "def reverse(s):\n    return s[::-1]",
             json.dumps([{"input":"'hello'","output":"'olleh'"}, {"input":"''","output":"''"}]),
             json.dumps([{"pattern":"for i","message":"Use slicing instead for better performance"}]), 0)
        ]
        
        all_challenges = []
        for i in range(count):
            category = random.choice(categories)
            difficulty = random.randint(1, 5)
            
            if category == "Algorithms":
                title = f"Algorithm Challenge {i}"
                desc = f"Implement {random.choice(['sorting', 'searching', 'pathfinding'])} algorithm"
            elif category == "OOP":
                title = f"OOP Problem {i}"
                desc = f"Create a {random.choice(['class', 'inheritance structure', 'decorator'])}"
            else:
                title = f"Challenge {i}"
                desc = f"Solve this {category.lower()} problem"
            
            all_challenges.append((
                i+1, title, desc, difficulty, category,
                f"Lesson about {category}", 
                json.dumps(["Hint 1", "Hint 2", "Hint 3"]),
                f"Solution for {title}",
                json.dumps([{"input":"sample","output":"result"}]),
                json.dumps([{"pattern":"bad","message":"Don't do this"}]),
                0
            ))
        
        return all_challenges

    def get_db_connection(self) -> sqlite3.Connection:
        return self.conn

# ======================
# CORE BOT FUNCTIONALITY
# ======================
class PythonCoachBot:
    def __init__(self):
        self.db = DatabaseManager()
        self.scheduler = AsyncIOScheduler()
        self.user_attempts = {}
        self.setup_schedulers()
    
    def setup_schedulers(self):
        self.scheduler.add_job(
            self.send_daily_challenges,
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
            member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
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
            await self._prompt_channel_join(update)
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
                    topics_list = json.loads(topics)
                    topics_filter = " AND category IN ({})".format(
                        ",".join(["?"] * len(topics_list)))
                    params.extend(topics_list)
                
                challenge = conn.execute(f"""
                SELECT * FROM challenges 
                WHERE difficulty <= ? {topics_filter}
                ORDER BY RANDOM()
                LIMIT 1
                """, params).fetchone()
                
                if challenge:
                    message = (
                        f"📅 *Daily Python Challenge*\n\n"
                        f"🏷️ **{challenge[1]}** (Level {challenge[3]})\n\n"
                        f"📚 *Concept:* {challenge[5]}\n\n"
                        f"💻 *Task:* {challenge[2]}\n\n"
                        f"⚡ *Example:* `{json.loads(challenge[8])[0]['input']}` → "
                        f"`{json.loads(challenge[8])[0]['output']}`"
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

    async def _prompt_channel_join(self, update: Update):
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
            await self._prompt_channel_join(update)
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
                
                test_cases = json.loads(challenge[8])
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
                        hints = json.loads(challenge[6])
                        current_hint = hints[min(attempts - 1, len(hints) - 1)]
                        
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
            await self._prompt_channel_join(update)
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
                    progress = min(100, (xp % xp_needed) / xp_needed * 100)
                    
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

# ======================
# APPLICATION SETUP
# ======================
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