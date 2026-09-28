# JARVIS v2.2 — Studio and connected actions

v2.2.1 corrects a clock-intent routing bug found in the live smoke check: “what time is it” now uses the local clock instead of asking an AI provider.

This release adds a protected owner mode to the web assistant. Set `JARVIS_ACCESS_TOKEN` on the Render service, then enter the same code through **Tools → Owner access** in your browser. Without a configured code, command and action APIs intentionally refuse requests. Do not put the code in GitHub or share it. The browser saves it locally; avoid shared devices.

**App Studio** creates a small static HTML/CSS/JavaScript project from a written brief and downloads a ZIP for review. Generated code is never run by the JARVIS server. This is a bounded first step, not a promise to build every kind of application. Publishing a new GitHub repository or Render service is not yet built into JARVIS and needs separate, scoped credentials and a review step.

**Email** sends a message only after the owner fills in the recipient, subject and body and presses Send. The server needs `EMAIL_USER` and `EMAIL_APP_PASSWORD` configured. A successful API response means SMTP accepted the message, not that the recipient read it. Mail setup or delivery was not verified against a real account during development.

**Personal WhatsApp** prepares one reply from a pasted incoming message and opens a prefilled WhatsApp link. The owner reviews and sends it. It cannot read conversations, impersonate the owner in the background, or auto-reply from a personal account. Official automated messaging would require a separately approved WhatsApp Business setup and safeguards.

Also included: browser-local owner controls, session-scoped notes/memory, timezone-aware scheduled reminders, persistent reminders delivered while the web tab is open, and regression tests. Pre-v2.2 note/memory records without session identifiers remain visible to the authenticated owner to preserve existing data. Reminders rely on the browser being open and do not send push notifications.
