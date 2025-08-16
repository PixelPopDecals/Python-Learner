import logging
import sqlite3
import json
import os
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Dict, List, Optional, Any, Tuple
from asyncio import TimeoutError

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
from telegram.helpers import escape_markdown
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ======================
# CONSTANTS
# ======================
MAX_ATTEMPTS = 5
REQUEST_TIMEOUT = 15
RATE_LIMIT_WINDOW = 300
RATE_LIMIT_MAX = 10
XP_PER_LEVEL = 100
MAX_LEVEL = 100
PYTHON_VERSION = "3.10"
MAX_USER_ATTEMPTS_SIZE = 1000

# ======================
# LOGGING CONFIGURATION
# ======================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ======================
# CONFIGURATION
# ======================
TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TOKEN or len(TOKEN) < 30:
    raise ValueError("Invalid TELEGRAM_TOKEN environment variable")

DB_PATH = os.getenv("DB_PATH", "codecoach.db")
PISTON_API = os.getenv("PISTON_API", "https://emkc.org/api/v2/piston/execute")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@PixelPopDecals")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# ======================
# DATABASE LAYER (THREAD-SAFE)
# ======================
class DatabaseManager:
    _instance = None
    _lock = Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialize()
            return cls._instance
    
    def _initialize(self):
        self.conn = sqlite3.connect(
            DB_PATH,
            timeout=30,
            isolation_level="IMMEDIATE",
            check_same_thread=False
        )
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._init_tables()
        self._seed_challenges_from_json()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.conn.rollback()
        else:
            self.conn.commit()
        return False
    
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
        try:
            with self.conn:
                if self.conn.execute("SELECT COUNT(*) FROM challenges").fetchone()[0] == 0:
                    json_path = os.path.join(os.path.dirname(__file__), "challenges.json")
                    if not os.path.exists(json_path):
                        raise FileNotFoundError(f"challenges.json not found at {json_path}")
                    
                    with open(json_path, 'r', encoding='utf-8') as f:
                        challenges = json.load(f)
                    
                    required_fields = {'id', 'title', 'description', 'difficulty', 
                                     'category', 'hints', 'solution', 'test_cases'}
                    for challenge in challenges:
                        if not all(field in challenge for field in required_fields):
                            raise ValueError(f"Challenge {challenge.get('id')} missing required fields")
                    
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
                    INSERT INTO challenges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, challenge_data)
                    logger.info(f"Loaded {len(challenge_data)} challenges")
        except Exception as e:
            logger.exception("Failed to seed challenges:")
            raise

