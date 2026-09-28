# 2.1.1 — Web conversation reliability

Built on SENTINEL without replacing its desktop features, memory, services or telemetry.

- Cache keys now include session, exact conversation, question and provider configuration. An identical follow-up in a different context no longer reuses an unrelated answer.
- Every web command is recorded once in the bounded conversation history, including local commands and cached replies. Repeat now works after local commands.
- General questions retain their full wording; questions about time dilation, date formats and weather concepts no longer get intercepted by simple time/weather commands. General AI failure is reported honestly; explicit Wikipedia lookup remains available.
- Full AI replies remain visible instead of being cut to 400 characters.
- Primary provider accepts its named environment key, including GEMINI_API_KEY.
- Provider/cache labels describe the current request, not the preceding one.
- API rejects blank/oversized commands and invalid session identifiers. Failures return HTTP 503; missing paths return JSON 404. Failed memory writes no longer claim success.
- Invalid reminder clock times are rejected. Wikipedia fallback recursion is bounded.
- Top-bar connection settings remain available on small screens. Connection addresses and action links are validated; URL query parameters no longer silently redirect conversations to another server.

## Verification

Offline Python regression suite, FastAPI in-process endpoint tests, JavaScript controller tests and syntax checks are included in CI. Provider responses are mocked: these tests do not prove live AI quality, microphone behavior or production deployment.

## Known boundaries and next steps

This is still a single-owner prototype, not a secure public multi-user service. Long-term memory, notes, reminders and server-configured email access are shared; browser session identifiers are not authentication. Do not expose personal data without an authenticated access layer. Durable hosted storage, acknowledged reminders and fuller voice lifecycle changes remain planned. Free/ephemeral hosting can lose files on restart or redeploy. AI answers still require a valid configured provider/model.

Existing Render/Netlify integrations may deploy after a main-branch push if connected. A repository configuration file alone does not confirm a successful live deployment.
