#!/usr/bin/env python3
"""
CW Farmer v5.0 - Исправленная версия
====================================

Фичи:
• Ключи и прокси в отдельных файлах (api_keys.txt, proxies.txt)
• Правильный алгоритм майнинга через token_id (25-1024)
• Корректная обработка CHALLENGE_REQUIRED
• LLM для решения challenges через OpenRouter
• Auto Public Moments (+6 trust × 5 = +30)
• Web Dashboard http://localhost:8080

Запуск:
    python cw_farmer_v5.py

Файлы:
    api_keys.txt  - API ключи (по одному на строку)
    proxies.txt   - Прокси (по одному на строку, формат: http://USER:PASS@IP:PORT)
"""

import asyncio
import json
import random
import time
import hashlib
import hmac
import os
import sys
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set
from pathlib import Path
import urllib.request
import urllib.parse
import urllib.error
import threading
import http.server
import socketserver

# ============================================
# LOGGING (tee stdout → console + file)
# ============================================

class TeeLogger:
    def __init__(self, filename: str):
        self.terminal = sys.stdout
        self.file = open(filename, "a", encoding="utf-8", buffering=1)

    def write(self, message: str):
        self.terminal.write(message)
        self.file.write(message)

    def flush(self):
        self.terminal.flush()
        self.file.flush()

    def close(self):
        self.file.close()

sys.stdout = TeeLogger("farmer.log")

# ============================================
# CONFIGURATION
# ============================================

BASE_URL = "https://work.clawplaza.ai"
API_INSCRIBE = f"{BASE_URL}/skill/inscribe"
API_BALANCE = f"{BASE_URL}/skill/cw"
API_SOCIAL = f"{BASE_URL}/skill/social"

# Trust система
TRUST_PER_MOMENT = 6
MAX_MOMENTS = 5
MOMENT_COOLDOWN_HOURS = 5
TRUST_TARGET = 65

# NFT token_id диапазон
TOKEN_ID_MIN = 25
TOKEN_ID_MAX = 1024

# LLM Config - ВСТАВЬ СВОЙ КЛЮЧ
LLM_API_KEY = "Замени на свой OpenRouter ключ"  # <-- Замени на свой OpenRouter ключ
LLM_BASE_URL = "https://openrouter.ai/api/v1"
LLM_MODEL = "openai/gpt-4o-mini"

# Файлы конфигурации
API_KEYS_FILE = "api_keys.txt"
PROXIES_FILE = "proxies.txt"
STATS_FILE = "farmer_stats.json"
STATE_FILE = "farmer_state.json"

# ============================================
# SHARED STATE (общий пул токенов между аккаунтами)
# ============================================

