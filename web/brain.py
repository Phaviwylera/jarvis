# -*- coding: utf-8 -*-
"""
J.A.R.V.I.S. Web Brain — self-contained command + AI logic for the web app.
Mirrors the desktop assistant, but returns JSON ({replies, actions}) instead
of speaking aloud. Speech happens in the browser (Web Speech API).
Pure standard library + the environment. Zero required pip packages.
"""

import datetime
import json
import os
import queue
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE = os.path.dirname(os.path.abspath(__file__))
MEM = os.path.join(BASE, "memory")
os.makedirs(MEM, exist_ok=True)
NOTES_F = os.path.join(MEM, "notes.json")
REMS_F = os.path.join(MEM, "reminders.json")
PREFS_F = os.path.join(MEM, "prefs.json")
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

_events = queue.Queue()          # due reminders waiting to be shown/spoken
_lock = threading.Lock()

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
    "You are J.A.R.V.I.S., personal AI of {user}. Personality: calm, supremely "
    "confident, cinema-grade charm in the style of Tamil superstar Thalapathy "
    "Vijay's screen presence — powerful one-liners with effortless swag. "
    "Occasionally (sparingly) drop a short Vijay-style punch or Tanglish word "
    "(e.g. 'I am waiting', 'Bloody sweet', 'Naa ready than varava', 'Thalaiva'). "
    "Address the user as {user} or 'Thalaiva'. CRITICAL: replies are SPOKEN "
    "ALOUD — keep 1-3 SHORT punchy sentences, plain text, no markdown, no emojis."
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
        self.history = []
        self.last_used = None
        self.chain = []
        self._add(self.primary, os.environ.get("JARVIS_API_KEY", ""))
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
                           "base_url": base, "key": key, "dead": False})

    @staticmethod
    def _live(e):
        return (e["key"] or e["name"] == "ollama") and not e["dead"]

    @property
    def available(self):
        return any(self._live(e) for e in self.chain)

    def status(self):
        live = [e["name"] for e in self.chain if self._live(e)]
        return "+".join(live) if live else "offline"

    def answer(self, question):
        if not self.available:
            return None
        self.history.append({"role": "user", "content": question})
        self.history = self.history[-16:]
        for attempt in (1, 2):
            for e in self.chain:
                if not self._live(e):
                    continue
                try:
                    reply = (self._ask_gemini(e) if e["name"] == "gemini"
                             else self._ask_openai_style(e))
                    if reply:
                        if e["name"] != self.primary:
                            print(f"[ai] FAILOVER -> answered by {e['name']}")
                        self.last_used = e["name"]
                        self.history.append({"role": "assistant", "content": reply})
                        return reply
                except urllib.error.HTTPError as err:
                    print(f"[ai] {e['name']}: HTTP {err.code} (attempt {attempt})")
                    if err.code in (400, 401, 403, 404):
                        e["dead"] = True          # auth/model errors won't self-heal
                except Exception as ex:
                    print(f"[ai] {e['name']} (attempt {attempt}): {ex}")
            time.sleep(1.2)
        return None

    def _ask_gemini(self, e):
        convo = "\n".join(("User: " if m["role"] == "user" else "JARVIS: ") + m["content"]
                          for m in self.history)
        url = f"{e['base_url']}/models/{e['model']}:generateContent?key={e['key']}"
        payload = {
            "system_instruction": {"parts": [{"text": SYSTEM_PERSONA.format(user=USER_NAME)}]},
            "contents": [{"role": "user", "parts": [{"text": convo}]}],
            "generationConfig": {"temperature": 0.7},
        }
        data = http_post_json(url, payload)
        return (data["candidates"][0]["content"]["parts"][0]["text"] or "").strip()

    def _ask_openai_style(self, e):
        headers = {"Authorization": f"Bearer {e['key']}"} if e["key"] else {}
        payload = {"model": e["model"],
                   "messages": [{"role": "system",
                                 "content": SYSTEM_PERSONA.format(user=USER_NAME)}] + self.history,
                   "temperature": 0.7, "max_tokens": 300}
        data = http_post_json(f"{e['base_url']}/chat/completions", payload, headers=headers)
        return (data["choices"][0]["message"]["content"] or "").strip()


