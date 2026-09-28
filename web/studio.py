"""Small web-app project builder. Generated code is packaged, never executed here."""

import io
import json
import re
import zipfile

import brain

MAX_FILES = 15
MAX_TOTAL_CHARS = 120_000
ALLOWED = re.compile(r"^(?:[a-zA-Z0-9_-]+/){0,2}[a-zA-Z0-9_-]+\.(?:html|css|js|json|md)$")


class StudioError(Exception):
    pass


def _model_reply(prompt):
    instructions = (
        "You are a web application engineer. Build a complete, small static browser app. "
        "Return only a JSON object with keys name, summary, files. files is a list of "
        "objects with path and content. Include index.html. Use local HTML, CSS and "
        "JavaScript; no backend, API keys, external scripts, tracking, or payment flows. "
        "Keep the app functional and accessible. User request: " + prompt
    )
    for provider in brain.AI.chain:
        if not brain.AI._live(provider):
            continue
        try:
            if provider["name"] == "gemini":
                data = brain.http_post_json(
                    f"{provider['base_url']}/models/{provider['model']}:generateContent?key={provider['key']}",
                    {"contents": [{"role": "user", "parts": [{"text": instructions}]}],
                     "generationConfig": {"temperature": 0.25, "maxOutputTokens": 8192,
                                          "responseMimeType": "application/json"}}, timeout=55)
                return data["candidates"][0]["content"]["parts"][0]["text"]
            headers = {"Authorization": f"Bearer {provider['key']}"} if provider["key"] else {}
            data = brain.http_post_json(
                f"{provider['base_url']}/chat/completions",
                {"model": provider["model"],
                 "messages": [{"role": "user", "content": instructions}],
                 "temperature": 0.25, "max_tokens": 6000}, headers=headers, timeout=55)
            return data["choices"][0]["message"]["content"]
        except Exception:
            continue
    raise StudioError("No configured AI provider could build this project right now.")


def build_project(prompt):
    if not 10 <= len(prompt.strip()) <= 2000:
        raise StudioError("Describe the app in 10 to 2000 characters.")
    raw = _model_reply(prompt.strip()).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
    try:
        project = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise StudioError("The AI returned an incomplete project. Please try a smaller request.") from exc
    if not isinstance(project, dict) or not isinstance(project.get("files"), list):
        raise StudioError("The AI returned an invalid project.")
    files = project["files"]
    if not 1 <= len(files) <= MAX_FILES:
        raise StudioError("The project exceeded the file limit. Ask for a smaller app.")
    names, total = set(), 0
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("content"), str):
            raise StudioError("The project contains an invalid file.")
        path = item["path"]
        if not ALLOWED.fullmatch(path) or path in names:
            raise StudioError("The project contains an unsupported or duplicate file path.")
        names.add(path)
        total += len(item["content"])
        if total > MAX_TOTAL_CHARS:
            raise StudioError("The project is too large. Ask for a smaller app.")
    if "index.html" not in names:
        raise StudioError("The project needs an index.html entry point.")
    name = re.sub(r"[^a-z0-9-]+", "-", str(project.get("name") or "jarvis-app").lower()).strip("-")[:50] or "jarvis-app"
    summary = str(project.get("summary") or "Generated web app")[:300]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            archive.writestr(item["path"], item["content"])
        archive.writestr("README.md", f"# {name}\n\n{summary}\n\nOpen index.html in a browser. Review generated code before publishing.\n")
    return name, output.getvalue(), len(files)
