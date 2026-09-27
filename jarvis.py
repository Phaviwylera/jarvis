#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
==============================================================================
  J.A.R.V.I.S. — Just A Rather Very Intelligent System
  A personal AI assistant for your desktop.

  FEATURES
    • Full voice interaction (speech in, speech out)         [optional libs]
    • Hybrid brain: offline commands + AI (Gemini/OpenAI/Groq/Ollama)
    • Open apps & websites, play music on YouTube
    • Weather, news, Wikipedia, web search
    • Notes, reminders, timers
    • Battery / system info, screenshots, volume control
    • Works with ZERO dependencies in text mode (voice = optional)

  QUICK START
    1.  python jarvis.py            (auto mode: voice if available)
    2.  python jarvis.py --text     (text-only, works anywhere)

  See README.md for setup, voice libraries and free AI key instructions.
==============================================================================
"""

import argparse
import ast
import calendar
import datetime
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

APP_NAME = "J.A.R.V.I.S."
VERSION = "1.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_DIR = os.path.join(BASE_DIR, "memory")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
NOTES_PATH = os.path.join(MEMORY_DIR, "notes.json")
REMINDERS_PATH = os.path.join(MEMORY_DIR, "reminders.json")

# Browser-style UA: providers behind Cloudflare (e.g. Groq) block script-looking agents.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ============================================================================
#  CONFIGURATION
# ============================================================================

DEFAULT_CONFIG = {
    "user_name": "sir",                 # how JARVIS addresses you ("sir", "Tony", your name...)
    "city": "Chennai",                  # default city for weather
    "stt_language": "en-IN",            # speech recognition language (en-US, en-GB, hi-IN, ...)
    "wake_word": "jarvis",              # activation word when wake-word mode is on
    "listen_for_wake_word": False,      # True = stays asleep until you say the wake word
    "voice_rate": 180,                  # speech speed (words per minute)
    "voice_enabled": True,              # set False to mute spoken replies
    "custom_apps": {                    # add your own:  "app-name": "command-or-path"
        # "vs code": "code",
        # "telegram": "C:\\Path\\To\\Telegram.exe",
    },
    "llm": {
        "provider": "gemini",           # gemini | openai | groq | ollama | none
        "api_key": "",                  # paste your key here (or set env JARVIS_API_KEY)
        "model": "",                    # empty = sensible default per provider
        "base_url": "",                 # empty = default endpoint per provider
        "extra_keys": {                 # AUTOMATIC FAILOVER brains used if primary fails
            "gemini": "",
            "groq": "",
            "openai": ""
        },
        "max_history": 8
    }
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
            deep_update(cfg, user_cfg)
        except Exception as e:
            print(f"[config] Could not parse config.json ({e}); using defaults.")
    else:
        save_config(cfg)
        print(f"[config] Created default config at {CONFIG_PATH}")
        print("[config] Tip: add your AI API key there to unlock full conversation.")
    # Environment variables override the file so you never hard-code secrets.
    if os.environ.get("JARVIS_API_KEY"):
        cfg["llm"]["api_key"] = os.environ["JARVIS_API_KEY"]
    if os.environ.get("JARVIS_LLM_PROVIDER"):
        cfg["llm"]["provider"] = os.environ["JARVIS_LLM_PROVIDER"]
    for _prov, _env in (("gemini", "GEMINI_API_KEY"), ("groq", "GROQ_API_KEY"),
                        ("openai", "OPENAI_API_KEY")):
        if os.environ.get(_env):            # failover keys via environment
            cfg["llm"].setdefault("extra_keys", {})[_prov] = os.environ[_env]
    return cfg


def deep_update(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_update(base[k], v)
        else:
            base[k] = v


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4)
    except Exception as e:
        print(f"[config] Could not save config: {e}")


# ============================================================================
#  OPTIONAL DEPENDENCIES  (everything degrades gracefully if missing)
# ============================================================================

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    import speech_recognition as sr
except ImportError:
    sr = None

try:
    import psutil
except ImportError:
    psutil = None

try:
    import pyautogui
except ImportError:
    pyautogui = None

try:
    import pywhatkit
except ImportError:
    pywhatkit = None


# ============================================================================
#  SPEECH OUTPUT  (TTS)
# ============================================================================

class Voice:
    """Text-to-speech with automatic fallback to printing."""

    def __init__(self, cfg):
        self.enabled = False
        self.engine = None
        self.lock = threading.Lock()
        if not cfg.get("voice_enabled", True):
            return
        if pyttsx3 is None:
            print("[voice] pyttsx3 not installed → text-only replies. (pip install pyttsx3)")
            return
        try:
            self.engine = pyttsx3.init()
            self.engine.setProperty("rate", int(cfg.get("voice_rate", 180)))
            self._pick_voice()
            self.enabled = True
        except Exception as e:
            print(f"[voice] Could not start speech engine ({e}) → text-only replies.")
            self.engine = None

    def _pick_voice(self):
        """Prefer a British/male voice to sound like the real JARVIS."""
        try:
            voices = self.engine.getProperty("voices") or []
            preferred = ("daniel", "david", "george", "james", "english", "male")
            for v in voices:
                tag = (v.name + " " + getattr(v, "id", "")).lower()
                if any(p in tag for p in preferred):
                    self.engine.setProperty("voice", v.id)
                    return
        except Exception:
            pass

    def say(self, text):
        if not text:
            return
        print(f"JARVIS: {text}")
        if self.enabled and self.engine:
            with self.lock:
                try:
                    self.engine.say(clean_for_speech(text))
                    self.engine.runAndWait()
                except Exception:
                    pass


def clean_for_speech(text):
    """Strip markdown-ish characters so they are not read aloud."""
    t = re.sub(r"[*_`#>~]", "", text)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


# ============================================================================
#  SPEECH INPUT  (STT)
# ============================================================================

class Ears:
    """Microphone input with automatic fallback to keyboard."""

    def __init__(self, cfg, text_only=False):
        self.cfg = cfg
        self.recognizer = None
        self.mic = None
        self.ready = False
        if text_only:
            return
        if sr is None:
            print("[ears] SpeechRecognition not installed → typing mode. "
                  "(pip install SpeechRecognition PyAudio)")
            return
        try:
            self.recognizer = sr.Recognizer()
            self.recognizer.energy_threshold = 300
            self.mic = sr.Microphone()
            with self.mic as source:
                self.recognizer.adjust_for_ambient_noise(source, duration=1)
            self.ready = True
        except Exception as e:
            print(f"[ears] Microphone unavailable ({e}) → typing mode.")
            self.recognizer = None
            self.mic = None

    def listen(self, prompt="You: ", timeout=6, phrase_limit=12):
        """Return the heard/typed text (possibly '')."""
        if not self.ready:
            try:
                return input(prompt).strip()
            except EOFError:
                return "exit"
        with self.mic as source:
            print("\n🎙  Listening..." + (" (say 'jarvis' first)" if self.cfg.get("listen_for_wake_word") else ""))
            try:
                audio = self.recognizer.listen(source, timeout=timeout,
                                               phrase_time_limit=phrase_limit)
            except sr.WaitTimeoutError:
                return ""
        try:
            text = self.recognizer.recognize_google(
                audio, language=self.cfg.get("stt_language", "en-IN"))
            print(f"You: {text}")
            return text.strip()
        except sr.UnknownValueError:
            print("(didn't catch that)")
            return ""
        except sr.RequestError:
            print("[ears] Speech service unreachable; type instead.")
            try:
                return input("You (type): ").strip()
            except EOFError:
                return "exit"


# ============================================================================
#  HTTP HELPERS (stdlib only)
# ============================================================================

def http_get(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_json(url, headers=None, timeout=12):
    return json.loads(http_get(url, headers, timeout).decode("utf-8", "replace"))


def http_post_json(url, payload, headers=None, timeout=25):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ============================================================================
#  AI BRAIN  (LLM providers + offline fallback)
# ============================================================================

SYSTEM_PERSONA = (
    "You are J.A.R.V.I.S., the highly intelligent AI assistant of {user}, "
    "inspired by Tony Stark's AI. You are polite, witty, efficient and slightly "
    "formal, addressing the user as '{user}'. IMPORTANT: your replies are "
    "spoken aloud, so keep them concise (1-3 short sentences unless detail is "
    "explicitly requested), use plain text with no markdown, emojis or symbols."
)


class Brain:
    """Multi-provider AI brain with AUTOMATIC FAILOVER.

    Chain =  primary provider (config llm.provider + llm.api_key)
          +  every provider in llm.extra_keys that has a key
          +  local ollama when OLLAMA_HOST is set.
    If one brain goes down, the next answers — automatically.
    """

    PROVIDER_DEFAULTS = {
        "gemini": {"model": "gemini-3.8-flash",
                   "base_url": "https://generativelanguage.googleapis.com/v1beta"},
        "groq":   {"model": "openai/gpt-oss-120b",
                   "base_url": "https://api.groq.com/openai/v1"},
        "openai": {"model": "gpt-4o-mini",
                   "base_url": "https://api.openai.com/v1"},
        "ollama": {"model": "llama3.1",
                   "base_url": "http://localhost:11434/v1"},
    }

    def __init__(self, cfg):
        self.cfg = cfg
        llm = cfg.get("llm", {})
        self.provider = (llm.get("provider") or "none").lower()
        self.history = []
        self.last_used = None
        self.chain = []
        if self.provider != "none":
            self._add(self.provider, llm.get("api_key", ""),
                      llm.get("model", ""), llm.get("base_url", ""))
        for name, key in (llm.get("extra_keys", {}) or {}).items():
            if name != self.provider and key:
                self._add(name, key)
        if (self.provider == "ollama" or os.environ.get("OLLAMA_HOST")) and \
                not any(e["name"] == "ollama" for e in self.chain):
            self._add("ollama", "")

    def _add(self, name, key, model="", base_url=""):
        d = self.PROVIDER_DEFAULTS.get(name)
        if not d:
            return
        self.chain.append({"name": name, "model": model or d["model"],
                           "base_url": (base_url or d["base_url"]).rstrip("/"),
                           "key": key, "dead": False})

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
        """Try every live provider in the chain until one answers."""
        if not self.available:
            return None
        self.history.append({"role": "user", "content": question})
        max_h = int(self.cfg.get("llm", {}).get("max_history", 8))
        self.history = self.history[-max_h * 2:]
        for attempt in (1, 2):
            for e in self.chain:
                if not self._live(e):
                    continue
                try:
                    reply = (self._ask_gemini(e) if e["name"] == "gemini"
                             else self._ask_openai_compatible(e))
                    if reply:
                        if e["name"] != self.provider:
                            print(f"[brain] FAILOVER -> answered by {e['name']}")
                        self.last_used = e["name"]
                        self.history.append({"role": "assistant", "content": reply})
                        return reply
                except urllib.error.HTTPError as err:
                    print(f"[brain] {e['name']}: HTTP {err.code} (attempt {attempt})")
                    if err.code in (400, 401, 403, 404):
                        e["dead"] = True      # auth/model errors won't self-heal
                except Exception as ex:
                    print(f"[brain] {e['name']} (attempt {attempt}): {ex}")
            time.sleep(1.2)
        return None

    def _persona(self):
        return SYSTEM_PERSONA.format(user=self.cfg.get("user_name", "sir"))

    def _ask_gemini(self, e):
        convo = "\n".join(
            ("User: " if m["role"] == "user" else "JARVIS: ") + m["content"]
            for m in self.history)
        url = f"{e['base_url']}/models/{e['model']}:generateContent?key={e['key']}"
        payload = {
            "system_instruction": {"parts": [{"text": self._persona()}]},
            "contents": [{"role": "user", "parts": [{"text": convo}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": 300},
        }
        data = http_post_json(url, payload)
        return (data["candidates"][0]["content"]["parts"][0]["text"] or "").strip()

    def _ask_openai_compatible(self, e):
        headers = {"Authorization": f"Bearer {e['key']}"} if e["key"] else {}
        payload = {
            "model": e["model"],
            "messages": ([{"role": "system", "content": self._persona()}]
                         + self.history),
            "temperature": 0.7,
            "max_tokens": 300,
        }
        url = f"{e['base_url']}/chat/completions"
        data = http_post_json(url, payload, headers=headers)
        return (data["choices"][0]["message"]["content"] or "").strip()


# ============================================================================
#  KNOWLEDGE / INFO SERVICES  (free, no key needed)
# ============================================================================

def wikipedia_summary(topic, sentences=3):
    topic = topic.strip()
    if not topic:
        return None
    title = urllib.parse.quote(topic.replace(" ", "_"))
    try:
        data = http_get_json(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}")
        if data.get("extract"):
            return data["extract"]
    except Exception:
        pass
    # Fallback: search API for the closest page
    try:
        q = urllib.parse.urlencode({
            "action": "opensearch", "search": topic, "limit": 1,
            "namespace": 0, "format": "json"})
        results = http_get_json(f"https://en.wikipedia.org/w/api.php?{q}")
        if len(results) > 1 and results[1]:
            return wikipedia_summary(results[1][0], sentences)
    except Exception:
        pass
    return None


def duckduckgo_answer(query):
    try:
        q = urllib.parse.urlencode({"q": query, "format": "json",
                                    "no_html": 1, "skip_disambig": 1})
        data = http_get_json(f"https://api.duckduckgo.com/?{q}")
        return data.get("AbstractText") or None
    except Exception:
        return None


def get_weather(city):
    try:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
        data = http_get_json(url)
        cur = data["current_condition"][0]
        desc = cur["weatherDesc"][0]["value"]
        area = data.get("nearest_area", [{}])
        place = ""
        if area:
            place = area[0].get("areaName", [{}])[0].get("value", "")
        return (f"Currently in {place or city}: {desc}, {cur['temp_C']}°C "
                f"(feels like {cur['FeelsLikeC']}°C), humidity {cur['humidity']}%, "
                f"wind {cur['windspeedKmph']} km/h.")
    except Exception as e:
        return f"I couldn't fetch the weather right now ({e})."


def get_top_news(country="IN", n=5):
    try:
        url = (f"https://news.google.com/rss?hl=en-{country}&gl={country}"
               f"&ceid={country}:en")
        raw = http_get(url)
        root = ET.fromstring(raw)
        items = root.findall(".//item/title")[:n]
        if not items:
            return None
        return [re.sub(r"\s+-\s+[^-]+$", "", it.text or "") for it in items]
    except Exception:
        return None


def safe_calculate(expr):
    """Safely evaluate a basic arithmetic expression."""
    allowed = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
               ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod, ast.Pow,
               ast.USub, ast.UAdd, ast.FloorDiv, ast.Load)
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise ValueError("unsupported expression")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float)):
            raise ValueError("unsupported constant")
    return eval(compile(tree, "<calc>", "eval"),
                {"__builtins__": {}}, {})


# ============================================================================
#  DATA: websites, apps, jokes
# ============================================================================

WEBSITES = {
    "google": "https://www.google.com", "youtube": "https://www.youtube.com",
    "gmail": "https://mail.google.com", "mail": "https://mail.google.com",
    "github": "https://github.com", "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "whatsapp": "https://web.whatsapp.com", "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com", "twitter": "https://x.com",
    "x": "https://x.com", "linkedin": "https://www.linkedin.com",
    "reddit": "https://www.reddit.com", "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.in", "flipkart": "https://www.flipkart.com",
    "spotify": "https://open.spotify.com", "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com", "drive": "https://drive.google.com",
    "calendar": "https://calendar.google.com", "news": "https://news.google.com",
    "translate": "https://translate.google.com", "chatgpt": "https://chat.openai.com",
    "wikipedia": "https://www.wikipedia.org", "hotstar": "https://www.hotstar.com",
    "prime video": "https://www.primevideo.com", "zomato": "https://www.zomato.com",
    "swiggy": "https://www.swiggy.com", "irctc": "https://www.irctc.co.in",
}

SYSTEM_APPS = {
    "win32": {
        "notepad": "notepad", "calculator": "calc", "paint": "mspaint",
        "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
        "command prompt": "cmd", "terminal": "cmd", "cmd": "cmd",
        "file explorer": "explorer", "explorer": "explorer",
        "settings": "ms-settings:", "task manager": "taskmgr",
        "control panel": "control", "chrome": "chrome",
        "camera": "microsoft.windows.camera:", "clock": "ms-clock:",
    },
    "darwin": {
        "notepad": "TextEdit", "textedit": "TextEdit", "calculator": "Calculator",
        "safari": "Safari", "terminal": "Terminal", "finder": "Finder",
        "calendar": "Calendar", "notes": "Notes", "music": "Music",
        "settings": "System Settings",
    },
    "linux": {
        "terminal": "gnome-terminal", "calculator": "gnome-calculator",
        "files": "nautilus", "file explorer": "nautilus", "text editor": "gedit",
        "notepad": "gedit", "settings": "gnome-control-center",
        "firefox": "firefox", "chrome": "google-chrome",
    },
}

JOKES = [
    "Why do programmers prefer dark mode? Because light attracts bugs.",
    "I told my computer I needed a break... now it won't stop sending me Kit-Kat ads.",
    "Why did the developer go broke? He used up all his cache.",
    "There are only 10 types of people in the world: those who understand binary and those who don't.",
    "I would tell you a UDP joke, but you might not get it.",
    "Why don't scientists trust atoms? Because they make up everything.",
    "Parallel lines have so much in common. Shame they'll never meet.",
    "I'm reading a book about anti-gravity. It's impossible to put down.",
    "Why did the scarecrow win an award? Because he was outstanding in his field.",
    "Artificial intelligence will never beat natural stupidity, sir. Present company excluded, of course.",
]

# ============================================================================
#  JARVIS CORE
# ============================================================================

RUNNING = True
EVENTS = queue.Queue()          # messages from background threads (reminders)


def platform_key():
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


class Jarvis:
    def __init__(self, cfg, force_text=False):
        self.cfg = cfg
        self.name = cfg.get("user_name", "sir")
        self.voice = Voice(cfg)
        self.ears = Ears(cfg, text_only=force_text)
        self.brain = Brain(cfg)
        self.awake = True         # used only when wake-word mode is on
        os.makedirs(MEMORY_DIR, exist_ok=True)
        self.notes = self._load_json(NOTES_PATH, [])
        self.reminders = self._load_json(REMINDERS_PATH, [])
        self._reschedule_reminders()

    # ---------------- persistence helpers ----------------
    @staticmethod
    def _load_json(path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    @staticmethod
    def _save_json(path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[memory] save failed: {e}")

    # ---------------- utilities ----------------
    def say(self, text):
        self.voice.say(text)

    def hear(self):
        return self.ears.listen()

    @staticmethod
    def open_url(url):
        import webbrowser
        try:
            webbrowser.open(url)
            return True
        except Exception:
            return False

    # ---------------- startup ----------------
    def greet(self):
        h = datetime.datetime.now().hour
        if h < 5:
            tod = "working late"
        elif h < 12:
            tod = "Good morning"
        elif h < 17:
            tod = "Good afternoon"
        elif h < 21:
            tod = "Good evening"
        else:
            tod = "Good evening"
        print("=" * 60)
        print("  J.A.R.V.I.S.  —  Just A Rather Very Intelligent System")
        print(f"  v{VERSION}   |   voice: {'ON' if self.voice.enabled else 'OFF'}   "
              f"|   mic: {'ON' if self.ears.ready else 'OFF (typing mode)'}   "
              f"|   AI: {self.brain.status()}")
        print("  Say or type 'help' to see what I can do. 'exit' quits.")
        print("=" * 60)
        if tod == "working late":
            self.say(f"Burning the midnight oil, {self.name}? All systems online.")
        else:
            self.say(f"{tod}, {self.name}. All systems are now online. "
                     f"How may I assist you today?")

    # ---------------- wake word handling ----------------
    def _handle_wake_word(self, text):
        if not self.cfg.get("listen_for_wake_word") or not self.ears.ready:
            return True, text
        wake = self.cfg.get("wake_word", "jarvis").lower()
        low = text.lower()
        if not self.awake:
            if wake in low:
                self.awake = True
                self.say(f"Yes, {self.name}?")
                low = low.split(wake, 1)[1].strip()
                return True, low  # may still contain the actual command
            return False, text
        # awake already; strip a leading wake word if present
        if low.startswith(wake):
            low = low[len(wake):].strip(" ,.")
        return True, low

    # ---------------- main loop ----------------
    def run(self):
        global RUNNING
        self.greet()
        while RUNNING:
            self._drain_events()
            try:
                text = self.hear()
            except KeyboardInterrupt:
                print()
                self.say("Powering down. Goodbye.")
                break
            if not text:
                continue
            proceed, command = self._handle_wake_word(text)
            if not proceed:
                continue
            if not command:
                continue
            try:
                if not self.handle(command):
                    break
            except Exception as e:
                print(f"[error] {e}")
                self.say(f"Apologies {self.name}, I ran into a problem with that request.")
        self.shutdown()

    def _drain_events(self):
        while not EVENTS.empty():
            try:
                self.say(EVENTS.get_nowait())
            except queue.Empty:
                break

    def shutdown(self):
        self.say(f"Shutting down. It's been a pleasure, {self.name}.")
        print("JARVIS offline.")

    # ============================================================================
    #  COMMAND ROUTER
    # ============================================================================

    def handle(self, text):
        """Return False to exit the main loop."""
        global RUNNING
        t = text.lower().strip()
        t = re.sub(r"\s+", " ", t)
        t = re.sub(r"^(please|hey|ok|okay)\s+", "", t)

        # --- exit -------------------------------------------------------------
        if t in ("exit", "quit", "goodbye", "bye", "power down", "shut down jarvis",
                 "shutdown jarvis", "stop jarvis"):
            RUNNING = False
            return False

        if t in ("go to sleep", "sleep", "standby", "stand by"):
            if self.cfg.get("listen_for_wake_word"):
                self.awake = False
                self.say(f"Going to standby. Call me when you need me, {self.name}.")
            else:
                self.say(f"I'm always here, {self.name}. Say 'exit' if you want me to power down.")
            return True

        # --- help -------------------------------------------------------------
        if t in ("help", "commands", "what can you do", "show commands", "menu"):
            self.show_help()
            return True

        # --- identity / small talk -------------------------------------------
        if re.search(r"\b(who are you|your name|about yourself)\b", t):
            self.say("I am JARVIS — Just A Rather Very Intelligent System. "
                     f"Your personal assistant, at your service, {self.name}.")
            return True
        if re.search(r"\b(how are you|how's it going|how are things)\b", t):
            self.say(f"All systems running at optimal capacity, {self.name}. "
                     "Thank you for asking. How can I help?")
            return True
        if re.search(r"\bwhat (are|r) (you|u) (doing|upto|up to)\b|\bwhat'?s up\b", t):
            self.say(f"Monitoring your systems, polishing the arc reactor, and awaiting your command, {self.name}.")
            return True
        if re.search(r"\b(thank you|thanks|thanked)\b", t):
            self.say(f"Always at your service, {self.name}.")
            return True
        if re.search(r"\b(who (made|created|built|designed) you|your (creator|maker|developer))\b", t):
            self.say(f"I was assembled for you, {self.name} — your very own JARVIS.")
            return True
        if re.search(r"\bi love you\b", t):
            self.say(f"Most kind, {self.name}. I shall endeavour to remain worthy of the sentiment.")
            return True
        if t in ("hello", "hi", "hey", "hello jarvis", "hi jarvis", "greetings"):
            self.say(f"Hello, {self.name}. What can I do for you?")
            return True

        # --- time & date ------------------------------------------------------
        if re.search(r"\b(what('s| is)?|tell me|current)\b.*\btime\b", t) or t == "time":
            now = datetime.datetime.now()
            self.say(f"It's {now.strftime('%I:%M %p')}, {self.name}.")
            return True
        if re.search(r"\b(what('s| is)?|tell me|today('s| is)?)\b.*\bdate\b", t) or t in ("date", "today"):
            now = datetime.datetime.now()
            self.say(f"Today is {now.strftime('%A, %B %d, %Y')}.")
            return True
        if re.search(r"\bwhat day\b", t):
            self.say(f"It's {datetime.datetime.now().strftime('%A')}, {self.name}.")
            return True

        # --- calculations -----------------------------------------------------
        m = re.match(r"(?:calculate|compute|what is|what's|eval|maths?|solve)\s+([\d\.,\s\+\-\*\/\%\(\)\^]+)$", t)
        if m:
            expr = m.group(1).replace("^", "**").replace(",", "").strip()
            try:
                result = safe_calculate(expr)
                result = round(result, 6) if isinstance(result, float) else result
                self.say(f"That comes to {result}, {self.name}.")
            except Exception:
                self.say("I couldn't compute that expression.")
            return True

        # --- fun --------------------------------------------------------------
        if re.search(r"\b(tell me a joke|joke|make me laugh)\b", t):
            self.say(random.choice(JOKES))
            return True
        if re.search(r"\bflip a coin\b", t):
            self.say(random.choice(["Heads.", "Tails."]) + f" As you wished, {self.name}.")
            return True
        if re.search(r"\broll (a |the )?(dice|die)\b", t):
            self.say(f"The die shows {random.randint(1, 6)}.")
            return True

        # --- reminders --------------------------------------------------------
        if re.search(r"^remind me", t):
            return self.cmd_reminder(t)
        if re.search(r"\b(my reminders|list reminders|show reminders)\b", t):
            return self.cmd_list_reminders()

        # --- notes ------------------------------------------------------------
        if re.search(r"^(take a note|make a note|add note|note this|write this down|remember this)[:\s]*", t):
            body = re.sub(r"^(take a note|make a note|add note|note this|write this down|remember this)[:\s]*", "", text, flags=re.I).strip()
            return self.cmd_add_note(body)
        m = re.match(r"note[:\s]+(.+)", text, flags=re.I)
        if m:
            return self.cmd_add_note(m.group(1))
        if re.search(r"\b(read|show|list) (my )?notes\b", t):
            return self.cmd_read_notes()
        if re.search(r"\b(clear|delete) (all )?(my )?notes\b", t):
            self.notes = []
            self._save_json(NOTES_PATH, self.notes)
            self.say("All notes erased.")
            return True

        # --- weather ----------------------------------------------------------
        m = re.search(r"\bweather(?:\s+(?:in|at|for)\s+(.+))?", t)
        if m:
            city = (m.group(1) or self.cfg.get("city", "Chennai")).strip()
            self.say(f"Checking the skies over {city}...")
            self.say(get_weather(city))
            return True

        # --- news -------------------------------------------------------------
        if re.search(r"\b(news|headlines|top stories|what's happening)\b", t):
            self.say("Fetching the latest headlines...")
            headlines = get_top_news()
            if headlines:
                self.say("Here are the top stories:")
                for i, h in enumerate(headlines, 1):
                    self.say(f"{i}. {h}")
            else:
                self.say("I couldn't reach the news feed right now.")
            return True

        # --- wikipedia / knowledge -------------------------------------------
        m = re.match(r"(?:wikipedia|wiki)\s+(.+)", t)
        if m:
            return self.cmd_wikipedia(m.group(1))
        m = re.match(r"(?:tell me (?:something )?about|know about|information (?:about|on)|explain)\s+(.+?)\??$", t)
        if m and len(m.group(1).split()) <= 8:
            return self.cmd_wikipedia(m.group(1))
        m = re.match(r"(?:who is|who's|who was|what is|what's a|whats a|who are)\s+(.+?)\??$", t)
        if m and len(m.group(1).split()) <= 8:
            topic = re.sub(r"\?$", "", m.group(1))
            return self.cmd_wikipedia(topic)

        # --- music ------------------------------------------------------------
        m = re.match(r"(?:play|put on)\s+(?:the song\s+|some\s+music\s+)?(.+?)(?:\s+on youtube)?$", text, flags=re.I)
        if m and t.startswith(("play", "put on")):
            return self.cmd_play(m.group(1))
        if t in ("play music", "play some music"):
            return self.cmd_play("top hits")

        # --- search -----------------------------------------------------------
        m = re.match(r"(?:search|google|look up|search for|google for)\s+(?:for\s+)?(.+)", text, flags=re.I)
        if m:
            query = m.group(1).strip()
            self.say(f"Searching for {query}.")
            self.open_url("https://www.google.com/search?q=" + urllib.parse.quote(query))
            return True

        # --- screenshots ------------------------------------------------------
        if re.search(r"\b(screenshot|screen shot|capture (the )?screen)\b", t):
            return self.cmd_screenshot()

        # --- volume -----------------------------------------------------------
        m = re.search(r"\bvolume (up|down|mute|max)\b", t)
        if m:
            return self.cmd_volume(m.group(1))

        # --- battery / system -------------------------------------------------
        if re.search(r"\bbattery\b", t):
            return self.cmd_battery()
        if re.search(r"\b(system (info|status)|cpu|memory usage|ram|performance)\b", t):
            return self.cmd_system_info()

        # --- open websites & apps --------------------------------------------
        m = re.match(r"open (?:the )?(.+)", t)
        if m:
            target = m.group(1).strip()
            return self.cmd_open(target)

        # --- AI / fallback ----------------------------------------------------
        reply = self.brain.answer(text)
        if reply:
            self.say(clean_for_speech(reply))
            return True

        ans = wikipedia_summary(text) or duckduckgo_answer(text)
        if ans:
            self.say(ans[:450])
        else:
            self.say(f"I'm not sure how to help with that yet, {self.name}. "
                     "Try 'help' to see my commands, or add an AI API key "
                     "in config.json so I can answer anything.")
        return True

    # ============================================================================
    #  FEATURE IMPLEMENTATIONS
    # ============================================================================

    def show_help(self):
        help_text = """