# ======================
# BOT IMPLEMENTATION
# ======================
class PythonCoachBot:
    def __init__(self):
        self.db = DatabaseManager()
        self.scheduler = AsyncIOScheduler()
        self.user_attempts: Dict[int, Tuple[int, datetime]] = {}
        self.rate_limit_lock = Lock()
        self.application: Optional[Application] = None
        self.setup_schedulers()
    
    def setup_schedulers(self):
        self.scheduler.add_job(
            self._send_daily_challenges_job,
            'cron',
            hour=9,
            minute=0,
            timezone='UTC'
        )
        
        self.scheduler.add_job(
            self._reset_daily_status_job,
            'cron',
            hour=0,
            minute=1,
            timezone='UTC'
        )
        
        self.scheduler.add_job(
            self._cleanup_attempts_job,
            'interval',
            minutes=5
        )
        
        self.scheduler.start()
    
    async def _send_daily_challenges_job(self):
        try:
            if not hasattr(self, 'application') or not self.application:
                logger.error("Application not initialized in scheduler job")
                return
                
            with DatabaseManager() as db:
                users = db.conn.execute("SELECT user_id FROM users").fetchall()
                for (user_id,) in users:
                    try:
                        await self.application.bot.send_message(
                            chat_id=user_id,
                            text="⌛ Preparing your daily challenge..."
                        )
                        await self._send_daily_challenge_to_user(user_id)
                    except Exception as e:
                        logger.error(f"Failed to send to {user_id}: {str(e)}")
        except Exception as e:
            logger.error(f"Daily challenge job failed: {str(e)}")
    
    async def _send_daily_challenge_to_user(self, user_id: int):
        try:
            with DatabaseManager() as db:
                user = db.conn.execute(
                    "SELECT level, preferred_topics FROM users WHERE user_id = ?",
                    (user_id,)
                ).fetchone()
                
                if not user:
                    return
                
                level, topics = user
                query = """
                SELECT * FROM challenges 
                WHERE difficulty <= ? 
                AND category IN (SELECT value FROM json_each(?))
                ORDER BY RANDOM()
                LIMIT 1
                """
                params = [level, json.dumps([])]
                
                if topics:
                    try:
                        topics_list = json.loads(topics)
                        if topics_list:
                            params[1] = json.dumps(topics_list)
                    except json.JSONDecodeError:
                        logger.error(f"Invalid topics JSON for user {user_id}")
                
                challenge = db.conn.execute(query, params).fetchone()
                
                if challenge:
                    try:
                        test_cases = json.loads(challenge[8])
                        test_case = test_cases[0] if test_cases else {}
                        title = escape_markdown(challenge[1], version=2)
                        description = escape_markdown(challenge[2], version=2)
                        category = escape_markdown(challenge[4], version=2)
                        
                        message = (
                            f"📅 *Daily Python Challenge*\n\n"
                            f"🏷️ **{title}** (Level {challenge[3]})\n\n"
                            f"📚 *Concept:* {category}\n\n"
                            f"💻 *Task:* {description}\n\n"
                        )
                        
                        if test_case.get('input') and test_case.get('output'):
                            message += (
                                f"⚡ *Example:* `{escape_markdown(test_case['input'], version=2)}` → "
                                f"`{escape_markdown(test_case['output'], version=2)}`"
                            )
                    except (json.JSONDecodeError, KeyError, IndexError) as e:
                        logger.error(f"Invalid test case format: {str(e)}")
                        message = (
                            f"📅 *Daily Python Challenge*\n\n"
                            f"🏷️ **{title}** (Level {challenge[3]})\n\n"
                            f"📚 *Concept:* {category}\n\n"
                            f"💻 *Task:* {description}"
                        )
                    
                    await self.application.bot.send_message(
                        chat_id=user_id,
                        text=message,
                        parse_mode="MarkdownV2"
                    )
        except Exception as e:
            logger.error(f"Failed to send challenge to {user_id}: {str(e)}")
    
    async def _reset_daily_status_job(self):
        try:
            today = datetime.now(timezone.utc).date()
            with DatabaseManager() as db:
                db.conn.execute(
                    "UPDATE users SET daily_completed = FALSE WHERE last_active < ?",
                    (today,)
                )
                logger.info("Reset daily status for all users")
        except Exception as e:
            logger.error(f"Failed to reset daily status: {str(e)}")
    
    async def _cleanup_attempts_job(self):
        try:
            now = datetime.now(timezone.utc)
            with self.rate_limit_lock:
                self.user_attempts = {
                    user_id: (count, timestamp)
                    for user_id, (count, timestamp) in self.user_attempts.items()
                    if (now - timestamp).total_seconds() < RATE_LIMIT_WINDOW
                }
                
                if len(self.user_attempts) > MAX_USER_ATTEMPTS_SIZE:
                    self.user_attempts.clear()
        except Exception as e:
            logger.error(f"Failed to cleanup attempts: {str(e)}")
    
    async def check_rate_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        if not update.effective_user:
            return False
            
        user_id = update.effective_user.id
        now = datetime.now(timezone.utc)
        
        with self.rate_limit_lock:
            self.user_attempts = {
                uid: (count, ts)
                for uid, (count, ts) in self.user_attempts.items()
                if (now - ts).total_seconds() < RATE_LIMIT_WINDOW
            }

            if user_id in self.user_attempts and self.user_attempts[user_id][0] >= RATE_LIMIT_MAX:
                return False

            self.user_attempts[user_id] = (
                self.user_attempts.get(user_id, (0, now))[0] + 1,
                now
            )
            return True
    
    async def check_channel_membership(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        try:
            if not update.effective_user:
                return False
                
            user_id = update.effective_user.id
            member = await context.bot.get_chat_member(REQUIRED_CHANNEL.lower(), user_id)
            return member.status in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]
        except Exception as e:
            logger.error(f"Channel check failed: {str(e)}")
            return False

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            return
            
        user = update.effective_user
        
        if not await self.check_channel_membership(update, context):
            await self._prompt_channel_join(update)
            return

        try:
            with DatabaseManager() as db:
                db.conn.execute("""
                INSERT INTO users (user_id, username, last_active)
                VALUES (?, ?, date('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    last_active = excluded.last_active
                """, (user.id, user.username))
                
                level, xp = db.conn.execute("""
                SELECT level, xp FROM users WHERE user_id = ?
                """, (user.id,)).fetchone()
                
            keyboard = [
                [InlineKeyboardButton("🎯 Start Learning", callback_data="new_challenge")],
                [InlineKeyboardButton("📊 My Progress", callback_data="progress")],
                [InlineKeyboardButton("⚔️ Coding Battle", callback_data="battle")]
            ]
            
            await update.message.reply_text(
                f"🐍 *Welcome to Python Coach, {escape_markdown(user.first_name, version=2)}!*\n\n"
                f"📊 *Your Level:* {level}\n"
                f"✨ *XP:* {xp}\n\n"
                "Choose an option to begin your Python mastery journey:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="MarkdownV2"
            )
            
        except Exception as e:
            logger.error(f"Start command failed: {str(e)}")
            await update.message.reply_text(
                "😞 We're having technical difficulties. Please try again later."
            )

    async def handle_verify_join(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        if not update.effective_user:
            return
            
        if not await self.check_channel_membership(update, context):
            await self._prompt_channel_join(update)
            return

        user = update.effective_user
        try:
            with DatabaseManager() as db:
                completed = db.conn.execute("""
                SELECT daily_completed FROM users WHERE user_id = ?
                """, (user.id,)).fetchone()[0]
                
                if completed:
                    await update.message.reply_text(
                        "🎉 You've already completed today's challenge! "
                        "Come back tomorrow for more."
                    )
                    return
                
                await self._send_daily_challenge_to_user(user.id)
                    
        except Exception as e:
            logger.error(f"Daily challenge failed: {str(e)}")
            await update.message.reply_text(
                "Failed to load daily challenge. Please try again later."
            )

    async def _prompt_channel_join(self, update: Update):
        await update.message.reply_text(
            "🔒 Please join our channel to access this feature:\n"
            f"👉 {REQUIRED_CHANNEL}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Join Channel", url=f"https://t.me/{REQUIRED_CHANNEL[1:]}")]
            ])
        )

    async def handle_code_submission(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.message.text:
            await update.message.reply_text("No code provided.")
            return
            
        if not update.effective_user:
            return
            
        if not await self.check_channel_membership(update, context):
            await self._prompt_channel_join(update)
            return

        user = update.effective_user
        code = "".join(c for c in update.message.text if ord(c) < 128)
        
        try:
            with DatabaseManager() as db:
                challenge = db.conn.execute("""
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
                    if not test_cases:
                        raise ValueError("No test cases")
                except (json.JSONDecodeError, ValueError) as e:
                    logger.error(f"Invalid test cases: {str(e)}")
                    await update.message.reply_text(
                        "This challenge has invalid test cases. Please try another one."
                    )
                    return
                
                all_passed = True
                failed_case = None
                
                for case in test_cases:
                    if 'input' not in case or 'output' not in case:
                        logger.error("Invalid test case format")
                        all_passed = False
                        failed_case = {"input": "", "output": "Invalid test case format"}
                        break
                        
                    try:
                        if not await self._execute_test_case(code, case):
                            all_passed = False
                            failed_case = case
                            break
                    except TimeoutError:
                        await update.message.reply_text(
                            "⌛ Code execution timed out. Please simplify your solution."
                        )
                        return
                    except Exception as e:
                        logger.error(f"Test case failed: {str(e)}")
                        all_passed = False
                        failed_case = {"input": "", "output": "Execution error"}
                        break
                
                if all_passed:
                    xp_earned = challenge[3] * 10
                    today = datetime.now(timezone.utc).date()
                    
                    db.conn.execute("""
                    UPDATE user_progress 
                    SET completed = TRUE, 
                        attempts = attempts + 1,
                        last_attempt = ?
                    WHERE user_id = ? AND challenge_id = ?
                    """, (datetime.now(timezone.utc).isoformat(), user.id, challenge[0]))
                    
                    last_active = db.conn.execute(
                        "SELECT last_active FROM users WHERE user_id = ?",
                        (user.id,)
                    ).fetchone()[0]
                    
                    streak_increment = 1 if last_active != today else 0
                    
                    db.conn.execute("""
                    UPDATE users 
                    SET xp = xp + ?,
                        daily_completed = TRUE,
                        last_active = ?,
                        streak = streak + ?
                    WHERE user_id = ?
                    """, (
                        xp_earned,
                        today,
                        streak_increment,
                        user.id
                    ))
                    
                    new_level = min(
                        challenge[3] + (xp_earned // XP_PER_LEVEL),
                        MAX_LEVEL
                    )
                    
                    response = (
                        f"✅ *Correct Solution!*\n\n"
                        f"✨ +{xp_earned} XP\n"
                        f"📈 Level: {new_level}\n\n"
                        f"💡 *Official Solution:*\n"
                        f"```python\n{challenge[7]}\n```"
                    )
                    
                    await update.message.reply_text(
                        response,
                        parse_mode="MarkdownV2"
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
                    db.conn.execute("""
                    UPDATE user_progress 
                    SET attempts = ?,
                        last_attempt = ?
                    WHERE user_id = ? AND challenge_id = ?
                    """, (attempts, datetime.now(timezone.utc).isoformat(), user.id, challenge[0]))
                    
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
                            logger.error(f"Invalid hints: {str(e)}")
                            current_hint = "Try reviewing the problem statement"
                        
                        response = (
                            f"❌ *Test Case Failed*\n\n"
                            f"Input: `{escape_markdown(failed_case['input'], version=2)}`\n"
                            f"Expected: `{escape_markdown(failed_case['output'], version=2)}`\n\n"
                            f"💡 Hint: {escape_markdown(current_hint, version=2)}\n\n"
                            f"Attempts: {attempts}/{MAX_ATTEMPTS}"
                        )
                    
                    await update.message.reply_text(
                        response,
                        parse_mode="MarkdownV2"
                    )
                    
        except Exception as e:
            logger.exception("Code submission failed:")
            await update.message.reply_text(
                "An error occurred while evaluating your code. Please try again."
            )

    async def _execute_test_case(self, code: str, test_case: Dict) -> bool:
        async with aiohttp.ClientSession() as session:
            for attempt in range(3):
                try:
                    payload = {
                        "language": "python",
                        "version": PYTHON_VERSION,
                        "files": [{"content": code}],
                        "stdin": test_case['input']
                    }
                    
                    async with session.post(
                        PISTON_API,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                    ) as response:
                        if response.status != 200:
                            logger.error(f"Piston API error: {await response.text()}")
                            continue
                            
                        data = await response.json()
                        output = data.get("run", {}).get("output", "").strip()
                        return output == str(test_case['output']).strip()
                        
                except Exception as e:
                    logger.error(f"Attempt {attempt + 1} failed: {str(e)}")
                    await asyncio.sleep(1)
                
        return False

    async def show_progress(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            return
            
        if not await self.check_channel_membership(update, context):
            await self._prompt_channel_join(update)
            return

        user = update.effective_user
        
        try:
            with DatabaseManager() as db:
                stats = db.conn.execute("""
                SELECT level, xp, streak, 
                       (SELECT COUNT(*) FROM user_progress 
                        WHERE user_id = ? AND completed = TRUE) as completed_challenges
                FROM users
                WHERE user_id = ?
                """, (user.id, user.id)).fetchone()
                
                if stats:
                    level, xp, streak, completed = stats
                    xp_needed = level * XP_PER_LEVEL
                    progress = min(100, (xp % xp_needed) / xp_needed * 100) if xp_needed > 0 else 0
                    
                    progress_bar = "🟩" * int(progress / 10) + "⬜" * (10 - int(progress / 10))
                    
                    await update.message.reply_text(
                        f"📊 *Your Learning Progress*\n\n"
                        f"🏅 Level: {level}\n"
                        f"✨ XP: {xp} ({progress:.1f}% to next level)\n"
                        f"{progress_bar}\n\n"
                        f"🔥 Streak: {streak} days\n"
                        f"✅ Challenges Completed: {completed}",
                        parse_mode="MarkdownV2"
                    )
                else:
                    await update.message.reply_text(
                        "No progress data found. Start with /daily to begin learning!"
                    )
                    
        except Exception as e:
            logger.error(f"Progress check failed: {str(e)}")
            await update.message.reply_text(
                "Failed to load progress data. Please try again later."
            )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data == "new_challenge":
            await self.send_daily_challenge(update, context)
        elif query.data == "progress":
            await self.show_progress(update, context)
        elif query.data == "next_challenge":
            await self.send_daily_challenge(update, context)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        logger.error("Exception while handling update:", exc_info=context.error)
        
        if update and hasattr(update, 'message'):
            await update.message.reply_text(
                "😞 An unexpected error occurred. Our engineers have been notified.\n"
                "Please try your request again later."
            )

def main():
    bot = PythonCoachBot()
    application = Application.builder().token(TOKEN).build()
    bot.application = application
    
    application.add_handler(TypeHandler(Update, bot.check_rate_limit), group=-1)
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("daily", bot.send_daily_challenge))
    application.add_handler(CommandHandler("progress", bot.show_progress))
    application.add_handler(CallbackQueryHandler(bot.handle_verify_join, pattern="^verify_join$"))
    application.add_handler(CallbackQueryHandler(bot.handle_callback))
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        bot.handle_code_submission
    ))
    application.add_error_handler(bot.error_handler)
    
    application.run_polling()

if __name__ == "__main__":
    main()