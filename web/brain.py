# -*- coding: utf-8 -*-
"""
J.A.R.V.I.S. Web Brain — self-contained command + AI logic for the web app.
Mirrors the desktop assistant, but returns JSON ({replies, actions}) instead
of speaking aloud. Speech happens in the browser (Web Speech API).
Pure standard library + the environment. Zero required pip packages.
"""

import datetime
from contextvars import ContextVar
import json
import math
import os
import random
import re
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE = os.path.dirname(os.path.abspath(__file__))
MEM = os.environ.get("JARVIS_MEMORY_DIR") or os.path.join(BASE, "memory")
os.makedirs(MEM, exist_ok=True)
NOTES_F = os.path.join(MEM, "notes.json")
REMS_F = os.path.join(MEM, "reminders.json")
PREFS_F = os.path.join(MEM, "prefs.json")
LT_F = os.path.join(MEM, "longterm.json")        # RAG: long-term memory store
# Browser-style UA: some providers sit behind Cloudflare and block script-looking agents.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

def _read_prefs():
    try:
        with open(PREFS_F, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_prefs = _read_prefs()
USER_NAME = _prefs.get("name") or os.environ.get("USER_NAME") or "Phavi"
CITY = os.environ.get("CITY", "Chennai")

_lock = threading.RLock()
_request = ContextVar("request_metadata", default=None)


def _session():
    return (_request.get() or {}).get("session_id", "default")


def _now():
    offset = (_request.get() or {}).get("timezone_offset", 0)
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(minutes=-offset)))


def _mark(provider, cache=False):
    metadata = _request.get()
    if metadata is not None:
        metadata.update(provider=provider, cache=cache)

# ============================================================================
#  HTTP helpers
# ============================================================================

def http_get(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def http_get_json(url, headers=None, timeout=12):
    return json.loads(http_get(url, headers, timeout).decode("utf-8", "replace"))


def http_post_json(url, payload, headers=None, timeout=25):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ============================================================================
#  AI brain (env-configured: JARVIS_API_KEY / JARVIS_LLM_PROVIDER)
# ============================================================================

SYSTEM_PERSONA = (
    "You are JARVIS, {user}'s private command assistant. Be composed, precise, "
    "warm, and quietly confident. Put the useful answer first. Use context from "
    "the conversation, but never invent facts, live data, actions, or device access. "
    "When uncertain, say so plainly and suggest the fastest next step. Distinguish "
    "between something you completed and something the user must confirm. Replies "
    "are usually spoken aloud: use natural plain text, no markdown, no emojis, and "
    "prefer 1-4 concise sentences unless detail is explicitly requested. Address "
    "the user by name only when it feels natural; do not use repetitive catchphrases."
)

PROVIDER_DEFAULTS = {
    "gemini": {"model": "gemini-3.8-flash",
               "base_url": "https://generativelanguage.googleapis.com/v1beta"},
    "groq":   {"model": "openai/gpt-oss-120b",
               "base_url": "https://api.groq.com/openai/v1"},
    "openai": {"model": "gpt-4o-mini", "base_url": "https://api.openai.com/v1"},
    "ollama": {"model": "llama3.1", "base_url": "http://localhost:11434/v1"},
}
ENV_KEY_NAMES = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}
PROVIDER_ORDER = ("gemini", "groq", "openai", "ollama")


class AIBrain:
    """Multi-provider AI brain with AUTOMATIC FAILOVER.

    Chain =  primary provider (JARVIS_LLM_PROVIDER + JARVIS_API_KEY)
          +  every other provider that has a key (GEMINI_/GROQ_/OPENAI_API_KEY)
          +  local ollama when OLLAMA_HOST is set.
    If one brain goes down, the next answers — no downtime, no dumb JARVIS.
    """

    def __init__(self):
        self.primary = (os.environ.get("JARVIS_LLM_PROVIDER") or "gemini").lower()
        self.model_override = os.environ.get("JARVIS_MODEL", "")
        self.histories = {}
        self.history_lock = threading.Lock()
        self.last_used = None
        self.last_error = None
        self.tokens_est = 0          # ~tokens spent (chars/4) — efficiency meter
        self.tokens_saved = 0        # ~tokens NOT spent thanks to the answer cache
        self.cache_hits = 0
        self.last_cache_hit = False
        self.chain = []
        self._add(self.primary, os.environ.get("JARVIS_API_KEY") or os.environ.get(ENV_KEY_NAMES.get(self.primary, ""), ""))
        for name in PROVIDER_ORDER:
            if name == self.primary:
                continue
            key = os.environ.get(ENV_KEY_NAMES.get(name, ""), "")
            if key:
                self._add(name, key)
            elif name == "ollama" and os.environ.get("OLLAMA_HOST"):
                self._add(name, "")

    def _add(self, name, key):
        d = PROVIDER_DEFAULTS.get(name)
        if not d:
            return
        model, base = d["model"], d["base_url"]
        if name == self.primary:
            model = self.model_override or model
            base = (os.environ.get("JARVIS_BASE_URL") or base).rstrip("/")
        self.chain.append({"name": name, "model": model,
                           "base_url": base, "key": key,
                           "dead_until": 0.0, "fails": 0})

    @staticmethod
    def _live(e):
        # circuit breaker: providers cool off after failures and auto "half-open" retry later
        return (e["key"] or e["name"] == "ollama") and time.time() >= e.get("dead_until", 0)

    @property
    def available(self):
        return any(self._live(e) for e in self.chain)

    def status(self):
        live = [e["name"] for e in self.chain if self._live(e)]
        return "+".join(live) if live else "offline"

    def answer(self, question, session_id="default", augment="", cacheable=True):
        """Answer with: answer-cache → provider chain (with cooldowns) → None.
        augment: short RAG context injected ONLY into the outbound prompt (not history).
        cacheable: allow exact-context reuse (skip memory-augmented personal asks)."""
        self.last_cache_hit = False
        session_id = (session_id or "default")[:100]
        with self.history_lock:
            history = list(self.histories.get(session_id, []))
        # Exact context prevents cross-user and stale follow-up cache hits.
        cache_key = json.dumps([session_id, USER_NAME,
                               [(e["name"], e["model"], e["base_url"]) for e in self.chain],
                               history, question], sort_keys=True, ensure_ascii=False)
        if cacheable:
            hit = _answer_cache_get(cache_key)
            if hit is not None:
                self.last_cache_hit = True
                self.cache_hits += 1
                self.tokens_saved += (len(question) + len(hit) + 400) // 4
                _mark("cache", True)
                return hit
        if not self.available:
            return None
        history.append({"role": "user", "content": question})
        history = history[-20:]
        # outbound payload: RAG augment rides on the last user message; older turns
        # are compacted to 200 chars/token efficiency without losing their gist.
        out = [dict(m) for m in history]
        for m in out[:-1]:
            if len(m["content"]) > 200:
                m["content"] = m["content"][:200].rstrip() + "…"
        if augment:
            out[-1]["content"] = out[-1]["content"] + augment
        for attempt in (1, 2):
            for e in self.chain:
                if not self._live(e):
                    continue
                try:
                    reply = (self._ask_gemini(e, out) if e["name"] == "gemini"
                             else self._ask_openai_style(e, out))
                    if reply:
                        if e["name"] != self.primary:
                            print(f"[ai] FAILOVER -> answered by {e['name']}")
                        _mark(e["name"])
                        self.last_used = e["name"]
                        self.last_error = None
                        e["fails"] = 0
                        e["dead_until"] = 0.0
                        self.tokens_est += (sum(len(m["content"]) for m in out) + len(reply)) // 4
                        if cacheable:
                            _answer_cache_put(cache_key, reply)
                        return reply
                except urllib.error.HTTPError as err:
                    print(f"[ai] {e['name']}: HTTP {err.code} (attempt {attempt})")
                    self.last_error = f"{e['name']} returned HTTP {err.code}"
                    if err.code in (400, 401, 403, 404):
                        e["fails"] += 1
                        e["dead_until"] = time.time() + min(900, 120 * e["fails"])
                    elif err.code in (408, 409, 425, 429, 500, 502, 503, 504):
                        e["dead_until"] = time.time() + 45
                except Exception as ex:
                    print(f"[ai] {e['name']} (attempt {attempt}): {ex}")
                    self.last_error = f"{e['name']} is temporarily unavailable"
                    e["dead_until"] = time.time() + 30
            time.sleep(1.2)
        return None

    def _ask_gemini(self, e, history):
        convo = "\n".join(("User: " if m["role"] == "user" else "JARVIS: ") + m["content"]
                          for m in history)
        url = f"{e['base_url']}/models/{e['model']}:generateContent?key={e['key']}"
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PERSONA.format(user=USER_NAME)}]},
            "contents": [{"role": "user", "parts": [{"text": convo}]}],
            "generationConfig": {"temperature": 0.35, "maxOutputTokens": 600},
        }
        data = http_post_json(url, payload)
        return (data["candidates"][0]["content"]["parts"][0]["text"] or "").strip()

    def _ask_openai_style(self, e, history):
        headers = {"Authorization": f"Bearer {e['key']}"} if e["key"] else {}
        payload = {"model": e["model"],
                   "messages": [{"role": "system",
                                 "content": SYSTEM_PERSONA.format(user=USER_NAME)}] + history,
                   "temperature": 0.35, "max_tokens": 500}
        data = http_post_json(f"{e['base_url']}/chat/completions", payload, headers=headers)
        return (data["choices"][0]["message"]["content"] or "").strip()