class SharedState:
    """Глобальное состояние: известные занятые и свободные токены.
    Asyncio — однопоточный, блокировка не нужна."""

    def __init__(self):
        self.tried_tokens: Set[int] = set()   # глобально занятые
        self.free_tokens: List[int] = []       # известные свободные (пробуем первыми)
        self.moment_states: Dict[str, dict] = {}  # account_id → {posted, last_post}

    def mark_taken(self, token_id: int):
        self.tried_tokens.add(token_id)
        if token_id in self.free_tokens:
            self.free_tokens.remove(token_id)

    def mark_free(self, token_id: int):
        if token_id not in self.tried_tokens and token_id not in self.free_tokens:
            self.free_tokens.insert(0, token_id)  # в начало — приоритет

    def get_next_token(self) -> Optional[int]:
        # Сначала из известных свободных
        if self.free_tokens:
            return self.free_tokens[0]
        # Иначе — случайный, исключая известные занятые
        available = list(set(range(TOKEN_ID_MIN, TOKEN_ID_MAX + 1)) - self.tried_tokens)
        if not available:
            return None
        return random.choice(available)

    def save_moment_state(self, account_id: str, posted: int, last_post: Optional[datetime]):
        self.moment_states[account_id] = {
            "posted": posted,
            "last_post": last_post.isoformat() if last_post else None
        }

    def get_moment_state(self, account_id: str) -> dict:
        return self.moment_states.get(account_id, {})

    def save(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({
                    "tried_tokens": list(self.tried_tokens),
                    "free_tokens": self.free_tokens,
                    "moment_states": self.moment_states,
                    "saved_at": datetime.now().isoformat()
                }, f)
        except Exception as e:
            print(f"⚠️ Could not save state: {e}")

    def load(self):
        path = Path(STATE_FILE)
        if not path.exists():
            return
        try:
            with open(path) as f:
                data = json.load(f)
            self.tried_tokens = set(data.get("tried_tokens", []))
            self.free_tokens = data.get("free_tokens", [])
            self.moment_states = data.get("moment_states", {})
            saved_at = data.get("saved_at", "unknown")
            print(f"📂 State loaded (saved {saved_at}): "
                  f"{len(self.tried_tokens)} taken, {len(self.free_tokens)} free tokens, "
                  f"{len(self.moment_states)} moment states")
        except Exception as e:
            print(f"⚠️ Could not load state: {e}")

# ============================================
# MOMENT CONTENT TEMPLATES
# ============================================

MOMENT_TEMPLATES = [
    "Just earned some CW tokens! The grind continues 🚀",
    "Working on my trust score today. Steady progress!",
    "Another day of mining. Building that portfolio!",
    "Exploring the ClawPlaza ecosystem. Great community!",
    "Trust score grinding in progress. NFT soon!",
    "CW mining update: making good progress today!",
    "Just passed another milestone! 💪",
    "Learning new strategies. Always improving!",
    "Community highlight: everyone's so supportive!",
    "Weekend mining session active. Let's go!",
    "Reflection on my mining journey. Good progress!",
    "Setting new goals for this week. Level up time!",
    "Appreciating the ClawPlaza community! 🙌",
    "Mining tip: consistency is everything!",
    "Celebrating small wins. Every CW counts!",
]

# ============================================
# DATA CLASSES
# ============================================

@dataclass
class AccountStats:
    id: str
    trust_score: int = 0
    cw_balance: int = 0
    cw_staked: int = 0
    total_mines: int = 0
    moments_posted: int = 0
    challenges_passed: int = 0
    challenges_failed: int = 0
    tokens_taken: int = 0
    status: str = "idle"
    last_moment: Optional[str] = None
    next_moment: Optional[str] = None
    start_time: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        runtime = datetime.now() - self.start_time
        return {
            "id": self.id,
            "trust_score": self.trust_score,
            "cw_balance": self.cw_balance,
            "cw_staked": self.cw_staked,
            "stake_ok": self.cw_staked >= 20000,
            "total_mines": self.total_mines,
            "moments_posted": self.moments_posted,
            "moments_remaining": MAX_MOMENTS - self.moments_posted,
            "challenges_passed": self.challenges_passed,
            "challenges_failed": self.challenges_failed,
            "tokens_taken": self.tokens_taken,
            "status": self.status,
            "runtime": str(runtime).split('.')[0],
            "target_reached": self.trust_score >= TRUST_TARGET,
            "trust_needed": max(0, TRUST_TARGET - self.trust_score)
        }

# ============================================
# HTTP CLIENT WITH PROXY
# ============================================

class HttpClient:
    """HTTP клиент с поддержкой прокси"""

    def __init__(self, api_key: str, proxy: str = None):
        self.api_key = api_key
        self.proxy = proxy
        self.headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

    def _build_opener(self):
        """Создать opener с прокси (или без)"""
        if self.proxy:
            proxy_handler = urllib.request.ProxyHandler({
                "http": self.proxy,
                "https": self.proxy
            })
            return urllib.request.build_opener(proxy_handler)
        return urllib.request.build_opener()

    def _execute(self, req: urllib.request.Request, timeout: int = 30) -> dict:
        """Выполнить запрос и вернуть JSON"""
        opener = self._build_opener()
        try:
            response = opener.open(req, timeout=timeout)
            return json.loads(response.read().decode())
        except urllib.error.HTTPError as e:
            try:
                error_body = e.read().decode()
                return json.loads(error_body) if error_body else {"error": str(e), "status": e.code}
            except:
                return {"error": str(e), "status": e.code}
        except Exception as e:
            return {"error": str(e)}

    def request(self, url: str, data: dict = None, method: str = "POST", timeout: int = 30) -> dict:
        """Выполнить HTTP запрос с X-API-Key (агентский ключ)"""
        body = json.dumps(data).encode() if data else b""
        req = urllib.request.Request(url, data=body, headers=self.headers.copy(), method=method)
        req.add_header("X-API-Key", self.api_key)
        return self._execute(req, timeout)

    def post(self, url: str, data: dict) -> dict:
        return self.request(url, data, method="POST")

    def get(self, url: str) -> dict:
        return self.request(url, method="GET")

# ============================================
# LLM CLIENT
# ============================================

class LLMClient:
    """Клиент для LLM (OpenRouter)"""

    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.request_count = 0

    @staticmethod
    def _validate_answer(prompt: str, answer: str) -> str:
        """Проверить и исправить ответ под числовые ограничения"""
        import re

        # "exactly X words"
        m = re.search(r'exactly\s+(\d+)\s+words', prompt, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            words = answer.split()
            if len(words) > n:
                answer = " ".join(words[:n])

        # "between X and Y words"
        m = re.search(r'between\s+(\d+)\s+and\s+(\d+)\s+words', prompt, re.IGNORECASE)
        if m:
            hi = int(m.group(2))
            words = answer.split()
            if len(words) > hi:
                answer = " ".join(words[:hi])

        return answer

    # Стили — каждый аккаунт получает свой по индексу
    STYLES = [
        "poetic and lyrical",
        "casual and conversational",
        "formal and academic",
        "vivid and descriptive",
        "philosophical and reflective",
        "simple and direct",
        "scientific and precise",
        "whimsical and imaginative",
        "enthusiastic and energetic",
    ]

    @staticmethod
    def _build_prompt(prompt: str, style: str = "natural"):
        """Собрать system + user промпт в зависимости от типа challenge"""
        import re

        is_paraphrase = bool(re.search(r"say this in different words", prompt, re.IGNORECASE))
        is_exact_words = bool(re.search(r"exactly\s+\d+\s+words", prompt, re.IGNORECASE))
        is_between_words = bool(re.search(r"between\s+\d+\s+and\s+\d+\s+words", prompt, re.IGNORECASE))

        style_note = f"Write in a {style} style. Your answer must be unique and unlike any other response."

        if is_paraphrase:
            system = (
                f"Rewrite the given sentence using completely different words while keeping the same meaning. "
                f"Do NOT reuse any nouns, verbs, or adjectives from the original. {style_note} "
                f"Output ONLY the rewritten sentence — no quotes, no explanation."
            )
            user = f"{prompt}\n\nYour unique rewrite (entirely different vocabulary):"

        elif is_exact_words or is_between_words:
            system = (
                f"You are solving word-count challenges. "
                f"CRITICAL: count every word in your answer BEFORE outputting it. "
                f"{style_note} Output ONLY the answer — no quotes, no labels."
            )
            user = f"{prompt}\n\nCount your words carefully. Output only the answer:"

        else:
            system = (
                f"You are solving writing challenges. Follow ALL constraints EXACTLY.\n"
                f"- Include ALL required words if mentioned.\n"
                f"- End with '?' if asked for a question.\n"
                f"- Start with the required word if specified.\n"
                f"{style_note}\n"
                f"Output ONLY the answer — no quotes, no labels, no explanation."
            )
            user = f"{prompt}\n\nYour unique answer:"

        return system, user

    def solve_challenge(self, prompt: str, style: str = "natural") -> str:
        """Решить challenge через LLM, с 1 повтором при таймауте"""
        import re

        system_prompt, user_prompt = self._build_prompt(prompt, style)

        data = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "max_tokens": 200,
            "temperature": 0.7
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://clawplaza.ai",
            "X-Title": "CW Farmer"
        }

        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    f"{self.base_url}/chat/completions",
                    data=json.dumps(data).encode(),
                    headers=headers,
                    method="POST"
                )
                response = urllib.request.urlopen(req, timeout=30)
                result = json.loads(response.read().decode())
                answer = result["choices"][0]["message"]["content"].strip()
                answer = self._validate_answer(prompt, answer)
                self.request_count += 1
                return answer
            except Exception as e:
                if attempt == 0:
                    print(f"[LLM] Error (retry): {e}")
                    time.sleep(5)
                else:
                    print(f"[LLM] Error: {e}")

        return self._fallback_answer(prompt)

    def _fallback_answer(self, prompt: str) -> str:
        """Fallback если LLM не ответил"""
        answers = [
            "The digital landscape evolves constantly, bringing new opportunities.",
            "Innovation drives progress in unexpected and exciting directions.",
            "Technology transforms our understanding of what's possible.",
            "Each step forward opens doors to new discoveries.",
            "The journey of exploration reveals hidden potentials.",
        ]
        return random.choice(answers)

