# J.A.R.V.I.S. — Just A Rather Very Intelligent System 🎙️

Your personal AI assistant — **two bodies, one brain**:

- 🖥️ **Desktop app** — full voice control (mic in / voice out), opens apps, screenshots, volume, battery…
- 🌐 **Web app** — Iron-Man-style HUD running in any browser, deployable in one click, browser voice built-in.

```
        ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗
        ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝
        ██║███████║██████╔╝██║   ██║██║███████╗
   ██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║
   ╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║
    ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝
```

---

## ✨ Features

| Category | Desktop 🖥️ | Web 🌐 |
|---|---|---|
| Voice interaction (talk 👂 listen 🔊) | ✅ system mic & speakers | ✅ browser Web Speech API |
| AI answers (Gemini / OpenAI / Groq / Ollama) | ✅ | ✅ |
| Weather, news, Wikipedia, web search | ✅ | ✅ |
| Reminders & notes | ✅ (survive restarts) | ✅ (server-side) |
| Music on YouTube | ✅ autoplay | ✅ opens player tab |
| Open apps, screenshots, volume, battery | ✅ | — (browser sandbox) |
| Open websites, downloads | ✅ | ✅ |
| Works with zero dependencies (text mode) | ✅ | ✅ (stdlib brain) |

---

## 🌐 WEB APP — deployment (both free, both auto-deploy on `git push`)

The web app has two halves: **FACE** 🎭 (static HUD in `web/static`) + **BRAIN** 🧠 (Python API in `web/`).
⚠️ **Netlify cannot run the Python brain** — it hosts static files only and CANNOT compile desktop libs
(PyAudio etc.). So the brain always lives on a Python host. Two supported layouts:

### 🅱️ Option B — Netlify (face) + Render (brain)  ★ recommended
1. **Netlify** → *Add new site → Import from Git* → pick **`jarvis`** → Deploy.
   `netlify.toml` does the rest automatically: publishes **only `web/static`** and skips all Python
   dependency installation → build is seconds long and always green. ✅
2. **Render** → deploy the brain as in *Option A* below → copy your brain URL
   `https://jarvis-xxxx.onrender.com`.
3. **Link once** (saved forever in the browser): open your Netlify site → tap **⚙️** in the bottom bar →
   paste the Render URL. (Or open the site once with `?api=https://jarvis-xxxx.onrender.com`.)
   CORS is enabled on the brain, so the Netlify face can call it from anywhere. 🔓
4. Push to GitHub → **face updates on Netlify + brain updates on Render**. Fully hands-free. ♻️

### 🅰️ Option A — all-in-one on Render
The same blueprint ALSO serves the HUD — one URL for face+brain, no linking step needed.

### First deploy on Render (≈ 3 minutes)
1. Go to **[render.com](https://render.com)** → **Sign up / Log in with GitHub**
2. Dashboard → **New +** → **Blueprint**
3. Select the **`jarvis`** repository → **Apply** (Render reads `render.yaml` and builds everything)
4. When deploy finishes, open your URL: `https://jarvis-xxxx.onrender.com` 🎉

### Unlock the AI brain(s) on the web app (important!)
In Render dashboard → your service → **Environment** → add:

| Key | Value | Role |
|---|---|---|
| `JARVIS_API_KEY` | your Gemini key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | 🧠 primary brain |
| `GROQ_API_KEY` | your Groq key from [console.groq.com/keys](https://console.groq.com/keys) | 🛟 backup brain #1 |
| `OPENAI_API_KEY` | *(optional)* OpenAI key | 🛟 backup brain #2 |
| `USER_NAME` | what JARVIS calls you — `sir` / your name | 🎩 persona |

Then **Save Changes** (auto-redeploys). Keys live only on the server — never in Git. 🔒

### 🔁 Automatic AI failover (built-in)
JARVIS keeps a **chain of brains**: Gemini → Groq → OpenAI (any with a key joins automatically).
If the primary API errors, rate-limits or goes down, the next brain **answers the same request instantly** — you never see a failure. Watch the status pill in the HUD (`GEMINI`, `GROQ`, `GEMINI+GROQ`)

to see which brains are live, and check Render logs for `FAILOVER -> answered by ...` events.

### Upgrading later (the easy loop you wanted)
```bash
git add -A && git commit -m "upgrade" && git push
```
Render sees the push and redeploys automatically. Nothing else to do.

### Run the web app locally
```bash
cd web
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000   (use Chrome/Edge for the microphone)
```

> 💤 **Free-tier note:** Render's free plan sleeps after ~15 min idle — first visit after a nap takes ~30s to wake, and reminders fire only while the server is awake / tab open.

---

## 🖥️ DESKTOP APP — quick start

**Requirements:** Python 3.9+

```bash
# Windows: double-click setup.bat      macOS/Linux: ./setup.sh
python jarvis.py            # voice mode (installs handled by setup script)
python jarvis.py --text     # text-only, zero dependencies, always works
```

One-click launchers afterwards: `start_jarvis.bat` / `start_jarvis.sh`.

### Talk to JARVIS — examples
```
what time is it                  open youtube / notepad
tell me a joke                   volume up · battery · screenshot
weather   ·   weather in Mumbai  system info
news                             play Shape of You
who is Elon Musk                 calculate 45 * 12 + 5
tell me about the Taj Mahal      note buy groceries tomorrow
remind me to call mom in 20 minutes
search best laptops 2026         help    ·    exit
```

### 🧠 Give it AI brains (with automatic failover)
`config.json` (created on first run):
```jsonc
"llm": {
  "provider": "gemini",
  "api_key":  "GEMINI-KEY",                 // 🧠 primary
  "extra_keys": { "groq": "GROQ-KEY",       // 🛟 automatic backups — join the
                  "openai": "" },           //     chain if the primary fails
}
```
Free keys: Gemini → [aistudio.google.com/apikey](https://aistudio.google.com/apikey),
Groq → [console.groq.com/keys](https://console.groq.com/keys).
Also supported: env vars `JARVIS_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`,
or fully-offline `ollama` as provider. The startup banner shows the live chain, e.g. `AI: gemini+groq`.

### ⚙️ Personalize (`config.json`)
```jsonc
{
  "user_name": "sir",             // how JARVIS addresses you
  "city": "Chennai",              // default weather city
  "stt_language": "en-IN",        // recognition accent/locale
  "listen_for_wake_word": false,  // true → sleeps until you say "jarvis" 🎬
  "custom_apps": { "vs code": "code" }   // teach it your apps
}
```

---

## 📁 Repository structure

```
jarvis/
├── jarvis.py                 🖥️  desktop assistant (single file, stdlib core)
├── config.json               🔒  local settings + keys — GIT-IGNORED
├── requirements.txt          🖥️  desktop deps (all optional; text mode needs none)
├── setup.bat / setup.sh      ⚙️  one-click installers
├── start_jarvis.bat / .sh    🚀  launchers
├── render.yaml               ☁️  Render blueprint (auto-deploy on push)
├── memory/                   🧠  notes & reminders (git-ignored)
└── web/                      🌐  the web app
    ├── main.py               FastAPI server (command API + static HUD)
    ├── brain.py              web command brain (stdlib only)
    ├── requirements.txt      fastapi + uvicorn
    └── static/index.html     Arc-reactor HUD (Web Speech API, zero CDN)
```

## 🔒 Security notes
- `config.json`, `.env`, and `memory/` are in `.gitignore` — API keys **never** reach GitHub.
- On Render, keys are environment variables visible only to you.

*Now go say "hello" to your new assistant, sir.* 🫡
