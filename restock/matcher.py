"""Matching spoken shopping-list entries to products on the regulars list.

This is the one place where a language model is used, because the input is
voice transcription and the task is genuinely fuzzy: "a. a. batteries" is AA
batteries, "fish fingers" is a specific Birds Eye box, and "AEG clean and
care" is a dishwasher product with no grocery equivalent at all.

We shell out to the `claude` CLI rather than calling the API directly, so this
runs on an existing Claude Code login with no API key to manage.

**The model's output is never trusted.** It is asked to choose only from the
labels we send it, and every label it returns is checked against the regulars
file before anything reaches the basket. A model asked to map "canned tuna"
onto a list can produce a plausible-looking product that is not on it; that
must be caught here rather than discovered in the delivery. Anything that
fails the check is reported as unmatched, never guessed at.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from . import config
from .alexa import ListItem
from .regulars import Entry

MODEL = "claude-opus-5"

PROMPT = """\
You are matching a voice-dictated shopping list to a fixed list of products \
someone regularly buys from Tesco.

The dictated items come from an Alexa shopping list, so the text is a raw \
speech transcription and may be garbled, abbreviated or oddly punctuated. \
For example "a. a. batteries" means AA batteries.

Here is the repertoire you may choose from. Each line is one option, given as \
its exact LABEL followed by the specific products it covers:

{repertoire}

Here are the dictated items to match:

{items}

For each dictated item, choose the LABEL that is clearly the same thing, or \
null if none of them is.

Rules:
- Use only labels from the repertoire above, copied exactly. Never invent one.
- Return null unless the match is obvious. A wrong match means the wrong food \
gets bought and eaten, so an honest null is much better than a near-miss.
- Being the same category is not enough. "fish fingers" matching a fish-finger \
product is right; "honey" matching a jam or conserve is wrong.
- Some items will have no match at all. That is expected and fine.

Reply with JSON only, no prose and no code fences: a list of objects each with \
"item" (the dictated text, copied exactly), "match" (a label, or null) and \
"reason" (at most 12 words).
"""


@dataclass(frozen=True)
class Match:
    """One dictated item, and the entry it was matched to (or not)."""

    item: ListItem
    entry: Entry | None
    reason: str

    @property
    def matched(self) -> bool:
        return self.entry is not None


class MatcherError(RuntimeError):
    """Raised when the model could not be consulted or understood."""


def describe_repertoire(entries: list[Entry]) -> str:
    """Render the regulars list as labelled options for the prompt."""
    lines = []
    for entry in entries:
        covers = "; ".join(o.name for o in entry.options)
        lines.append(f'- LABEL: "{entry.label}" — covers: {covers}')
    return "\n".join(lines)


def build_prompt(items: list[ListItem], entries: list[Entry]) -> str:
    return PROMPT.format(
        repertoire=describe_repertoire(entries),
        items="\n".join(f"- {item.text}" for item in items),
    )


def match_items(
    items: list[ListItem], entries: list[Entry], *, timeout_s: int = 240
) -> list[Match]:
    """Match dictated items to regulars entries, validating every answer."""
    if not items:
        return []
    raw = _ask_claude(build_prompt(items, entries), timeout_s=timeout_s)
    return _validate(raw, items, entries)


def _ask_claude(prompt: str, *, timeout_s: int) -> str:
    """Run the prompt through the `claude` CLI and return the model's text."""
    command = [
        "claude",
        "-p",
        prompt,
        "--model",
        MODEL,
        "--output-format",
        "json",
    ]
    try:
        finished = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            # Without this the CLI waits on stdin before starting.
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise MatcherError(
            "The `claude` CLI is not on PATH, so the shopping list cannot be "
            "matched. Install Claude Code, or run `fill` without a list."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MatcherError(f"`claude` did not answer within {timeout_s}s.") from exc

    if finished.returncode != 0:
        raise MatcherError(
            f"`claude` exited {finished.returncode}: {finished.stderr.strip()[:400]}"
        )
    try:
        envelope = json.loads(finished.stdout)
    except json.JSONDecodeError as exc:
        raise MatcherError("Could not parse the `claude` CLI's own output.") from exc
    if envelope.get("is_error"):
        raise MatcherError(f"`claude` reported an error: {envelope.get('result')}")
    return str(envelope.get("result", ""))


def _extract_json(text: str) -> list:
    """Pull the JSON list out of the model's reply, fences and all."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        raise MatcherError(f"No JSON list in the model's reply: {text[:200]!r}")
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise MatcherError("The model's reply was not valid JSON.") from exc
    if not isinstance(parsed, list):
        raise MatcherError("The model's reply was not a JSON list.")
    return parsed


def _validate(reply: str, items: list[ListItem], entries: list[Entry]) -> list[Match]:
    """Turn the model's reply into Matches, rejecting anything unrecognised.

    Every item we asked about gets exactly one Match, whatever the model said.
    A label we don't recognise, or an item we never sent, is discarded and the
    item reported as unmatched — the model does not get to widen the list.
    """
    by_label = {entry.label.strip().lower(): entry for entry in entries}
    answers: dict[str, tuple[Entry | None, str]] = {}

    for row in _extract_json(reply):
        if not isinstance(row, dict):
            continue
        text = str(row.get("item", "")).strip().lower()
        reason = str(row.get("reason", "")).strip()
        label = row.get("match")
        if label is None:
            answers[text] = (None, reason or "no match offered")
            continue
        entry = by_label.get(str(label).strip().lower())
        if entry is None:
            config.vprint(f"rejected unknown label {label!r} for {row.get('item')!r}")
            answers[text] = (None, f"model returned unknown label {label!r}")
            continue
        answers[text] = (entry, reason)

    matches: list[Match] = []
    for item in items:
        entry, reason = answers.get(
            item.text.strip().lower(), (None, "model gave no answer")
        )
        matches.append(Match(item, entry, reason))
    return matches
