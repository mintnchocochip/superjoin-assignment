#!/usr/bin/env python3
"""Render Claude Code session transcripts to Markdown for the AI transparency log.

Claude Code already records every session as JSONL under
~/.claude/projects/<encoded-project-path>/<session-id>.jsonl. This script only
renders them; nothing captures or logs anything new.

Run:  python tools/export_transcript.py            # write docs/ai-log/
      python tools/export_transcript.py --check    # verify no unredacted identifiers

Known limitation: the Stop hook can fire before the final assistant message is
flushed to the JSONL, so the newest turn may lag by one run. Output is rewritten
in full each time, so the next run catches up.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

# Applied to every string that reaches the output. Keep this the only exit path.
REDACTIONS = [
    (re.compile(r"shawfighter@gmail\.com", re.I), "[email redacted]"),
    (re.compile(r"[A-Za-z]:[\/]+Users[\/]+Shawf", re.I), "~"),
    (re.compile(r"/c/Users/Shawf", re.I), "~"),
    (re.compile(r"[A-Za-z]:[\/]+superjoin-proj", re.I), "<project>"),
    (re.compile(r"MANIKANDAN\s+S\b", re.I), "[redacted]"),
    (re.compile(r"\b23BCE\d{4}\b", re.I), "[redacted]"),
    (re.compile(r"K7\s+COMPUTING.*?600119", re.I | re.S), "[redacted]"),
    (re.compile(r"Internship_\d+\.pdf", re.I), "[redacted].pdf"),
]

CMD_MSG = re.compile(r"<command-message>(.*?)</command-message>", re.S)
CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)
STRIP_TAGS = re.compile(
    r"<(system-reminder|command-args|command-contents|local-command-stdout)>.*?"
    r"</\1>|<command-message>.*?</command-message>|<command-name>.*?</command-name>",
    re.S,
)


def redact(text):
    for pat, repl in REDACTIONS:
        text = pat.sub(repl, text)
    return text


def blocks(msg):
    """Yield (type, block) for a message, treating a bare string as one text block."""
    content = msg.get("content")
    if isinstance(content, str):
        yield "str", content
    else:
        for b in content or []:
            if isinstance(b, dict):
                yield b.get("type"), b


def clean_prompt(text):
    """Strip harness noise; surface a slash command as a readable line."""
    name = CMD_NAME.search(text)
    if name:
        cmd = name.group(1).strip()
        rest = STRIP_TAGS.sub("", text).strip()
        head = f"*(invoked `{cmd}`)*"
        return f"{head}\n\n{rest}" if rest else head
    return STRIP_TAGS.sub("", text).strip()


def parse_session(path):
    """Return (title, turns). A turn is one user prompt plus everything until the next."""
    title, turns, cur = None, [], None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind = d.get("type")
        if kind == "custom-title":
            title = d.get("customTitle") or title
            continue
        if d.get("isSidechain"):  # subagent traffic, not the user's conversation
            continue

        msg = d.get("message") or {}
        if kind == "user":
            for btype, b in blocks(msg):
                if btype == "str" and b.strip():
                    text = clean_prompt(b)
                    if text:
                        cur = {"ts": d.get("timestamp", ""), "prompt": text,
                               "replies": [], "tools": Counter()}
                        turns.append(cur)
        elif kind == "assistant" and cur is not None:
            for btype, b in blocks(msg):
                if btype == "text" and b.get("text", "").strip():
                    cur["replies"].append(b["text"].strip())
                elif btype == "tool_use":
                    cur["tools"][b.get("name", "?")] += 1
    return title, turns


def tool_line(tools):
    if not tools:
        return ""
    total = sum(tools.values())
    parts = ", ".join(f"{n} \u00d7{c}" for n, c in tools.most_common())
    return f"\n*[Claude ran {total} tool{'s' if total != 1 else ''}: {parts}]*\n"


def render(title, turns, session_id):
    out = [f"# {title or 'Session'}", "",
           f"Session `{session_id[:8]}` \u00b7 {len(turns)} turn"
           f"{'s' if len(turns) != 1 else ''}", ""]
    for i, t in enumerate(turns, 1):
        out.append(f"## Turn {i} \u2014 {t['ts'][:16].replace('T', ' ')}")
        out.append("")
        out.append("**Me:**")
        out.append("")
        out += [f"> {ln}" if ln.strip() else ">" for ln in t["prompt"].splitlines()]
        out.append("")
        line = tool_line(t["tools"])
        if line:
            out.append(line.strip())
            out.append("")
        if t["replies"]:
            out.append("**Claude:**")
            out.append("")
            out.append("\n\n".join(t["replies"]))
            out.append("")
        out.append("---")
        out.append("")
    return redact("\n".join(out))


def slugify(text):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (text or "session").lower())).strip("-")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="verify no unredacted identifier survives in the output")
    args = ap.parse_args()

    project = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()).resolve()
    outdir = project / "docs" / "ai-log"

    if args.check:
        bad = 0
        for md in sorted(outdir.glob("*.md")):
            text = md.read_text(encoding="utf-8")
            for pat, _ in REDACTIONS:
                for hit in pat.findall(text):
                    print(f"LEAK {md.name}: {pat.pattern} -> {str(hit)[:60]}")
                    bad += 1
        print("check: clean" if not bad else f"check: {bad} leak(s)")
        return 1 if bad else 0

    slug = re.sub(r"[^a-zA-Z0-9]", "-", str(project))
    src = Path.home() / ".claude" / "projects" / slug
    if not src.is_dir():
        print(f"no transcripts at {src}", file=sys.stderr)
        return 1

    outdir.mkdir(parents=True, exist_ok=True)
    index = []
    for jsonl in sorted(src.glob("*.jsonl")):
        title, turns = parse_session(jsonl)
        if not turns:
            continue
        date = turns[0]["ts"][:10] or "undated"
        name = f"{date}-{slugify(title)}.md"
        (outdir / name).write_text(render(title, turns, jsonl.stem), encoding="utf-8")
        index.append((date, name, title or "Session", len(turns)))
        print(f"wrote docs/ai-log/{name}  ({len(turns)} turns)")

    lines = ["# AI Transparency Log", "",
             "Every prompt sent to Claude Code while building this project, with its response.",
             "Generated from Claude Code's own session transcripts by",
             "[`tools/export_transcript.py`](../../tools/export_transcript.py) and refreshed",
             "automatically after each turn. Tool calls are collapsed to a one-line summary;",
             "personal identifiers and local paths are redacted.", "",
             "| Date | Session | Turns |", "|---|---|---|"]
    lines += [f"| {d} | [{t}]({n}) | {c} |" for d, n, t, c in sorted(index)]
    lines.append("")
    (outdir / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote docs/ai-log/README.md  ({len(index)} sessions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