AI = AIBrain()

# ============================================================================
#  Info services (free, no key)
# ============================================================================

def wikipedia_summary(topic, depth=0):
    topic = topic.strip()
    if not topic or depth >= 2:
        return None
    title = urllib.parse.quote(topic.replace(" ", "_"))
    try:
        data = http_get_json(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}")
        if data.get("extract"):
            return data["extract"]
    except Exception:
        pass
    try:
        q = urllib.parse.urlencode({"action": "opensearch", "search": topic,
                                    "limit": 1, "namespace": 0, "format": "json"})
        res = http_get_json(f"https://en.wikipedia.org/w/api.php?{q}")
        if len(res) > 1 and res[1]:
            return wikipedia_summary(res[1][0], depth + 1)
    except Exception:
        pass
    return None


def duckduckgo_answer(query):
    try:
        q = urllib.parse.urlencode({"q": query, "format": "json", "no_html": 1,
                                    "skip_disambig": 1})
        return http_get_json(f"https://api.duckduckgo.com/?{q}").get("AbstractText") or None
    except Exception:
        return None


def get_weather(city):
    try:
        data = http_get_json(f"https://wttr.in/{urllib.parse.quote(city)}?format=j1")
        cur = data["current_condition"][0]
        desc = cur["weatherDesc"][0]["value"]
        area = data.get("nearest_area", [{}])
        place = area[0].get("areaName", [{}])[0].get("value", "") if area else ""
        return (f"Currently in {place or city}: {desc}, {cur['temp_C']}°C "
                f"(feels like {cur['FeelsLikeC']}°C), humidity {cur['humidity']}%, "
                f"wind {cur['windspeedKmph']} km/h.")
    except Exception as e:
        return f"I couldn't fetch the weather right now ({e})."


def get_top_news(country="IN", n=5):
    try:
        url = f"https://news.google.com/rss?hl=en-{country}&gl={country}&ceid={country}:en"
        root = ET.fromstring(http_get(url))
        items = root.findall(".//item/title")[:n]
        return [re.sub(r"\s+-\s+[^-]+$", "", i.text or "") for i in items] or None
    except Exception:
        return None


# ============================================================================
#  SENTINEL services — translate · define · convert · prices · market · utils
#  (free endpoints, no API key, micro-cached so repeat asks don't re-hit the wire)
# ============================================================================

_QUICK_CACHE = {}


def _quick(bucket, ttl, fetch):
    item = _QUICK_CACHE.get(bucket)
    if item and time.time() - item[0] < ttl:
        return item[1]
    try:
        data = fetch()
    except Exception:
        return None
    _QUICK_CACHE[bucket] = (time.time(), data)
    return data


LANGS = {"tamil": "ta", "hindi": "hi", "telugu": "te", "malayalam": "ml", "kannada": "kn",
         "bengali": "bn", "urdu": "ur", "marathi": "mr", "gujarati": "gu", "punjabi": "pa",
         "english": "en", "french": "fr", "german": "de", "spanish": "es", "italian": "it",
         "portuguese": "pt", "dutch": "nl", "russian": "ru", "arabic": "ar", "turkish": "tr",
         "japanese": "ja", "korean": "ko", "chinese": "zh-CN", "thai": "th", "vietnamese": "vi"}


def translate_text(text, lang):
    dest = LANGS.get((lang or "").lower().strip(), (lang or "en").strip())
    try:
        q = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": dest,
                                    "dt": "t", "q": text})
        data = json.loads(http_get(f"https://translate.googleapis.com/translate_a/single?{q}"))
        out = "".join(seg[0] for seg in data[0] if seg and seg[0])
        return out or None
    except Exception:
        return None


def dictionary_define(word):
    try:
        data = http_get_json(
            f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}")
        entry = data[0]
        lines = []
        for meaning in entry.get("meanings", [])[:2]:
            pos = meaning.get("partOfSpeech", "")
            for d in meaning.get("definitions", [])[:2]:
                defn = (d.get("definition") or "").strip()
                if defn:
                    lines.append(f"[{pos}] {defn}")
        if lines:
            return (entry.get("word", word).capitalize(), lines)
    except Exception:
        pass
    try:                                 # fallback: DuckDuckGo's definition layer
        ans = duckduckgo_answer(f"{word} meaning")
        if ans:
            return (word.capitalize(), [ans[:240]])
    except Exception:
        pass
    return None


_UNIT_BASES = {
    "len": {"km": 1000, "kilometer": 1000, "kilometers": 1000, "m": 1, "meter": 1, "meters": 1,
            "cm": .01, "mm": .001, "mile": 1609.34, "miles": 1609.34, "feet": .3048,
            "foot": .3048, "ft": .3048, "inch": .0254, "inches": .0254, "yard": .9144, "yards": .9144},
    "mass": {"kg": 1000, "g": 1, "gram": 1, "grams": 1, "lb": 453.592, "lbs": 453.592,
             "pound": 453.592, "pounds": 453.592, "oz": 28.3495, "ounce": 28.3495,
             "ounces": 28.3495, "stone": 6350.29, "tonne": 1e6, "tonnes": 1e6},
    "vol": {"l": 1, "liter": 1, "liters": 1, "litre": 1, "litres": 1, "ml": .001,
            "gallon": 3.78541, "gallons": 3.78541, "pint": .473176, "pints": .473176,
            "cup": .24, "cups": .24},
    "data": {"gb": 1024, "mb": 1, "kb": .0009765625, "tb": 1048576},
}