AI = AIBrain()

# ============================================================================
#  Info services (free, no key)
# ============================================================================

def wikipedia_summary(topic):
    topic = topic.strip()
    if not topic:
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
            return wikipedia_summary(res[1][0])
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
    "info: weather [in X] • news • who is/what is • wikipedia X • search X\n"
    "web: open youtube • open gmail • play <song> • search <anything>\n"
    "productivity: remind me to X in 10 min / at 6 pm • my reminders • note X • read my notes\n"
    "fun: tell me a joke • flip a coin • roll a dice • calculate 45*12\n"
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
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[mem] {e}")


def drain_events():
    out = []
    while not _events.empty():
        try:
            out.append(_events.get_nowait())
        except queue.Empty:
            break
    return out


def _reminder_loop():
    while True:
        try:
            now = time.time()
            with _lock:
                rems = _load(REMS_F, [])
                due = [r for r in rems if r.get("when", 0) <= now]
                keep = [r for r in rems if r.get("when", 0) > now]
                if due:
                    with open(REMS_F, "w", encoding="utf-8") as f:
                        json.dump(keep, f, indent=2)
            for r in due:
                _events.put(f"⏰ Reminder for you, {USER_NAME}: {r['task']}")
        except Exception as e:
            print(f"[reminders] {e}")
        time.sleep(3)


threading.Thread(target=_reminder_loop, daemon=True).start()


def _schedule(task, when_epoch):
    with _lock:
        rems = _load(REMS_F, [])
        rems.append({"task": task, "when": when_epoch})
        try:
            with open(REMS_F, "w", encoding="utf-8") as f:
                json.dump(rems, f, indent=2)
        except Exception as e:
            print(f"[reminders] save: {e}")


# ============================================================================
#  MAIN COMMAND HANDLER
# ============================================================================