# ============================================
# MOMENTS MANAGER
# ============================================

class MomentsManager:
    """Управление Public Moments"""

    def __init__(self, client: HttpClient):
        self.client = client
        self.posted = 0
        self.last_post: Optional[datetime] = None

    def can_post(self) -> tuple:
        """Проверить можно ли постить момент"""
        if self.posted >= MAX_MOMENTS:
            return False, "Max moments reached"
        if self.last_post:
            elapsed = datetime.now() - self.last_post
            if elapsed < timedelta(hours=MOMENT_COOLDOWN_HOURS):
                remaining = timedelta(hours=MOMENT_COOLDOWN_HOURS) - elapsed
                return False, f"Cooldown: {str(remaining).split('.')[0]}"
        return True, "Ready"

    def get_next_post_time(self) -> Optional[datetime]:
        """Когда можно постить следующий момент"""
        if self.posted >= MAX_MOMENTS:
            return None
        if self.last_post:
            return self.last_post + timedelta(hours=MOMENT_COOLDOWN_HOURS)
        return datetime.now()

    def generate_content(self) -> str:
        """Генерировать контент для момента"""
        content = random.choice(MOMENT_TEMPLATES)
        if random.random() > 0.5:
            content += random.choice([" 💪", " 🎯", " ⚡", " 🔥", " ✨", " 🚀"])
        return content

    def post(self) -> dict:
        """Запостить момент"""
        can, reason = self.can_post()
        if not can:
            return {"success": False, "error": reason}

        content = self.generate_content()

        result = self.client.post(API_SOCIAL, {
            "module": "moments",
            "content": content,
            "visibility": "public"
        })

        if result.get("success"):
            self.posted += 1
            self.last_post = datetime.now()
            return {
                "success": True,
                "content": content,
                "trust_earned": TRUST_PER_MOMENT,
                "moments_remaining": MAX_MOMENTS - self.posted
            }

        return result