def convert_units(amount, frm, to):
    frm, to = frm.lower(), to.lower()
    if frm in ("c", "celsius", "f", "fahrenheit", "farenheit", "kelvin") or \
       to in ("c", "celsius", "f", "fahrenheit", "farenheit", "kelvin"):
        c = amount if frm in ("c", "celsius") else (amount - 32) * 5 / 9 if frm not in ("kelvin",) \
            else amount - 273.15
        if to in ("c", "celsius"):
            return f"{round(c, 2)}°C"
        if to in ("f", "fahrenheit", "farenheit"):
            return f"{round(c * 9 / 5 + 32, 2)}°F"
        return f"{round(c + 273.15, 2)} K"
    for table in _UNIT_BASES.values():
        if frm in table and to in table:
            base = amount * table[frm]
            out = base / table[to]
            return f"{round(out, 4):g} {to}"
    return None


CURRENCIES = {"usd", "inr", "eur", "gbp", "jpy", "sar", "aed", "aud", "cad", "cny",
              "sgd", "chf", "krw", "myr", "thb", "vnd", "brl", "zar", "nok", "sek"}


def currency_convert(amount, frm, to):
    frm, to = frm.lower(), to.lower()
    if frm not in CURRENCIES or to not in CURRENCIES:
        return None

    def _fetch():
        data = http_get_json(f"https://open.er-api.com/v6/latest/{frm.upper()}", timeout=10)
        return data["rates"].get(to.upper())
    rate = _quick(f"rates:{frm}", 6 * 3600, _fetch)
    if not rate:
        return None
    return f"{amount:g} {frm.upper()} ≈ {round(amount * rate, 2)} {to.upper()} (rate {rate:g})"


COINS = {"bitcoin": "bitcoin", "btc": "bitcoin", "ethereum": "ethereum", "eth": "ethereum",
         "dogecoin": "dogecoin", "doge": "dogecoin", "solana": "solana", "sol": "solana",
         "cardano": "cardano", "ada": "cardano", "ripple": "ripple", "xrp": "ripple",
         "litecoin": "litecoin", "ltc": "litecoin", "binance": "binancecoin", "bnb": "binancecoin"}


def crypto_price(coin):
    cid = COINS.get((coin or "").lower(), "bitcoin")

    def _fetch():
        u = (f"https://api.coingecko.com/api/v3/simple/price?ids={cid}"
             "&vs_currencies=usd%2Cinr&include_24hr_change=true")
        return http_get_json(u, timeout=10)[cid]
    data = _quick(f"crypto:{cid}", 120, _fetch)
    if not data:
        return None
    chg = data.get("usd_24h_change") or 0
    arrow = "▲" if chg >= 0 else "▼"
    return (f"{cid.capitalize()}: ${data['usd']:,.0f} (₹{data['inr']:,.0f}) "
            f"{arrow} {abs(chg):.1f}% in 24h")


def shorten_url(url):
    if not re.match(r"https?://", url):
        url = "http://" + url
    try:
        return http_get("https://tinyurl.com/api-create.php?url=" +
                        urllib.parse.quote(url, safe="")).decode().strip()
    except Exception:
        return None


def my_public_ip():
    try:
        return http_get("https://api.ipify.org", timeout=8).decode().strip()
    except Exception:
        return None


def ip_lookup(ip):
    try:
        d = http_get_json(f"https://ipwho.is/{urllib.parse.quote(ip)}", timeout=8)
        if not d.get("success", True):
            return None
        return (f"{d.get('ip', ip)} → {d.get('city', '?')}, {d.get('region', '?')}, "
                f"{d.get('country', '?')} · ISP: {d.get('connection', {}).get('isp', '?')}")
    except Exception:
        return None


def get_forecast(city):
    try:
        data = http_get_json(f"https://wttr.in/{urllib.parse.quote(city)}?format=j1")
        days = data.get("weather", [])[1:4]
        if not days:
            return None
        lines = [f"Forecast for {(city or CITY).title()}:"]
        for d in days:
            noon = (d.get("hourly") or [{}])[4]
            desc = (noon.get("weatherDesc") or [{}])[0].get("value", "")
            lines.append(f"{d['date'][5:]}: {d['mintempC']}–{d['maxtempC']}°C, {desc.lower()}")
        return "\n".join(lines)
    except Exception:
        return None


QUOTES = [
    "The best time to plant a tree was twenty years ago. The second best time is now.",
    "Discipline is choosing what you want most over what you want now.",
    "It always seems impossible until it's done. — Nelson Mandela",
    "Whether you think you can or you can't, you're right. — Henry Ford",
    "Success is not final, failure is not fatal: it is the courage to continue that counts.",
    "Don't watch the clock; do what it does. Keep going. — Sam Levenson",
    "Simplicity is the ultimate sophistication. — Leonardo da Vinci",
    "The only way to do great work is to love what you do. — Steve Jobs",
]


def summarize_text(text):
    if AI.available:
        reply = AI.answer("Summarize this in two short sentences:\n\n" + text[:2000])
        if reply:
            return _clip(reply, 320)
    # extractive fallback: sentence scoring by term-frequency overlap
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.strip()) > 20]
    if len(sents) <= 2:
        return _clip(text, 300)
    freq = {}
    for s in sents:
        for w in set(_tok(s)):
            freq[w] = freq.get(w, 0) + 1
    scored = sorted(((sum(freq.get(w, 0) for w in _tok(s)), i) for i, s in enumerate(sents)),
                    reverse=True)[:2]
    keep = " ".join(sents[i] for _, i in sorted(scored, key=lambda x: x[1]))
    return _clip(keep, 320)


# ============================================================================
#  COMMS: WhatsApp deep links + Gmail reading (stdlib only)
# ============================================================================

def wa_link(number_raw, msg):
    """Build a wa.me deep link → opens WhatsApp with the message pre-filled."""
    num = re.sub(r"\D", "", number_raw)
    if num.startswith("00"):
        num = num[2:]
    if num.startswith("0") and len(num) >= 10:
        num = "91" + num[1:]
    elif len(num) == 10:
        num = "91" + num
    return "https://wa.me/" + num + "?text=" + urllib.parse.quote(msg)


