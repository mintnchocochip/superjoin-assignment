# superjoin-assignment

Fact knowledge layer for the Superjoin VIT 2026 engineering intern assignment.

## Setup and Run Instructions

_TODO_

## Video Demo

_TODO_

## Approach

_TODO_

### AI Transparency Log

Every prompt sent to Claude Code while building this project, together with its
response, is recorded in [`docs/ai-log/`](docs/ai-log/).

The log is not written by hand. Claude Code already stores each session as JSONL
under `~/.claude/projects/`, so [`tools/export_transcript.py`](tools/export_transcript.py)
just renders those existing transcripts to Markdown. A `Stop` hook in
[`.claude/settings.json`](.claude/settings.json) reruns it after every turn, so the
log cannot drift out of date.

- Tool calls are collapsed to a one-line summary per turn; the prompts and replies are verbatim.
- Personal identifiers and local filesystem paths are redacted by the exporter itself,
  so redaction cannot be forgotten. `python tools/export_transcript.py --check` re-scans
  the output and exits non-zero if anything slips through.
- Full fidelity (reasoning traces, tool inputs and outputs) remains in the raw JSONL,
  which is deliberately not committed.

## Limitations and Next Steps

_TODO_

## Additional Notes

_TODO_