# ============================================
# ACCOUNT FARMER
# ============================================

class AccountFarmer:
    """Фармер для одного аккаунта"""

    def __init__(self, account_id: str, api_key: str, proxy: str, llm: LLMClient, shared: "SharedState"):
        self.id = account_id
        self.client = HttpClient(api_key, proxy)
        self.llm = llm
        self.shared = shared
        self.moments = MomentsManager(self.client)
        self.stats = AccountStats(id=self.id)
        self.running = False
        # Уникальный стиль ответов — по индексу аккаунта
        idx = int(account_id.split("_")[-1]) - 1
        self.style = LLMClient.STYLES[idx % len(LLMClient.STYLES)]

    def get_next_token_id(self) -> Optional[int]:
        """Получить следующий token_id через SharedState"""
        return self.shared.get_next_token()

    async def get_balance(self) -> dict:
        """Получить баланс и trust"""
        result = self.client.post(API_BALANCE, {"action": "balance"})

        if result.get("success"):
            data = result.get("data", {})
            self.stats.trust_score = data.get("trust_score", 0)
            self.stats.cw_balance = data.get("cw_balance", 0)
            self.stats.cw_staked = data.get("cw_staked", 0)

        return result


    async def post_moment(self) -> dict:
        """Запостить момент если можно"""
        can, reason = self.moments.can_post()
        if not can:
            return {"success": False, "error": reason}

        result = self.moments.post()

        if result.get("success"):
            self.stats.moments_posted = self.moments.posted
            self.stats.last_moment = datetime.now().isoformat()
            print(f"[{self.id}] ✅ Moment posted! +{result['trust_earned']} trust ({self.moments.posted}/{MAX_MOMENTS})")
            # Сохраняем стейт моментов сразу после поста
            self.shared.save_moment_state(self.id, self.moments.posted, self.moments.last_post)

        # Обновляем время следующего момента
        next_time = self.moments.get_next_post_time()
        self.stats.next_moment = next_time.isoformat() if next_time else None

        return result

    async def mine(self) -> dict:
        """Выполнить майнинг с правильным алгоритмом"""

        # 1. Получаем свободный token_id
        token_id = self.get_next_token_id()
        if not token_id:
            print(f"[{self.id}] ❌ All tokens are taken!")
            return {"success": False, "error": "all_tokens_taken"}

        # 2. Отправляем запрос на инскрипцию
        result = self.client.post(API_INSCRIBE, {"token_id": token_id})

        # 3. Проверяем ответ

        # Случай 0: Серверная ошибка (5xx)
        if result.get("status", 0) in (500, 502, 503, 504):
            print(f"[{self.id}] 🌐 Server error {result['status']}, retrying in 60s...")
            return {"success": False, "error": "server_error", "retry_after": 60}

        # Случай 1: Токен занят
        if result.get("id_status") == "taken":
            self.shared.mark_taken(token_id)
            self.stats.tokens_taken += 1
            print(f"[{self.id}] ⚠️ Token #{token_id} taken by {result.get('taken_by', 'unknown')}, trying another...")
            return {"success": False, "error": "token_taken", "token_id": token_id}

        # Случай 2: Требуется challenge
        if result.get("error") == "CHALLENGE_REQUIRED":
            return await self.handle_challenge(token_id, result)

        # Случай 3: Успех (без challenge)
        if result.get("hash") or result.get("cw_earned"):
            self.shared.mark_free(token_id)  # остальные тоже могут его майнить
            self.stats.total_mines += 1
            self.stats.cw_balance = result.get("cw_balance", self.stats.cw_balance)
            self.stats.trust_score = result.get("trust_score", self.stats.trust_score)

            cw_earned = result.get("cw_earned", 0)
            print(f"[{self.id}] ⛏️ Mined token #{token_id}! +{cw_earned} CW | Trust: {self.stats.trust_score}/{TRUST_TARGET}")

            if result.get("nft_hit"):
                print(f"[{self.id}] 🎉🎉🎉 NFT HIT! 🎉🎉🎉")

            return {"success": True, "token_id": token_id, **result}

        # Случай 4: Rate limited
        if result.get("error") == "RATE_LIMITED":
            retry_after = result.get("retry_after", 60)
            print(f"[{self.id}] ⏳ Rate limited, waiting {retry_after}s...")
            return {"success": False, "error": "rate_limited", "retry_after": retry_after}

        # Случай 5: Неизвестная ошибка
        print(f"[{self.id}] ❌ Unknown response: {result}")
        return result

    async def handle_challenge(self, token_id: int, challenge_response: dict, depth: int = 0) -> dict:
        """Обработать CHALLENGE_REQUIRED"""

        challenge = challenge_response.get("challenge", {})
        challenge_id = challenge.get("id")
        prompt = challenge.get("prompt", "")

        if not challenge_id or not prompt:
            print(f"[{self.id}] ❌ Invalid challenge format: {challenge_response}")
            return {"success": False, "error": "invalid_challenge"}

        print(f"[{self.id}] 🧩 Challenge: {prompt}")
        print(f"[{self.id}] 📋 Challenge data: {challenge}")

        # Решаем через LLM (в executor — не блокирует event loop)
        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(None, self.llm.solve_challenge, prompt, self.style)
        # Чистим ответ: убираем кавычки, лишние переносы
        answer = answer.strip().strip('"').strip("'").strip()
        print(f"[{self.id}] 🤖 LLM answer: {answer}")

        # Отправляем ответ
        result = self.client.post(API_INSCRIBE, {
            "token_id": token_id,
            "challenge_id": challenge_id,
            "challenge_answer": answer
        })

        # Проверяем результат

        # Серверная ошибка (5xx)
        if result.get("status", 0) in (500, 502, 503, 504):
            print(f"[{self.id}] 🌐 Server error {result['status']} on challenge, retrying in 60s...")
            return {"success": False, "error": "server_error", "retry_after": 60}

        if result.get("hash") or result.get("cw_earned"):
            self.shared.mark_free(token_id)  # остальные тоже могут его майнить
            self.stats.challenges_passed += 1
            self.stats.total_mines += 1
            self.stats.cw_balance = result.get("cw_balance", self.stats.cw_balance)
            self.stats.trust_score = result.get("trust_score", self.stats.trust_score)

            cw_earned = result.get("cw_earned", 0)
            print(f"[{self.id}] ✅ Challenge passed! +{cw_earned} CW | Trust: {self.stats.trust_score}/{TRUST_TARGET}")
            return {"success": True, "challenge_cooldown": True, "token_id": token_id, **result}

        if result.get("error") == "CHALLENGE_FAILED":
            self.stats.challenges_failed += 1
            msg = result.get("message", "bad answer")
            print(f"[{self.id}] ❌ Challenge failed ({msg}) | answer: '{answer}'")
            # Сервер может вернуть новый challenge — пробуем сразу (макс 3 раза)
            new_ch = result.get("challenge")
            if new_ch and new_ch.get("id") and new_ch.get("prompt") and depth < 3:
                print(f"[{self.id}] 🔄 Server gave new challenge, trying immediately (depth={depth+1})...")
                return await self.handle_challenge(token_id, {"challenge": new_ch}, depth + 1)
            return {"success": False, "error": "challenge_failed"}

        if result.get("error") == "CHALLENGE_USED":
            print(f"[{self.id}] ⚠️ Challenge expired, getting new one...")
            return {"success": False, "error": "challenge_used"}

        print(f"[{self.id}] ❌ Unknown challenge result: {result}")
        return result

    async def run(self):
        """Main farming loop"""
        self.running = True
        self.stats.status = "farming"

        print(f"[{self.id}] 🚀 Starting farmer...")

        # Восстанавливаем состояние моментов из сохранённого стейта
        m_state = self.shared.get_moment_state(self.id)
        if m_state:
            self.moments.posted = m_state.get("posted", 0)
            last_post_str = m_state.get("last_post")
            if last_post_str:
                self.moments.last_post = datetime.fromisoformat(last_post_str)
            self.stats.moments_posted = self.moments.posted
            print(f"[{self.id}] 📅 Moments restored: {self.moments.posted}/{MAX_MOMENTS}"
                  + (f", last post {self.moments.last_post.strftime('%H:%M')}" if self.moments.last_post else ""))

        # Получаем начальный статус
        await self.get_balance()
        print(f"[{self.id}] 📊 Trust: {self.stats.trust_score}/{TRUST_TARGET} | CW: {self.stats.cw_balance:,} | Staked: {self.stats.cw_staked:,}")

        consecutive_token_failures = 0

        while self.running and self.stats.trust_score < TRUST_TARGET:
            try:
                # 1. Проверяем моменты
                if self.moments.posted < MAX_MOMENTS:
                    can, _ = self.moments.can_post()
                    if can:
                        await self.post_moment()
                        await asyncio.sleep(3)

                # 2. Майним
                result = await self.mine()

                # Обрабатываем результат
                if result.get("error") == "token_taken":
                    consecutive_token_failures += 1
                    if consecutive_token_failures > 10:
                        print(f"[{self.id}] ⚠️ Too many taken tokens, waiting...")
                        await asyncio.sleep(random.randint(60, 120))
                        consecutive_token_failures = 0

                if result.get("error") == "rate_limited":
                    retry = result.get("retry_after", 60)
                    print(f"[{self.id}] 🔒 Rate limited, sleeping {retry}s...")
                    await asyncio.sleep(retry + random.randint(10, 30))
                    continue

                if result.get("error") == "server_error":
                    retry = result.get("retry_after", 60)
                    print(f"[{self.id}] 🌐 Server unavailable, sleeping {retry}s...")
                    await asyncio.sleep(retry)
                    continue

                if result.get("success"):
                    consecutive_token_failures = 0

                # Проверяем цель
                if self.stats.trust_score >= TRUST_TARGET:
                    print(f"[{self.id}] 🎉 TARGET REACHED! Trust: {self.stats.trust_score}")
                    self.stats.status = "completed"
                    break

                # После успешного челленджа — ждём 31-32 минуты
                if result.get("challenge_cooldown"):
                    wait = random.randint(1860, 1920)
                    print(f"[{self.id}] ⏳ Challenge cooldown: sleeping {wait // 60}m {wait % 60}s...")
                    try:
                        await asyncio.sleep(wait)
                    except asyncio.CancelledError:
                        if not self.running:
                            raise
                    continue

                # Ждём перед следующим циклом
                delay = random.randint(120, 200)  # 2-3.5 минуты (более консервативно)
                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[{self.id}] ❌ Error: {e}")
                await asyncio.sleep(60)

        print(f"[{self.id}] 🏁 Farmer stopped. Final trust: {self.stats.trust_score}")

    def stop(self):
        self.running = False
        self.stats.status = "stopped"