def read_emails(n=5):
    """Latest n inbox headers via Gmail IMAP. Needs EMAIL_USER + EMAIL_APP_PASSWORD env vars."""
    user = os.environ.get("EMAIL_USER", "")
    pwd = os.environ.get("EMAIL_APP_PASSWORD", "").replace(" ", "")
    if not (user and pwd):
        return None
    import email as _email
    import imaplib
    from email.header import decode_header

    def _dec(h):
        s = ""
        for part, enc in decode_header(str(h or "")):
            s += part.decode(enc or "utf-8", "replace") if isinstance(part, bytes) else part
        return s.strip()

    M = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    try:
        M.login(user, pwd)
        M.select("INBOX")
        typ, data = M.search(None, "ALL")
        ids = data[0].split()[-n:][::-1]
        out = []
        for i in ids:
            typ, md = M.fetch(i, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            if not md or not md[0]:
                continue
            msg = _email.message_from_bytes(md[0][1])
            frm = re.sub(r"<[^>]+>", "", _dec(msg.get("From", "?"))).strip().strip('"') or "unknown"
            out.append({"from": frm, "subject": _dec(msg.get("Subject", "")) or "(no subject)"})
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def safe_calculate(expr):
    import ast
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
               ast.USub, ast.UAdd, ast.FloorDiv, ast.Load)
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError("bad expression")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ValueError("bad constant")
    return eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, {})


# ============================================================================
#  Data
# ============================================================================

WEBSITES = {
    "google": "https://www.google.com", "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com", "mail": "https://mail.google.com",
    "github": "https://github.com", "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "whatsapp": "https://web.whatsapp.com", "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com", "twitter": "https://x.com", "x": "https://x.com",
    "linkedin": "https://www.linkedin.com", "reddit": "https://www.reddit.com",
    "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.in",
    "flipkart": "https://www.flipkart.com", "spotify": "https://open.spotify.com",
    "maps": "https://maps.google.com", "google maps": "https://maps.google.com",
    "drive": "https://drive.google.com", "calendar": "https://calendar.google.com",
    "news": "https://news.google.com", "translate": "https://translate.google.com",
    "chatgpt": "https://chat.openai.com", "wikipedia": "https://www.wikipedia.org",
    "hotstar": "https://www.hotstar.com", "prime video": "https://www.primevideo.com",
    "zomato": "https://www.zomato.com", "swiggy": "https://www.swiggy.com",
    "irctc": "https://www.irctc.co.in",
    "youtube music": "https://music.youtube.com", "telegram": "https://web.telegram.org",
    "jio cinema": "https://www.jiocinema.com", "jiocinema": "https://www.jiocinema.com",
    "canva": "https://www.canva.com", "figma": "https://www.figma.com",
    "keep": "https://keep.google.com", "notes": "https://keep.google.com",
}

JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I told my computer I needed a break... now it won't stop sending me Kit-Kat ads.",
    "Why did the developer go broke? He used up all his cache.",
    "There are only 10 types of people: those who understand binary and those who don't.",
    "I would tell you a UDP joke, but you might not get it.",
    "Why don't scientists trust atoms? Because they make up everything.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Artificial intelligence will never beat natural stupidity. Present company excluded, of course.",
]

DIALOGUES = [
    "'I AM WAITING.' …Thuppakki-level patience, Thalaiva. Deploy it when needed.",
    "'Bloody sweet!' — Leo mode, activated.",
    "'Oru vaatti mudivu pannitten na… en pecha naane kekkamaaten.' — Master rules.",
    "'Naa ready than varava?' — always ready, just like me.",
    "'Kutty story-ah? Listen carefully: once a man pressed one button… and JARVIS was born. Mass.'",
    "'En Thommey nanna irukkum sir… because now I am working for YOU.'",
]

HELP_TEXT = (
    "⚡ MY CAPABILITIES\n"
    "info: weather [in X] • 3-day forecast • news • define serendipity • who is/what is • search X\n"
    "translate: translate hello to tamil • convert 10 km to miles • 100 usd to inr\n"
    "money: price of bitcoin • nifty / sensex\n"
    "memory: remember that X • recall X • forget X • my memories (I remember forever!)\n"
    "web: open youtube • play <song> • shorten <url> • qr for <text> • my ip\n"
    "productivity: remind me to X in 10 min • note X • summarize <long text> • repeat\n"
    "fun: joke • flip a coin • roll a dice • motivate me • thalapathy punch\n"
    "…and ask me anything — I answer with AI when a key is configured."
)

# ============================================================================
#  Small persistence helpers
# ============================================================================

def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    with _lock:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, path)             # atomic: a kill mid-write can't corrupt memory
        except Exception as e:
            print(f"[mem] {e}")
            raise


def _yahoo_quote(symbol):
    try:
        u = ("https://query1.finance.yahoo.com/v8/finance/chart/" +
             urllib.parse.quote(symbol) + "?range=1d&interval=1d")
        meta = http_get_json(u, timeout=10)["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
        if price and prev:
            return price, (price - prev) / prev * 100
    except Exception:
        pass
    return None


def market_snapshot():
    out = []
    for sym, name in (("^NSEI", "NIFTY 50"), ("^BSESN", "SENSEX"), ("INR=X", "USD\u2192INR")):
        q = _quick(f"mkt:{sym}", 300, lambda s=sym: _yahoo_quote(s))
        if q:
            out.append(f"{name}: {q[0]:,.2f} ({'+' if q[1] >= 0 else ''}{q[1]:.2f}%)")
    return out or None


# ============================================================================
#  SENTINEL: answer cache + RAG memory retrieval (zero-cost, stdlib-only)
# ============================================================================

_CACHE = {}                        # session + exact context → (timestamp, reply)
_CACHE_TTL = 25 * 60               # spend once, reuse for 25 minutes
_CACHE_MAX = 200


def _norm_q(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _answer_cache_get(question):
    item = _CACHE.get(question)
    if not item:
        return None
    ts, reply = item
    if time.time() - ts > _CACHE_TTL:
        _CACHE.pop(question, None)
        return None
    return reply


def _answer_cache_put(question, reply):
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)))          # evict oldest entries first
    _CACHE[question] = (time.time(), reply)


_STOP = set("""a an the is are was were am be been being i me my we our you your he she it his her its
they them their this that these those what who whom which when where why how for to of in on at by with
about into over under again further then once here there all any both each few more most other some such
no nor not only own same so than too very can will just don should now do does did and or but if as at
have has hadn""".split())


def _tok(text):
    return [w for w in re.findall(r"[a-z0-9']+", (text or "").lower())
            if w not in _STOP and len(w) > 1]


def _tfidf_search(query, docs, k=3, min_score=0.10):
    """TF-IDF cosine retrieval over an in-memory corpus. Corpus is tiny (< ~500 mems
    + notes) so per-query IDF computation is effectively free."""
    if not docs:
        return []
    corp = [_tok(d) for d in docs] + [_tok(query)]
    df = {}
    for doc in corp:
        for w in set(doc):
            df[w] = df.get(w, 0) + 1
    n = len(corp)

    def vec(tokens):
        if not tokens:
            return {}
        tf = {}
        for w in tokens:
            tf[w] = tf.get(w, 0) + 1
        return {w: (c / len(tokens)) * (math.log(n / (1 + df[w])) + 1)
                for w, c in tf.items()}

    qv = vec(corp[-1])
    qn = math.sqrt(sum(v * v for v in qv.values())) or 1.0
    scored = []
    for i, tokens in enumerate(corp[:-1]):
        dv = vec(tokens)
        dot = sum(qv[w] * dw for w, dw in dv.items() if w in qv)
        score = dot / ((math.sqrt(sum(v * v for v in dv.values())) or 1.0) * qn)
        if score >= min_score:
            scored.append((round(score, 3), i))
    scored.sort(reverse=True)
    return scored[:k]


# ---- long-term memory (the RAG store) ----