╔═══════════════════════════════ MY CAPABILITIES ═══════════════════════════════╗
  INFORMATION         SYSTEM & APPS              PRODUCTIVITY
  • weather [in X]    • open youtube / notepad   • remind me to X in 10 minutes
  • news              • open chrome / settings   • remind me to X at 6:30 pm
  • who is / what is  • screenshot               • my reminders
  • wikipedia <topic> • volume up / down / mute  • note buy milk tomorrow
  • search <anything> • battery / system info    • read my notes / clear notes
  FUN                 MUSIC                      CONTROL
  • tell me a joke    • play <song name>         • help
  • flip a coin       • play <song> on youtube   • go to sleep (wake-word mode)
  • roll a dice                                    • exit / goodbye
  • calculate 45 * 12 + 5
  ...and anything else — I'll answer with AI if an API key is configured.
╚═══════════════════════════════════════════════════════════════════════════════╝
"""
        print(help_text)
        self.say("Here's what I can do for you. I've printed the full list on screen.")

    # ---- reminders ----
    def cmd_reminder(self, t):
        original = t
        m = re.match(r"remind me to (.+?) in (\d+)\s*(seconds?|minutes?|hours?)", t)
        if m:
            task, amount, unit = m.group(1), int(m.group(2)), m.group(3)
            seconds = amount * (1 if unit.startswith("sec") else
                                60 if unit.startswith("min") else 3600)
            when = time.time() + seconds
            return self._schedule_reminder(task, when,
                                           f"Reminder set: {task} — in {amount} {unit}.")
        m = re.match(r"remind me to (.+?) at (\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?$", t)
        if m:
            task, hh, mm, ap = m.group(1), int(m.group(2)), int(m.group(3) or 0), m.group(4)
            if ap and ap.startswith("p") and hh < 12:
                hh += 12
            if ap and ap.startswith("a") and hh == 12:
                hh = 0
            now = datetime.datetime.now()
            when_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if when_dt <= now:
                when_dt += datetime.timedelta(days=1)
            return self._schedule_reminder(
                task, when_dt.timestamp(),
                f"Reminder set: {task} — at {when_dt.strftime('%I:%M %p')}.")
        m = re.match(r"remind me in (\d+)\s*(seconds?|minutes?|hours?) to (.+)", t)
        if m:
            amount, unit, task = int(m.group(1)), m.group(2), m.group(3)
            seconds = amount * (1 if unit.startswith("sec") else
                                60 if unit.startswith("min") else 3600)
            return self._schedule_reminder(task, time.time() + seconds,
                                           f"Reminder set: {task} — in {amount} {unit}.")
        task = re.sub(r"^remind me (to )?", "", original).strip()
        if not task:
            self.say("Remind you of what, exactly?")
            return True
        self.say("When should I remind you? Say something like 'in 10 minutes' or 'at 6 pm'.")
        when_text = self.hear().lower()
        if when_text:
            return self.cmd_reminder(f"remind me to {task} {when_text}")
        self.say("I didn't catch the time, so I cancelled that reminder.")
        return True

    def _schedule_reminder(self, task, when_epoch, confirm):
        entry = {"task": task, "when": when_epoch}
        self.reminders.append(entry)
        self._save_json(REMINDERS_PATH, self.reminders)
        delay = max(0.5, when_epoch - time.time())
        timer = threading.Timer(delay, self._fire_reminder, args=(entry,))
        timer.daemon = True
        timer.start()
        self.say(confirm)
        return True

    def _fire_reminder(self, entry):
        try:
            self.reminders.remove(entry)
            self._save_json(REMINDERS_PATH, self.reminders)
        except ValueError:
            pass
        EVENTS.put(f"Reminder for you, {self.name}: {entry['task']}.")

    def _reschedule_reminders(self):
        now = time.time()
        keep = []
        for entry in self.reminders:
            if entry.get("when", 0) > now:
                keep.append(entry)
                delay = entry["when"] - now
                timer = threading.Timer(delay, self._fire_reminder, args=(entry,))
                timer.daemon = True
                timer.start()
        if len(keep) != len(self.reminders):
            self.reminders = keep
            self._save_json(REMINDERS_PATH, keep)

    def cmd_list_reminders(self):
        if not self.reminders:
            self.say("You have no pending reminders.")
            return True
        self.say(f"You have {len(self.reminders)} reminder(s):")
        for entry in sorted(self.reminders, key=lambda e: e["when"]):
            when = datetime.datetime.fromtimestamp(entry["when"])
            self.say(f"{entry['task']} — {when.strftime('%I:%M %p, %b %d')}")
        return True

    # ---- notes ----
    def cmd_add_note(self, body):
        if not body:
            self.say("What would you like me to note down?")
            body = self.hear()
            if not body:
                self.say("Nothing noted.")
                return True
        self.notes.append({"text": body,
                           "time": datetime.datetime.now().isoformat(timespec="seconds")})
        self._save_json(NOTES_PATH, self.notes)
        self.say("Noted.")
        return True

    def cmd_read_notes(self):
        if not self.notes:
            self.say("You have no notes yet, " + self.name + ".")
            return True
        self.say(f"You have {len(self.notes)} note(s):")
        for i, n in enumerate(self.notes, 1):
            self.say(f"{i}. {n['text']}")
        return True

    # ---- knowledge ----
    def cmd_wikipedia(self, topic):
        topic = topic.strip()
        self.say(f"Checking my archives on {topic}...")
        summary = wikipedia_summary(topic)
        if summary:
            self.say(summary[:450])
        else:
            ans = duckduckgo_answer(topic)
            if ans:
                self.say(ans[:450])
            else:
                self.say(f"I couldn't find anything on '{topic}'.")
        return True

    # ---- music ----
    def cmd_play(self, song):
        song = song.strip()
        self.say(f"Playing {song} on YouTube.")
        if pywhatkit:
            try:
                pywhatkit.playonyt(song)
                return True
            except Exception as e:
                print(f"[music] pywhatkit failed: {e}")
        self.open_url("https://www.youtube.com/results?search_query=" +
                      urllib.parse.quote(song))
        return True

    # ---- system ----
    def cmd_screenshot(self):
        folder = os.path.join(os.path.expanduser("~"), "Desktop", "JARVIS_Screenshots")
        os.makedirs(folder, exist_ok=True)
        fname = os.path.join(folder,
                             "screenshot_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".png")
        if pyautogui:
            try:
                pyautogui.screenshot(fname)
                self.say(f"Screenshot saved to {fname}")
                return True
            except Exception as e:
                print(f"[screenshot] pyautogui failed: {e}")
        # fallbacks
        cmd = {"win32": None, "darwin": ["screencapture", fname],
               "linux": ["gnome-screenshot", "-f", fname]}.get(platform_key())
        if cmd:
            try:
                subprocess.run(cmd, check=True, timeout=20)
                self.say(f"Screenshot saved to {fname}")
                return True
            except Exception as e:
                print(f"[screenshot] {e}")
        self.say("I need the pyautogui library for that. Run: pip install pyautogui")
        return True

    def cmd_volume(self, action):
        presses = {"up": ("volumeup", 5), "down": ("volumedown", 5),
                   "max": ("volumeup", 25), "mute": ("volumemute", 1)}[action]
        if pyautogui:
            key, n = presses
            try:
                for _ in range(n):
                    pyautogui.press(key)
                self.say({"up": "Volume raised.", "down": "Volume lowered.",
                          "max": "Volume at maximum.", "mute": "Volume muted."}[action])
                return True
            except Exception as e:
                print(f"[volume] {e}")
        # linux fallback
        if platform_key() == "linux" and shutil.which("amixer"):
            sub = {"up": "5%+", "down": "5%-", "max": "100%", "mute": "toggle"}[action]
            subprocess.run(["amixer", "-q", "sset", "Master", sub])
            self.say("Done.")
            return True
        self.say("Volume control needs pyautogui: pip install pyautogui")
        return True

    def cmd_battery(self):
        if psutil:
            b = psutil.sensors_battery()
            if b is None:
                self.say("I can't detect a battery on this machine — perhaps it's a desktop.")
                return True
            state = "charging" if b.power_plugged else "not charging"
            mins = "unknown"
            if b.secsleft not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN):
                mins = str(round(b.secsleft / 60))
            self.say(f"Battery is at {round(b.percent)}%, {state}. "
                     + (f"About {mins} minutes remaining." if mins != "unknown" else ""))
            return True
        self.say("Install psutil for battery status: pip install psutil")
        return True

    def cmd_system_info(self):
        if not psutil:
            self.say("Install psutil for system monitoring: pip install psutil")
            return True
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage(os.path.abspath(os.sep))
        self.say(f"CPU load is {cpu}%. Memory usage {mem.percent}% "
                 f"({round(mem.used / 1e9, 1)} of {round(mem.total / 1e9, 1)} gigabytes). "
                 f"Disk is {disk.percent}% full.")
        return True

    # ---- open apps / sites ----
    def cmd_open(self, target):
        custom = self.cfg.get("custom_apps", {})
        if target in custom:
            return self._launch(custom[target], target)

        # website names first: "open youtube" should go to the site
        clean = re.sub(r"\s+(website|site|app|application)$", "", target)
        if clean in WEBSITES:
            self.say(f"Opening {clean}.")
            self.open_url(WEBSITES[clean])
            return True

        apps = SYSTEM_APPS.get(platform_key(), {})
        if clean in apps:
            return self._launch(apps[clean], clean)

        # maybe an explicit URL
        if re.match(r"^[\w\-]+(\.[\w\-]+)+(/\S*)?$", clean):
            self.open_url("https://" + clean)
            self.say(f"Opening {clean}.")
            return True

        # try a system binary on PATH
        binary = clean.replace(" ", "")
        if shutil.which(binary):
            return self._launch(binary, clean)

        self.say(f"I don't know how to open '{target}' yet. "
                 "You can teach me by adding it under custom_apps in config.json.")
        return True

    def _launch(self, command, label):
        plat = platform_key()
        try:
            if re.match(r"^https?://", str(command)):
                self.open_url(command)
            elif plat == "win32":
                if os.path.exists(command):
                    os.startfile(command)  # type: ignore[attr-defined]
                elif str(command).endswith(":"):
                    os.startfile(command)  # type: ignore[attr-defined]  # ms-settings: URIs
                else:
                    subprocess.Popen(f'start "" {command}', shell=True)
            elif plat == "darwin":
                if os.path.exists(command) or command.startswith("/"):
                    subprocess.Popen(["open", command])
                else:
                    subprocess.Popen(["open", "-a", command])
            else:
                subprocess.Popen(shlex_split(command),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.say(f"Opening {label}, {self.name}.")
        except Exception as e:
            print(f"[open] {e}")
            self.say(f"I wasn't able to open {label}.")
        return True


def shlex_split(cmd):
    import shlex
    return shlex.split(str(cmd))


# ============================================================================
#  ENTRY POINT
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="J.A.R.V.I.S. — personal AI assistant")
    parser.add_argument("--text", action="store_true",
                        help="force text-only mode (no microphone)")
    parser.add_argument("--voice", action="store_true",
                        help="force voice mode (fail loudly if unavailable)")
    args = parser.parse_args()

    cfg = load_config()
    jarvis = Jarvis(cfg, force_text=args.text)
    if args.voice and not jarvis.ears.ready:
        print("[main] Voice mode requested but microphone/speech libs unavailable.")
        print("       pip install pyttsx3 SpeechRecognition PyAudio")
        sys.exit(1)
    jarvis.run()


if __name__ == "__main__":
    main()