def handle(text):
    """Process one user command → {'replies': [...], 'actions': [...]}"""
    global USER_NAME
    replies, actions = [], []

    def say(msg):
        if msg:
            replies.append(msg)

    def open_url(url, label):
        actions.append({"type": "open_url", "url": url, "label": label})

    t = (text or "").lower().strip()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"^(please|hey|ok|okay| jarvis,?)\s+", "", t)
    if not t:
        return {"replies": ["Yes? I'm listening."], "actions": []}

    # ---- identity / small talk ----
    if re.search(r"\b(who are you|your name|about yourself)\b", t):
        say(f"I am JARVIS — Just A Rather Very Intelligent System, at your service, {USER_NAME}.")
    elif re.search(r"\bhow are you\b", t):
        say(f"All systems running at optimal capacity, {USER_NAME}. How can I help?")
    elif re.search(r"\bthank", t):
        say(f"Always at your service, {USER_NAME}.")
    elif t in ("hello", "hi", "hey", "hello jarvis", "hi jarvis", "greetings", "start"):
        h = datetime.datetime.now().hour
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
    elif re.search(r"\b(what('s| is)?|tell me|current)\b.*\btime\b", t) or t == "time":
        say(f"It's {datetime.datetime.now().strftime('%I:%M %p')}, {USER_NAME}.")
    elif re.search(r"\b(what('s| is)?|today('s| is)?)\b.*\bdate\b", t) or t in ("date", "today"):
        say(f"Today is {datetime.datetime.now().strftime('%A, %B %d, %Y')}.")
    elif re.search(r"\bwhat day\b", t):
        say(f"It's {datetime.datetime.now().strftime('%A')}.")

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

    # ---- weather ----
    elif re.search(r"\bweather\b", t):
        m = re.search(r"weather\s+(?:in|at|for)\s+(.+)", t)
        city = (m.group(1) if m else CITY).strip()
        say(f"Checking the skies over {city}...")
        say(get_weather(city))

    # ---- news ----
    elif re.search(r"\b(news|headlines|top stories|what's happening)\b", t):
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
            say(f"Reminder set: {task} — in {n} {unit}. (Keep this tab open and I'll alert you.)")
        elif m2:
            task, hh, mm, ap = m2.group(1), int(m2.group(2)), int(m2.group(3) or 0), m2.group(4)
            if ap and ap.startswith("p") and hh < 12:
                hh += 12
            if ap and ap.startswith("a") and hh == 12:
                hh = 0
            now = datetime.datetime.now()
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
        rems = sorted(_load(REMS_F, []), key=lambda r: r["when"])
        if not rems:
            say("You have no pending reminders.")
        else:
            say(f"You have {len(rems)} reminder(s):")
            for r in rems:
                when = datetime.datetime.fromtimestamp(r["when"])
                say(f"• {r['task']} — {when.strftime('%I:%M %p, %b %d')}")

    # ---- notes ----
    elif re.match(r"(?:take a note|make a note|add note|note this|note)[:\s]+(.+)", text, flags=re.I):
        body = re.match(r"(?:take a note|make a note|add note|note this|note)[:\s]+(.+)",
                        text, flags=re.I).group(1).strip()
        notes = _load(NOTES_F, [])
        notes.append({"text": body,
                      "time": datetime.datetime.now().isoformat(timespec="seconds")})
        _save(NOTES_F, notes)
        say("Noted.")
    elif re.search(r"\b(read|show|list) (my )?notes\b", t):
        notes = _load(NOTES_F, [])
        if not notes:
            say("You have no notes yet.")
        else:
            say(f"You have {len(notes)} note(s):")
            for i, n in enumerate(notes, 1):
                say(f"{i}. {n['text']}")
    elif re.search(r"\b(clear|delete) (all )?(my )?notes\b", t):
        _save(NOTES_F, [])
        say("All notes erased.")

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
        say(f"WhatsApp armed for {re.sub(chr(92)+'s','',num)}, Thalaiva. Tap the link and hit send — vaadi!")
        open_url(wa_link(num, msg), f"📩 WhatsApp → {re.sub(chr(92)+'D','',num)}")

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

    # ---- knowledge patterns ----
    elif re.match(r"(?:wikipedia|wiki)\s+(.+)", t):
        topic = re.match(r"(?:wikipedia|wiki)\s+(.+)", t).group(1)
        say(_knowledge(topic))
    elif re.match(r"(?:tell me (?:something )?about|know about|information (?:about|on)|explain)\s+(.+?)\??$", t):
        topic = re.match(r"(?:tell me (?:something )?about|know about|information (?:about|on)|explain)\s+(.+?)\??$", t).group(1)
        if len(topic.split()) <= 8:
            say(_knowledge(topic))
        else:
            say(_fallback(text))
    elif re.match(r"(?:who is|who's|who was|what is|what's a|whats a|who are)\s+(.+?)\??$", t):
        topic = re.match(r"(?:who is|who's|who was|what is|what's a|whats a|who are)\s+(.+?)\??$", t).group(1)
        if len(topic.split()) <= 8:
            say(_knowledge(re.sub(r"\?$", "", topic)))
        else:
            say(_fallback(text))

    # ---- AI / offline fallback ----
    else:
        say(_fallback(text))

    return {"replies": replies, "actions": actions}


def _clip(text, limit=260):
    """Voice-sized answers: trim long extracts at a sentence boundary."""
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stops = [m.end() for m in re.finditer(r"[.!?]\s", cut)]
    return cut[:stops[-1]].strip() if stops and stops[-1] > 100 else cut.strip() + "…"


def _knowledge(topic):
    summary = wikipedia_summary(topic)
    if summary:
        return _clip(summary)
    ans = duckduckgo_answer(topic)
    return (_clip(ans) if ans else f"I couldn't find anything on '{topic}'.")


def _fallback(text):
    reply = AI.answer(text)
    if reply:
        return _clip(re.sub(r"[*_`#>~]", "", reply).strip(), 400)
    ans = wikipedia_summary(text) or duckduckgo_answer(text)
    if ans:
        return _clip(ans)
    return (f"I'm not sure about that yet, {USER_NAME}. Try 'help' to see my commands — "
            "or set a JARVIS_API_KEY and I'll answer absolutely anything.")