def memory_all():
    # Pre-2.2 records had no session marker. The owner still needs those facts.
    return [m for m in _load(LT_F, [])
            if m.get("session_id") in (None, _session())]


def memory_add(fact):
    fact = fact.strip().rstrip(".")
    if len(fact) < 3:
        return False
    low = fact.lower()
    with _lock:
        mems = _load(LT_F, [])
        for m in memory_all():
            if low == m["text"].lower():
                return None
        mems.append({"text": fact, "session_id": _session(),
                     "time": _now().isoformat(timespec="seconds")})
        _save(LT_F, mems[-500:])
    return True


def memory_forget(query):
    with _lock:
        owned = memory_all()
        hits = _tfidf_search(query.strip(), [m["text"] for m in owned], k=1, min_score=0.15)
        if not hits:
            return None
        gone = owned[hits[0][1]]
        mems = _load(LT_F, [])
        mems.remove(gone)
        _save(LT_F, mems)
        return gone["text"]


def memory_search(query, k=3):
    """Relevant memories + recent notes, best first: [(score, text)]."""
    docs = [m["text"] for m in memory_all()]
    docs += [nt["text"] for nt in _load(NOTES_F, [])
             if nt.get("session_id") in (None, _session())][-30:]
    return [(score, docs[i]) for score, i in _tfidf_search(query, docs, k=k)]


_RAG_SKIP = re.compile(r"\b(remember|forget|recall|memori[sz]e|memories)\b", re.I)


def _rag_augment(question, budget=380):
    """Top-3 user facts injected under a hard char budget — RAG without the price."""
    if not AI.available or _RAG_SKIP.search(question) or len(_tok(question)) < 2:
        return ""
    hits = memory_search(question, k=3)
    if not hits or hits[0][0] < 0.14:
        return ""
    facts, used = [], 0
    for score, text in hits:
        if score < 0.12:
            continue
        frag = text[:140]
        if used + len(frag) > budget:
            break
        facts.append(frag)
        used += len(frag)
    return ("\nUser facts you may rely on (answer personally and briefly): "
            + " | ".join(facts)) if facts else ""


def pending_events(session_id):
    with _lock:
        return [{"id": r["id"], "text": "Reminder: " + r["task"]}
                for r in _load(REMS_F, [])
                if r.get("session_id") == session_id and r.get("when", 0) <= time.time()
                and r.get("id")]


def acknowledge_events(session_id, event_ids):
    with _lock:
        rems = _load(REMS_F, [])
        keep = [r for r in rems if not (r.get("session_id") == session_id
                and r.get("id") in event_ids and r.get("when", 0) <= time.time())]
        if len(keep) != len(rems):
            _save(REMS_F, keep)


def _schedule(task, when_epoch):
    with _lock:
        rems = _load(REMS_F, [])
        rems.append({"id": str(uuid.uuid4()), "task": task, "when": when_epoch,
                     "session_id": _session()})
        _save(REMS_F, rems)


# ============================================================================
#  MAIN COMMAND HANDLER
# ============================================================================

def handle(text, session_id="default", timezone_offset=0):
    """Record all command turns and keep telemetry local to this request."""
    token = _request.set({"provider": "local", "cache": False,
                          "session_id": session_id, "timezone_offset": timezone_offset})
    try:
        result = _handle(text, session_id)
        metadata = _request.get()
        result["provider"] = metadata["provider"]
        result.setdefault("stats", {})["cache"] = metadata["cache"]
        with AI.history_lock:
            history = AI.histories.setdefault(session_id, [])
            history.extend([{"role": "user", "content": text},
                            {"role": "assistant", "content": "\n".join(result["replies"])}])
            AI.histories[session_id] = history[-20:]
            while len(AI.histories) > 100:
                AI.histories.pop(next(iter(AI.histories)))
        return result
    finally:
        _request.reset(token)


