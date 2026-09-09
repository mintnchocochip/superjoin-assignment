"""Ask a model for JSON. One function, four interchangeable backends.

Pick one with LLM_BACKEND, or set an API key and let it choose:

    claude      the Claude Code CLI on this machine  (no API key, default)
    groq        GROQ_API_KEY        free tier
    openrouter  OPENROUTER_API_KEY  free tier (":free" models)
    gemini      GEMINI_API_KEY      free tier
"""

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request

BACKEND = os.getenv("LLM_BACKEND") or (
    "groq" if os.getenv("GROQ_API_KEY")
    else "openrouter" if os.getenv("OPENROUTER_API_KEY")
    else "gemini" if os.getenv("GEMINI_API_KEY")
    else "claude"
)

MODEL = os.getenv("LLM_MODEL") or {
    "claude": "claude-haiku-4-5-20251001",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
    "gemini": "gemini-2.0-flash",
}.get(BACKEND, "")

TIMEOUT = int(os.getenv("LLM_TIMEOUT", "300"))


class LLMError(RuntimeError):
    pass


def ask_json(prompt, fallback):
    """Send prompt, return parsed JSON. Never raises: on failure returns
    (fallback, error string) so one bad page cannot kill a whole document."""
    try:
        raw = _send(prompt)
    except Exception as exc:  # network, CLI, timeout - all recoverable per call
        return fallback, f"{BACKEND}: {exc}"
    parsed = _parse_json(raw)
    if parsed is None:
        return fallback, f"unparseable model output: {raw[:200]}"
    return parsed, None


def _send(prompt):
    if BACKEND == "claude":
        return _claude_cli(prompt)
    if BACKEND == "gemini":
        return _gemini(prompt)
    return _openai_compatible(prompt)


def _claude_cli(prompt):
    exe = shutil.which("claude")
    if not exe:
        raise LLMError("claude CLI not on PATH - install Claude Code or set an API key")
    done = subprocess.run(
        [exe, "-p", prompt, "--model", MODEL],
        capture_output=True, text=True, timeout=TIMEOUT,
        # Windows would otherwise decode the CLI's UTF-8 as cp1252 and turn every
        # rupee sign in a quote into mojibake.
        encoding="utf-8", errors="replace",
    )
    if done.returncode != 0:
        raise LLMError((done.stderr or done.stdout or "claude CLI failed").strip()[:300])
    return done.stdout


def _openai_compatible(prompt):
    url, key = {
        "groq": ("https://api.groq.com/openai/v1/chat/completions", os.getenv("GROQ_API_KEY")),
        "openrouter": ("https://openrouter.ai/api/v1/chat/completions", os.getenv("OPENROUTER_API_KEY")),
    }[BACKEND]
    body = {
        "model": MODEL,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = _post(url, body, {"Authorization": f"Bearer {key}"})
    return data["choices"][0]["message"]["content"]


def _gemini(prompt):
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}"
           f":generateContent?key={os.getenv('GEMINI_API_KEY')}")
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0}}
    data = _post(url, body, {})
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _post(url, body, headers):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise LLMError(f"HTTP {exc.code}: {exc.read()[:200].decode(errors='replace')}") from None


def _parse_json(raw):
    """Models like to wrap JSON in prose and code fences. Dig it out."""
    text = re.sub(r"```(?:json)?|```", "", raw).strip()
    for candidate in (text, _slice(text, "[", "]"), _slice(text, "{", "}")):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _slice(text, open_ch, close_ch):
    start, end = text.find(open_ch), text.rfind(close_ch)
    return text[start:end + 1] if 0 <= start < end else ""


if __name__ == "__main__":
    print(f"backend={BACKEND} model={MODEL}")
    print(ask_json('Return only this JSON array: [{"ok": true}]', []))
