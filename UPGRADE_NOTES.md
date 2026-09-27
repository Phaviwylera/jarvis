# JARVIS v2 experience upgrade

## What changed

- Rebuilt the web interface as a responsive command center with a focused reactor,
  readable activity stream, system telemetry, quick commands, and mobile layout.
- Removed the forced boot and activation overlays. Text input is ready immediately;
  voice activates only when the user requests it.
- Added direct voice mode and an optional continuous wake-word mode.
- Added response latency and active-provider status to the interface.
- Added a stable browser session identifier and isolated AI conversation history by
  session instead of sharing one global history across every visitor.
- Open-ended factual questions now prefer the configured AI provider, with
  Wikipedia and DuckDuckGo retained as offline fallbacks.
- Rewrote the assistant prompt around accuracy, clarity, honest capability claims,
  and natural concise speech.
- Applied the same accuracy-first persona and routing improvements to the desktop
  assistant.

## Deployment

Push these files to the repository's `main` branch. The existing Render and
Netlify configurations will redeploy automatically. Configure at least one AI key
on Render; without a key, deterministic commands still work but general answers
remain limited to public lookup fallbacks.

## Verification completed

- Python syntax compilation for desktop and web modules
- Browser JavaScript syntax validation
- Live FastAPI health and command requests
- Desktop-size visual inspection
- End-to-end browser command test (`calculate 45 * 12 + 5`)