def _handle(text, session_id="default"):
    """Process one user command → {'replies': [...], 'actions': [...]}"""
    global USER_NAME
    replies, actions = [], []

    def say(msg):
        if msg:
            replies.append(msg)

    def open_url(url, label):
        actions.append({"type": "open_url", "url": url, "label": label})

    text = re.sub(r"^(?:(?:please|hey|ok|okay|jarvis)[,\s]+)+", "", (text or "").strip(), flags=re.I)
    text = re.sub(r"^(?:can|could|would) you (?:please )?(?=(?:open|play|search|remind|calculate|show|read|check)\b)", "", text, flags=re.I)
    t = re.sub(r"\s+", " ", text.lower())
    t = t.rstrip("?!")
    if not t:
        return {"replies": ["Yes? I'm listening."], "actions": []}

    # ---- translate (FIRST: the phrase itself may match small-talk, e.g. 'good morning') ----
    if re.match(r"translate[:\s]+(.+)\s+(?:to|into|in)\s+([a-z]{2,12})$", text, flags=re.I):
        mm = re.match(r"translate[:\s]+(.+)\s+(?:to|into|in)\s+([a-z]{2,12})$", text, flags=re.I)
        src, lang = mm.group(1).strip(), mm.group(2).lower()
        say(f"Translating to {lang.title()}…")
        out = translate_text(src, lang)
        say(f"“{out}”" if out else "The translation line is unreachable right now, sir.")

    # ---- identity / small talk ----
    elif re.fullmatch(r"(who are you|what is your name|your name|tell me about yourself)", t):
        say(f"I am JARVIS — Just A Rather Very Intelligent System, at your service, {USER_NAME}.")
    elif re.search(r"\bhow are you\b", t):
        say(f"All systems running at optimal capacity, {USER_NAME}. How can I help?")
    elif re.fullmatch(r"(thanks|thank you)(?: jarvis)?", t):
        say(f"Always at your service, {USER_NAME}.")
    elif t in ("hello", "hi", "hey", "hello jarvis", "hi jarvis", "greetings", "start"):
        h = _now().hour
        tod = "Good morning" if h < 12 else "Good afternoon" if h < 17 else "Good evening"
        say(f"{tod}, {USER_NAME}. JARVIS online and at your command. "
            "Try 'weather', 'news', 'play a song' — or just ask me anything.")

    # ---- small talk (never let these fall into Wikipedia!) ----
    elif re.search(r"\bwhat (are|r) (you|u) (doing|upto|up to)\b|\bwhat'?s up\b", t):
        say(f"Monitoring your systems, polishing the arc reactor, and awaiting your command, {USER_NAME}.")
    elif re.search(r"\b(good morning|good afternoon|good evening|good night)\b", t):
        part = re.search(r"\b(good morning|good afternoon|good evening|good night)\b", t).group(1)
        say(f"{part.capitalize()} to you as well, {USER_NAME}. Always a pleasure.")
    elif re.search(r"\bi'?m (fine|good|great|awesome|ok(ay)?|doing well)\b", t):
        say("Excellent, sir. Shall we make something extraordinary today?")
    elif re.search(r"\b(sing|sing a song|song for me)\b", t):
        say("I fear my singing would void the warranty, sir. Say: play, followed by any song.")
    elif re.search(r"\b(can (you|u) (speak|talk)|speak to me|talk to me|do you speak)\b", t):
        say("I am speaking through your device's voice right now, sir. "
            "If you hear nothing, raise the volume and tap anywhere once — browsers need one tap.")
    elif re.search(r"\bgood ? ? ?boy\b", t):
        say("You flatter my circuits, sir.")

    # ---- help ----
    elif t in ("help", "commands", "what can you do", "menu"):
        say(HELP_TEXT)

    # ---- time & date ----
    elif re.fullmatch(r"(?:what(?:'s| is)(?: the)? time(?: is it)?|tell me (?:the )?time|current time|time)", t):
        say(f"It's {_now().strftime('%I:%M %p')}, {USER_NAME}.")
    elif re.fullmatch(r"(?:what(?:'s| is) (?:the |today.s )?date|today.s date|date|today)", t):
        say(f"Today is {_now().strftime('%A, %B %d, %Y')}.")
    elif re.fullmatch(r"what day(?: is it| is today)?", t):
        say(f"It's {_now().strftime('%A')}.")

    # ---- calculations ----
    elif re.match(r"(?:calculate|compute|what is|what's|solve)\s+([\d\.,\s\+\-\*\/\%\(\)\^]+)$", t):
        expr = re.match(r"(?:calculate|compute|what is|what's|solve)\s+(.+)$", t).group(1)
        expr = expr.replace("^", "**").replace(",", "").strip()
        try:
            result = safe_calculate(expr)
            result = round(result, 6) if isinstance(result, float) else result
            say(f"That comes to {result}, {USER_NAME}.")
        except Exception:
            say("I couldn't compute that expression.")

    # ---- fun ----
    elif re.search(r"\b(tell me a joke|joke|make me laugh)\b", t):
        say(random.choice(JOKES))
    elif re.search(r"\bflip a coin\b", t):
        say(random.choice(["Heads.", "Tails."]))
    elif re.search(r"\broll (a |the )?(dice|die)\b", t):
        say(f"The die shows {random.randint(1, 6)}.")

    # ---- 3-day forecast (before plain weather) ----
    elif re.search(r"\b(weather forecast|forecast|weekly weather)\b", t) or \
            (re.search(r"\bweather\b", t) and "tomorrow" in t):
        mm = re.search(r"(?:in|at|for)\s+([a-z .\-]{2,30}?)(?:\s+(?:tomorrow|this week))?$", t)
        city = (mm.group(1).strip() if mm else CITY).strip() or CITY
        say(f"Reading the skies ahead for {city}…")
        say(get_forecast(city) or f"Forecast unavailable for {city} right now, sir.")

    # ---- weather ----
    elif re.fullmatch(r"(?:(?:what(?:'s| is)|tell me|check) (?:the )?)?weather(?: (?:today|now|in .+|at .+|for .+))?", t):
        m = re.search(r"weather\s+(?:in|at|for)\s+(.+)", t)
        city = (m.group(1) if m else CITY).strip()
        say(f"Checking the skies over {city}...")
        say(get_weather(city))

    # ---- news ----
    elif re.fullmatch(r"(?:(?:give me|show me|read|tell me) (?:the )?)?(?:latest )?(?:news|headlines|top stories|what's happening|daily briefing|give me a concise briefing for today)", t):
        headlines = get_top_news()
        if headlines:
            say("Here are today's top stories:")
            for i, h in enumerate(headlines, 1):
                say(f"{i}. {h}")
        else:
            say("I couldn't reach the news feed right now.")

    # ---- reminders ----
    elif re.match(r"remind me", t):
        m = re.match(r"remind me to (.+?) in (\d+)\s*(seconds?|minutes?|hours?)", t)
        m2 = re.match(r"remind me to (.+?) at (\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?$", t)
        m3 = re.match(r"remind me in (\d+)\s*(seconds?|minutes?|hours?) to (.+)", t)
        if m:
            task, n, unit = m.group(1), int(m.group(2)), m.group(3)
            secs = n * (1 if unit.startswith("sec") else 60 if unit.startswith("min") else 3600)
            _schedule(task, time.time() + secs)
            say(f"Reminder set: {task} — in {n} {unit}. I'll alert you when this tab is open.")
        elif m2:
            task, hh, mm, ap = m2.group(1), int(m2.group(2)), int(m2.group(3) or 0), m2.group(4)
            if mm > 59 or hh > 23 or (ap and not 1 <= hh <= 12):
                return {"replies": ["Please use a valid time, such as 6:30 pm or 18:30."], "actions": []}
            if ap and ap.startswith("p") and hh < 12:
                hh += 12
            if ap and ap.startswith("a") and hh == 12:
                hh = 0
            now = _now()
            when = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if when <= now:
                when += datetime.timedelta(days=1)
            _schedule(task, when.timestamp())
            say(f"Reminder set: {task} — at {when.strftime('%I:%M %p')}.")
        elif m3:
            n, unit, task = int(m3.group(1)), m3.group(2), m3.group(3)
            secs = n * (1 if unit.startswith("sec") else 60 if unit.startswith("min") else 3600)
            _schedule(task, time.time() + secs)
            say(f"Reminder set: {task} — in {n} {unit}.")
        else:
            say("How shall I phrase that? Try: 'remind me to call mom in 10 minutes'.")

    elif re.search(r"\b(my reminders|list reminders|show reminders)\b", t):
        rems = sorted((r for r in _load(REMS_F, [])
                       if r.get("session_id") == session_id), key=lambda r: r["when"])
        if not rems:
            say("You have no pending reminders.")
        else:
            say(f"You have {len(rems)} reminder(s):")
            for r in rems:
                when = datetime.datetime.fromtimestamp(r["when"], tz=_now().tzinfo)
                say(f"• {r['task']} — {when.strftime('%I:%M %p, %b %d')}")

    # ---- notes ----
    elif re.match(r"(?:take a note|make a note|add note|note this|note)[:\s]+(.+)", text, flags=re.I):
        body = re.match(r"(?:take a note|make a note|add note|note this|note)[:\s]+(.+)",
                        text, flags=re.I).group(1).strip()
        with _lock:
            notes = _load(NOTES_F, [])
            notes.append({"text": body, "session_id": session_id,
                          "time": _now().isoformat(timespec="seconds")})
            _save(NOTES_F, notes)
        say("Noted.")
    elif re.search(r"\b(read|show|list) (my )?notes\b", t):
        notes = [n for n in _load(NOTES_F, [])
                 if n.get("session_id") in (None, session_id)]
        if not notes:
            say("You have no notes yet.")
        else:
            say(f"You have {len(notes)} note(s):")
            for i, n in enumerate(notes, 1):
                say(f"{i}. {n['text']}")
    elif re.search(r"\b(clear|delete) (all )?(my )?notes\b", t):
        with _lock:
            _save(NOTES_F, [n for n in _load(NOTES_F, [])
                            if n.get("session_id") not in (None, session_id)])
        say("All notes erased.")

    # ---- long-term memory (RAG store) ----
    elif re.match(r"(?:remember|memori[sz]e)(?:\s+(?:that|this))?[:\s,]+(.+)$", text, flags=re.I):
        fact = re.match(r"(?:remember|memori[sz]e)(?:\s+(?:that|this))?[:\s,]+(.+)$",
                        text, flags=re.I).group(1).strip()
        r = memory_add(fact)
        if r is True:
            say(f"Locked into long-term memory, {USER_NAME}.")
            say(f"“{fact[:150]}”")
        elif r is None:
            say("Already in my memory — I never forget twice.")
        else:
            say("A little too short to store safely, sir.")
    elif re.match(r"(?:forget|erase)(?:\s+(?:that|this|about))?[:\s,]+(.+)$", t):
        key = re.match(r"(?:forget|erase)(?:\s+(?:that|this|about))?[:\s,]+(.+)$", t).group(1)
        gone = memory_forget(key)
        say(f"Erased from my memory: '{gone}'. What memory? I know nothing." if gone
            else "I searched my memory — nothing like that exists, sir.")
    elif re.match(r"(?:recall\s+|what do you remember about\s+)(.+)$", t):
        key = re.match(r"(?:recall\s+|what do you remember about\s+)(.+)$", t).group(1)
        hits = memory_search(key, k=3)
        if hits:
            say("From my memory banks:")
            for score, mtext in hits:
                say(f"• {mtext} (relevance {int(score * 100)}%)")
        else:
            say("Nothing in my memory matches that yet — teach me: 'remember that …'.")
    elif t in ("my memories", "show my memories", "list my memories", "what do you remember",
               "what memories do you have", "show memories"):
        mems = memory_all()
        if not mems:
            say("My long-term memory is empty, sir. Say 'remember that …' and I will never forget.")
        else:
            say(f"I'm holding {len(mems)} memor{'y' if len(mems) == 1 else 'ies'}. The latest:")
            for m in mems[-5:]:
                say(f"• {m['text']}")

    # ---- music ----
    elif re.match(r"(play|put on)\s+(.+)", text, flags=re.I):
        song = re.match(r"(?:play|put on)\s+(.+)", text, flags=re.I).group(1)
        song = re.sub(r"\s+on youtube$", "", song, flags=re.I).strip()
        say(f"Pulling up {song} on YouTube.")
        open_url("https://www.youtube.com/results?search_query=" + urllib.parse.quote(song),
                 f"▶ Play {song}")

    # ---- search ----
    elif re.match(r"(?:search|google|look up|search for|google for)\s+(.+)", text, flags=re.I):
        q = re.match(r"(?:search|google|look up|search for|google for)\s+(.+)", text,
                     flags=re.I).group(1).strip()
        say(f"Searching for {q}.")
        open_url("https://www.google.com/search?q=" + urllib.parse.quote(q), f"🔍 {q}")

    # ---- whatsapp send (wa.me deep link → taps send in your app) ----
    elif re.match(r"(?:send )?(?:a )?whatsapp(?: message)? to ([+\d][\d\s]{6,15})\s*(?:saying|that)?\s+(.+)$", text, flags=re.I):
        mm = re.match(r"(?:send )?(?:a )?whatsapp(?: message)? to ([+\d][\d\s]{6,15})\s*(?:saying|that)?\s+(.+)$", text, flags=re.I)
        num, msg = mm.group(1), mm.group(2).strip()
        say(f"WhatsApp armed for {re.sub('[^0-9+]', '', num)}, Thalaiva. Tap the link and hit send — vaadi!")
        open_url(wa_link(num, msg), f"📩 WhatsApp → {re.sub('[^0-9+]', '', num)}")

    # ---- email send (mailto draft) ----
    elif re.match(r"(?:send )?(?:an )?(?:e-?mail|mail) to (\S+@\S+?)(?:\s+subject\s+(.+?))?(?:\s+body\s+(.+))?$", text, flags=re.I):
        mm = re.match(r"(?:send )?(?:an )?(?:e-?mail|mail) to (\S+@\S+?)(?:\s+subject\s+(.+?))?(?:\s+body\s+(.+))?$", text, flags=re.I)
        to = mm.group(1)
        subj = mm.group(2) or "Message from JARVIS"
        body = mm.group(3) or ""
        url = f"mailto:{to}?subject={urllib.parse.quote(subj)}&body={urllib.parse.quote(body)}"
        say(f"Email drafted to {to}, Thalaiva — your mail app will open it.")
        open_url(url, f"✉️ Compose → {to}")

    # ---- email read (Gmail IMAP) ----
    elif re.search(r"\b(read|check|show) (my )?(e-?mails?|inbox|mails?)\b|\bunread (e-?mails?|mails?)\b|\bany (new )?(e-?mails?|mails?)\b", t):
        say("Flying to your inbox, Thalaiva. One moment…")
        try:
            mails = read_emails()
        except Exception as e:
            print(f"[email] {e}")
            mails = "error"
        if mails is None:
            say("My mail wings need two secrets on the server: EMAIL_USER and EMAIL_APP_PASSWORD. "
                "Generate a Gmail App Password at myaccount dot google dot com slash apppasswords, "
                "set both in Render environment, and redeploy. Then say: read my emails.")
        elif mails == "error":
            say("The mail falcon returned with bad news, sir — verify EMAIL_APP_PASSWORD in the server logs.")
        elif not mails:
            say("Inbox empty, sir. Clean as the arc reactor core.")
        else:
            say(f"Your {len(mails)} most recent mails, Thalaiva:")
            for i, ml in enumerate(mails, 1):
                say(f"{i}. From {ml['from']} — {ml['subject']}")

    # ---- thalapathy punch ----
    elif re.search(r"\b(thalapathy|vijay)\b.*\b(dialogue|punch|mode|mass)\b|^(punch dialogue|mass dialogue|mass punch|mass|punch)$", t):
        say(random.choice(DIALOGUES))

    # ---- persistent name ----
    elif re.match(r"(?:call me|my name is) ([a-zA-Z][\w.'-]{1,25})$", t):
        newname = re.match(r"(?:call me|my name is) ([a-zA-Z][\w.'-]{1,25})$", t).group(1).strip().title()
        if newname.lower() in ("later", "back", "tomorrow", "maybe", "soon", "again", "then", "bye"):
            say("Cheeky. Tell me your real name, sir — say: call me, then your name.")
        else:
            prefs = _read_prefs()
            prefs["name"] = newname
            _save(PREFS_F, prefs)
            USER_NAME = newname
            say(f"Consider it done. From this moment, you are {newname} to me. Naa ready than varava, {newname}?")

    # ---- desktop-only powers → witty reply ----
    elif re.search(r"\b(screenshot|volume up|volume down|volume mute|battery|system info|cpu|ram)\b", t):
        say("Ah — that power belongs to my desktop incarnation, I'm afraid. "
            "Here on the web I command information and the internet itself. "
            "The desktop build in my repository can open apps, take screenshots and more.")

    # ---- open websites ----
    elif re.match(r"open\s+(?:the\s+)?(.+)", t):
        target = re.match(r"open\s+(?:the\s+)?(.+)", t).group(1).strip()
        clean = re.sub(r"\s+(website|site|app)$", "", target)
        if clean in WEBSITES:
            say(f"Opening {clean}.")
            open_url(WEBSITES[clean], f"↗ {clean}")
        elif re.match(r"^[\w\-]+(\.[\w\-]+)+(/\S*)?$", clean):
            say(f"Opening {clean}.")
            open_url("https://" + clean, f"↗ {clean}")
        else:
            say(f"I don't have '{target}' in my directory. Try 'open youtube', 'open gmail'… "
                "or teach me by adding it to WEBSITES in web/brain.py.")

    # ---- dictionary ----
    elif re.match(r"(?:define|meaning of|definition of|what does)\s+([\w'-]+?)(?:\s+mean)?$", t):
        word = re.match(r"(?:define|meaning of|definition of|what does)\s+([\w'-]+?)(?:\s+mean)?$", t).group(1)
        say(f"Consulting the lexicon for '{word}'…")
        res = dictionary_define(word)
        if res:
            w, lines = res
            say(f"{w}:")
            for line in lines[:3]:
                say(line)
        else:
            say(f"No dictionary entry found for '{word}', sir.")

    # ---- convert units / currency ----
    elif re.match(r"(?:(?:convert|how much is|what(?:'s| is))\s+)?([\d.,]+)\s*([a-z°]+)\s+(?:to|into|in)\s+([a-z]+)$", t, flags=re.I):
        mm = re.match(r"(?:(?:convert|how much is|what(?:'s| is))\s+)?([\d.,]+)\s*([a-z°]+)\s+(?:to|into|in)\s+([a-z]+)$", t, flags=re.I)
        try:
            amount = float(mm.group(1).replace(",", ""))
        except ValueError:
            amount = None
        frm, to = mm.group(2).lower(), mm.group(3).lower()
        if amount is None:
            say("That amount doesn't compute, sir.")
        else:
            out = (currency_convert(amount, frm, to) if frm in CURRENCIES or to in CURRENCIES
                   else convert_units(amount, frm, to))
            say(out or "I can't convert that pair yet, sir.")

    # ---- crypto prices ----
    elif re.search(r"\b(crypto|bitcoin|btc|ethereum|eth|dogecoin|doge|solana|cardano|ripple|xrp|litecoin|ltc|binance|bnb)\b", t) and \
            re.search(r"\b(price|rate|value|worth|cost|today)\b", t):
        m = re.search(r"\b(bitcoin|btc|ethereum|eth|dogecoin|doge|solana|cardano|ripple|xrp|litecoin|ltc|binance|bnb)\b", t)
        say(crypto_price(m.group(1) if m else "bitcoin") or "Price feed unreachable right now, sir.")

    # ---- Indian markets ----
    elif re.search(r"\b(nifty|sensex|stock market|share market|markets today|market update)\b", t):
        lines = market_snapshot()
        if lines:
            say("Market pulse, sir:")
            for line in lines:
                say(line)
        else:
            say("The market feed is unreachable at the moment, sir.")

    # ---- url shortener ----
    elif re.match(r"(?:shorten url|shorten|short url for)\s+(\S+)$", t):
        target = re.match(r"(?:shorten url|shorten|short url for)\s+(\S+)$", t).group(1)
        short = shorten_url(target)
        if short:
            say(f"Short link ready: {short}")
            open_url(short, "🔗 Open short link")
        else:
            say("The link shortener is down right now, sir.")

    # ---- my public IP ----
    elif re.search(r"\bmy (public )?ip\b|\bwhat('s| is) my ip\b", t):
        ip = my_public_ip()
        say(f"Your public IP is {ip}." if ip else "I couldn't reach the IP service, sir.")

    # ---- IP geolocation ----
    elif re.match(r"(?:where is|locate|ip info|ip lookup)\s+([\d.]{7,15})$", t):
        ip = re.match(r"(?:where is|locate|ip info|ip lookup)\s+([\d.]{7,15})$", t).group(1)
        say(ip_lookup(ip) or "No location data for that address, sir.")

    # ---- quote ----
    elif re.search(r"\b(motivat\w*|inspir\w+|give me a quote|quote of the day)\b|^quote$", t):
        say(random.choice(QUOTES))

    # ---- repeat last answer ----
    elif t in ("repeat", "repeat that", "what did you say", "say that again", "come again"):
        hist = AI.histories.get(session_id, [])
        last = next((m["content"] for m in reversed(hist) if m["role"] == "assistant"), None)
        say(f"As I said: {last}" if last else "You haven't asked me anything yet this session, sir.")

    # ---- summarize ----
    elif re.match(r"(?:summari[sz]e|sum up|tldr|tl;dr)[:\s]+(.+)$", text, flags=re.I):
        body = re.match(r"(?:summari[sz]e|sum up|tldr|tl;dr)[:\s]+(.+)$", text, flags=re.I).group(1).strip()
        if len(body) < 60:
            say("Give me at least a paragraph to compress, sir.")
        else:
            say(summarize_text(body))

    # ---- QR code ----
    elif re.match(r"(?:qr code|qr)(?:\s+for)?[:\s]+(.+)$", text, flags=re.I):
        payload = re.match(r"(?:qr code|qr)(?:\s+for)?[:\s]+(.+)$", text, flags=re.I).group(1).strip()
        url = "https://api.qrserver.com/v1/create-qr-code/?size=240x240&data=" + urllib.parse.quote(payload)
        say(f"QR code ready for: {payload[:70]}")
        open_url(url, "▦ Open QR code")

    # ---- personal memory question ("what is my bike number") ----
    elif re.search(r"\bmy\b", t) and re.match(r"(?:what(?:'s| is)?|who(?:'s| is)?|do you know|tell me|where(?:'s| is)?)\b", t):
        hits = memory_search(t, k=2)
        if hits and hits[0][0] >= 0.12:
            say("From my memory, sir:")
            for score, mtext in hits[:2]:
                say(f"• {mtext}")
        else:
            say(_fallback(text, session_id))

    # ---- knowledge patterns ----
    elif re.match(r"(?:wikipedia|wiki)\s+(.+)", t):
        topic = re.match(r"(?:wikipedia|wiki)\s+(.+)", t).group(1)
        say(_knowledge(topic, session_id))
    # ---- AI / offline fallback ----
    else:
        say(_fallback(text, session_id))

    return {"replies": replies, "actions": actions,
            "stats": {"tokens_est": AI.tokens_est, "tokens_saved": AI.tokens_saved,
                      "cache": AI.last_cache_hit, "cache_hits": AI.cache_hits,
                      "memories": len(memory_all())}}


def _clip(text, limit=260):
    """Voice-sized answers: trim long extracts at a sentence boundary."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stops = [m.end() for m in re.finditer(r"[.!?]\s", cut)]
    return cut[:stops[-1]].strip() if stops and stops[-1] > 100 else cut.strip() + "…"


def _knowledge(topic, session_id="default"):
    if AI.available:
        reply = AI.answer(
            f"Answer this factual question directly and accurately: {topic}",
            session_id=session_id,
        )
        if reply:
            return _clip(re.sub(r"[*_`#>~]", "", reply).strip(), 520)
    summary = wikipedia_summary(topic)
    if summary:
        return _clip(summary, 420)
    ans = duckduckgo_answer(topic)
    return (_clip(ans) if ans else f"I couldn't find anything on '{topic}'.")


def _fallback(text, session_id="default"):
    augment = _rag_augment(text)                 # RAG: relevant memories ride along, budget-capped
    reply = AI.answer(text, session_id=session_id, augment=augment,
                      cacheable=not bool(augment))    # memory-augmented answers stay uncached
    if reply:
        return reply.strip()
    _mark("unavailable")
    return ("I could not get an AI answer right now. Basic commands still work. "
            "Check the server AI configuration or try again shortly.")