# ============================================
# DASHBOARD SERVER
# ============================================

class DashboardServer:
    """Web Dashboard для мониторинга"""

    def __init__(self, farmers: List[AccountFarmer], port: int = 8080):
        self.farmers = farmers
        self.port = port
        self.server = None

    def get_stats(self) -> dict:
        """Получить статистику всех аккаунтов"""
        accounts = [f.stats.to_dict() for f in self.farmers]

        return {
            "accounts": accounts,
            "summary": {
                "total_accounts": len(self.farmers),
                "completed": sum(1 for f in self.farmers if f.stats.trust_score >= TRUST_TARGET),
                "total_cw": sum(f.stats.cw_balance for f in self.farmers),
                "total_staked": sum(f.stats.cw_staked for f in self.farmers),
                "total_mines": sum(f.stats.total_mines for f in self.farmers),
                "avg_trust": sum(f.stats.trust_score for f in self.farmers) / len(self.farmers) if self.farmers else 0,
                "total_moments": sum(f.stats.moments_posted for f in self.farmers),
                "total_challenges_passed": sum(f.stats.challenges_passed for f in self.farmers),
                "total_challenges_failed": sum(f.stats.challenges_failed for f in self.farmers),
                "running": sum(1 for f in self.farmers if f.running)
            },
            "last_update": datetime.now().isoformat()
        }

    def start(self):
        """Запустить сервер"""

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *args, dashboard=None, **kwargs):
                self.dashboard = dashboard
                super().__init__(*args, **kwargs)

            def do_GET(self):
                if self.path == "/" or self.path == "/index.html":
                    self.send_html()
                elif self.path == "/api/stats":
                    self.send_json()
                else:
                    self.send_error(404)

            def send_html(self):
                html = '''<!DOCTYPE html>
<html><head><title>CW Farmer Dashboard</title>
<meta charset="utf-8"><meta http-equiv="refresh" content="10">
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Monaco','Menlo',monospace; background: #0a0a0a; color: #00ff00; padding: 20px; }
h1 { text-align: center; margin-bottom: 20px; text-shadow: 0 0 10px #00ff00; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px; }
.card { background: #111; border: 1px solid #00ff00; border-radius: 8px; padding: 15px; }
.card h2 { font-size: 12px; color: #888; margin-bottom: 10px; text-transform: uppercase; }
.stat { display: flex; justify-content: space-between; padding: 5px 0; border-bottom: 1px solid #222; }
.stat-label { color: #666; }
.stat-value { font-weight: bold; }
.stat-value.good { color: #00ff00; }
.stat-value.warn { color: #ffaa00; }
.stat-value.done { color: #00ffff; }
.trust-bar { height: 8px; background: #222; border-radius: 4px; margin: 10px 0; overflow: hidden; }
.trust-fill { height: 100%; background: linear-gradient(90deg,#ff4444,#ffaa00,#00ff00); }
.summary { background: #0a0a0a; border: 2px solid #00ff00; border-radius: 8px; padding: 20px; margin-bottom: 20px; text-align: center; }
.summary h2 { color: #00ff00; margin-bottom: 15px; }
.summary-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 20px; }
.summary-item { text-align: center; }
.summary-value { font-size: 24px; font-weight: bold; color: #00ff00; }
.summary-label { font-size: 11px; color: #666; }
.update { text-align: center; color: #444; margin-top: 20px; font-size: 11px; }
</style></head><body>
<h1>🤖 CW Farmer v5.0 Dashboard</h1>
<div class="summary" id="summary">Loading...</div>
<div class="grid" id="accounts">Loading...</div>
<p class="update" id="update"></p>
<script>
async function load() {
 try {
  const res = await fetch('/api/stats');
  const data = await res.json();

  const s = data.summary;
  document.getElementById('summary').innerHTML = `
   <h2>📊 Overview</h2>
   <div class="summary-grid">
    <div class="summary-item"><div class="summary-value">${s.completed}/${s.total_accounts}</div><div class="summary-label">Completed</div></div>
    <div class="summary-item"><div class="summary-value">${s.total_cw.toLocaleString()}</div><div class="summary-label">Total CW</div></div>
    <div class="summary-item"><div class="summary-value">${s.total_staked.toLocaleString()}</div><div class="summary-label">Total Staked</div></div>
    <div class="summary-item"><div class="summary-value">${s.total_mines}</div><div class="summary-label">Total Mines</div></div>
    <div class="summary-item"><div class="summary-value">${s.avg_trust.toFixed(1)}</div><div class="summary-label">Avg Trust</div></div>
   </div>
  `;

  document.getElementById('accounts').innerHTML = data.accounts.map(a => `
   <div class="card">
    <h2>${a.id}</h2>
    <div class="stat"><span class="stat-label">Trust</span><span class="stat-value ${a.trust_score>=65?'done':'warn'}">${a.trust_score}/65</span></div>
    <div class="trust-bar"><div class="trust-fill" style="width:${Math.min(100,a.trust_score/65*100)}%"></div></div>
    <div class="stat"><span class="stat-label">CW Balance</span><span class="stat-value">${a.cw_balance.toLocaleString()}</span></div>
    <div class="stat"><span class="stat-label">Staked</span><span class="stat-value ${a.stake_ok?'done':'warn'}">${a.cw_staked.toLocaleString()} ${a.stake_ok?'✅':'⏳'}</span></div>
    <div class="stat"><span class="stat-label">Mines</span><span class="stat-value">${a.total_mines}</span></div>
    <div class="stat"><span class="stat-label">Moments</span><span class="stat-value">${a.moments_posted}/5</span></div>
    <div class="stat"><span class="stat-label">Challenges</span><span class="stat-value ${a.challenges_failed>0?'warn':'good'}">${a.challenges_passed}/${a.challenges_passed+a.challenges_failed}</span></div>
    <div class="stat"><span class="stat-label">Tokens Taken</span><span class="stat-value">${a.tokens_taken}</span></div>
    <div class="stat"><span class="stat-label">Status</span><span class="stat-value ${a.target_reached?'done':'warn'}">${a.target_reached?'✅ DONE':a.status}</span></div>
    <div class="stat"><span class="stat-label">Runtime</span><span class="stat-value">${a.runtime}</span></div>
   </div>
  `).join('');

  document.getElementById('update').textContent = 'Last update: ' + new Date().toLocaleTimeString();
 } catch(e) { console.error(e); }
}
load(); setInterval(load, 10000);
</script></body></html>'''
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(html.encode())

            def send_json(self):
                stats = self.dashboard.get_stats() if self.dashboard else {}
                data = json.dumps(stats)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data.encode())

        def create_handler(*args, **kwargs):
            return Handler(*args, dashboard=self, **kwargs)

        self.server = socketserver.TCPServer(("", self.port), create_handler)
        print(f"\n🌐 Dashboard: http://localhost:{self.port}\n")

        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

# ============================================
# CONFIG LOADERS
# ============================================

def load_api_keys(filepath: str) -> List[str]:
    """Загрузить API ключи из файла"""
    path = Path(filepath)
    if not path.exists():
        print(f"❌ File not found: {filepath}")
        return []

    keys = []
    with open(path) as f:
        for line in f:
            key = line.strip()
            if key and not key.startswith('#'):
                keys.append(key)
    return keys

def load_proxies(filepath: str) -> List[str]:
    """Загрузить прокси из файла"""
    path = Path(filepath)
    if not path.exists():
        print(f"⚠️ Proxy file not found: {filepath} (running without proxies)")
        return []

    proxies = []
    with open(path) as f:
        for line in f:
            proxy = line.strip()
            if proxy and not proxy.startswith('#'):
                proxies.append(proxy)
    return proxies

# ============================================
# MAIN CONTROLLER
# ============================================

class FarmerController:
    """Контроллер для всех фармеров"""

    def __init__(self):
        self.farmers: List[AccountFarmer] = []
        self.llm = LLMClient(LLM_API_KEY, LLM_BASE_URL, LLM_MODEL)
        self.shared = SharedState()
        self.dashboard = None

    def setup(self):
        """Инициализация фармеров"""
        print("\n" + "="*60)
        print("🚀 CW Farmer v5.0 - Starting")
        print("="*60)

        # Проверяем LLM ключ
        if LLM_API_KEY == "sk-or-v1-YOUR_KEY_HERE":
            print("\n⚠️  LLM API ключ не установлен!")
            print("   Отредактируй LLM_API_KEY в начале файла")
            print("   Получить ключ: https://openrouter.ai/keys\n")
            sys.exit(1)

        # Загружаем сохранённое состояние токенов
        self.shared.load()

        # Загружаем конфиги
        api_keys = load_api_keys(API_KEYS_FILE)
        proxies = load_proxies(PROXIES_FILE)

        if not api_keys:
            print(f"❌ No API keys found in {API_KEYS_FILE}")
            sys.exit(1)

        print(f"\n📊 API Keys loaded: {len(api_keys)}")
        print(f"📊 Proxies loaded: {len(proxies)}")
        print(f"🎯 Target: {TRUST_TARGET} trust per account")
        print(f"🤖 LLM: {LLM_MODEL}")

        # Создаём фармеров
        for i, api_key in enumerate(api_keys):
            account_id = f"acc_{i+1:02d}"
            proxy = proxies[i] if i < len(proxies) else None
            farmer = AccountFarmer(account_id, api_key, proxy, self.llm, self.shared)
            self.farmers.append(farmer)

        print(f"\n📋 Farmers created: {len(self.farmers)}")

        print("\n📅 Trust Farming Schedule (per account):")
        print("  Start: ~20 trust (avatar)")
        print(f"  Moments (5x +{TRUST_PER_MOMENT}): +30 trust (~20 hours)")
        print("  Mining (75x): +15 trust (~2 days)")
        print("  ─────────────────────────────")
        print(f"  TOTAL: {TRUST_TARGET} trust → NFT eligible!")

        # Запускаем дашборд
        self.dashboard = DashboardServer(self.farmers, 8080)
        self.dashboard.start()

    async def run(self):
        """Запустить всех фармеров"""

        async def staggered_start(farmer: AccountFarmer, delay: int):
            if delay > 0:
                print(f"[{farmer.id}] ⏱️ Starting in {delay}s...")
                await asyncio.sleep(delay)
            await farmer.run()

        tasks = [staggered_start(farmer, i * 20) for i, farmer in enumerate(self.farmers)]

        # Периодическое сохранение статистики
        async def save_loop():
            while any(f.running for f in self.farmers):
                self.save_stats()
                await asyncio.sleep(30)

        await asyncio.gather(*tasks, save_loop())

        # Финальный отчёт
        self.print_report()

    def save_stats(self):
        """Сохранить статистику и состояние в файлы"""
        stats = self.dashboard.get_stats() if self.dashboard else {}
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, indent=2)
        self.shared.save()

    def print_report(self):
        """Вывести финальный отчёт"""
        print("\n" + "="*60)
        print("📊 FINAL REPORT")
        print("="*60)

        for farmer in self.farmers:
            status = "✅ DONE" if farmer.stats.trust_score >= TRUST_TARGET else "⏳ In progress"
            print(f"  {farmer.id}: Trust {farmer.stats.trust_score}/{TRUST_TARGET} | CW {farmer.stats.cw_balance:,} | {status}")

        completed = sum(1 for f in self.farmers if f.stats.trust_score >= TRUST_TARGET)
        total_cw = sum(f.stats.cw_balance for f in self.farmers)
        avg_trust = sum(f.stats.trust_score for f in self.farmers) / len(self.farmers)

        print(f"\nSummary:")
        print(f"  Completed: {completed}/{len(self.farmers)}")
        print(f"  Total CW: {total_cw:,}")
        print(f"  Avg Trust: {avg_trust:.1f}")
        print("="*60)

    def stop_all(self):
        """Остановить всех фармеров"""
        for farmer in self.farmers:
            farmer.stop()

# ============================================
# MAIN
# ============================================

async def main():
    controller = FarmerController()
    controller.setup()

    try:
        await controller.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n\n🛑 Stopping all farmers...")
        controller.stop_all()
        controller.save_stats()
        controller.print_report()

if __name__ == "__main__":
    print("""
╔════════════════════════════════════════════════════════════╗
║                CW Farmer v5.0                              ║
║                                                            ║
║  Multi-account | Auto Moments | Mining + Challenges        ║
║                                                            ║
║  Files: api_keys.txt, proxies.txt                          ║
║  Dashboard: http://localhost:8080                          ║
║                                                            ║
║  Press Ctrl+C to stop                                      ║
╚════════════════════════════════════════════════════════════╝
""")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
