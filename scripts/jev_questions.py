"""Every Jev question and threshold used by the triage, in one place for review.

Each review is one request: state = {"app": ..., "review": {...}} and every
question below is asked about it in parallel (speculative fan-out). Code then
decides which answers apply, e.g. severity only matters when a problem exists.
"""

from __future__ import annotations

import re

from typesafe_sdk import Choice, Noul, NoulCriteria, Score

# ----------------------------------------------------------------- thresholds
# Noul values are probabilities of "yes". YES is the cut for counting a label;
# values between UNSURE_LOW and YES are reported as borderline for human review.
YES = 0.6
UNSURE_LOW = 0.4
# Off-topic reviews are dropped from every aggregate, so require more certainty.
OFF_TOPIC_YES = 0.75
# Below this Score confidence the sentiment label is shown as uncertain.
SENTIMENT_MIN_CONFIDENCE = 0.35
# Only ask for a key sentence when a review is at least this many sentences.
KEY_SENTENCE_MIN_SENTENCES = 3
KEY_SENTENCE_MAX_OPTIONS = 40

# Composite priority for a product area = sum over its issue reviews of
#   BASE + SEVERITY * severity(0..3) + CHURN * churn + REGRESSION * after_update
# Change these to re-rank areas; rerunning triage.py reuses cached answers, so no new Jev calls.
PRIORITY_WEIGHTS = {"base": 1.0, "severity": 1.0, "churn": 2.0, "regression": 1.0}

SENTIMENT_LABELS = ["very negative", "negative", "mixed / neutral", "positive", "very positive"]
SEVERITY_LABELS = ["no problem", "minor", "degraded", "blocking"]

# ----------------------------------------------------------------- review-level questions

SENTIMENT = Score(
    instructions={
        "question": "How does the reviewer feel about the app overall, based on `review`?",
        "focus": "Judge the feelings the reviewer expresses in their words, not how serious the problem is.",
    },
    criteria=[
        {"what": "Very negative", "signals": ["angry or hostile", "calls the app useless, terrible, or a scam"]},
        {"what": "Negative", "signals": ["mainly disappointed or frustrated", "few or no positives"]},
        {"what": "Mixed or neutral", "signals": ["praise and complaints roughly balance", "states facts or asks a question without much feeling"]},
        {"what": "Positive", "signals": ["mainly satisfied", "at most minor reservations"]},
        {"what": "Very positive", "signals": ["enthusiastic praise", "no complaints"]},
    ],
)

SEVERITY = Score(
    instructions={
        "question": "How badly does the problem described in `review` stop the reviewer from using the app?",
        "focus": "Judge the practical impact on the reviewer, not their tone or how strongly they word it.",
    },
    criteria=[
        {"what": "No problem is described", "examples": ["Love it!", "Works great"]},
        {
            "what": "Minor: a cosmetic issue, small inconvenience, or dislike of a design or business decision; the reviewer can still do what they want",
            "examples": ["the icons look dated", "too many upgrade prompts", "I don't want the AI features"],
        },
        {
            "what": "Degraded: something works poorly or unreliably, a feature is missing or was removed, or a change made a task harder",
            "examples": ["sync is slow", "search was replaced and is worse", "can't do on mobile what I can on desktop"],
        },
        {
            "what": "Blocking: the app or the reviewer's main task does not work at all, or they lost data or money",
            "examples": ["crashes on launch", "won't load anything", "locked out of my account", "my notes were deleted", "charged twice"],
        },
    ],
)

REVIEW_NOULS: dict[str, Noul] = {
    "reports_bug": Noul(
        instructions="Does `review` describe the app malfunctioning, such as crashing, freezing, showing errors, losing data, or a feature not working the way it is supposed to?",
        criteria=NoulCriteria(
            true="Something in the app is broken or behaves incorrectly",
            false="Only dislikes a design decision, price, policy, or content, or describes no problem",
        ),
    ),
    "requests_feature": Noul(
        instructions="Does `review` ask for a capability or option the app does not currently offer, or ask for a removed feature to be brought back?",
        criteria=NoulCriteria(
            true="Names something specific to add or restore, e.g. 'please add a sleep timer', 'bring back the old playlist view', 'let me queue a playlist after a song'",
            false="Only complains, or only asks to fix a problem or reduce something, e.g. 'fewer ads', 'stop crashing', 'fix login'; or makes no request",
        ),
    ),
    "churn_signal": Noul(
        instructions="Does the reviewer say they have stopped or will stop using or paying for the app, such as uninstalling, cancelling, or switching to another app?",
        criteria=NoulCriteria(
            true="States they uninstalled, cancelled, are leaving, switching, or will if nothing changes",
            false="No statement about leaving, uninstalling, cancelling, or switching",
        ),
    ),
    "after_update": Noul(
        instructions="Does the reviewer say a problem started or got worse after a recent app update or redesign?",
        criteria=NoulCriteria(
            true="Ties a problem to an update, new version, or redesign, e.g. 'since the last update'",
            false="Does not connect any problem to an update",
        ),
    ),
    "repro_detail": Noul(
        instructions="Does `review` give a specific detail an engineer could use to reproduce a problem, such as the device, operating system version, the steps taken, or exactly when it happens?",
        criteria=NoulCriteria(
            true="Names a device, OS version, screen, sequence of steps, or a precise trigger",
            false="Describes the problem only in general terms, or describes no problem",
        ),
    ),
    "off_topic": Noul(
        instructions="Is `review` unrelated to the experience of using this app, such as spam, gibberish, or a message about something else?",
        criteria=NoulCriteria(
            true="Spam, gibberish, a person's name, or about something other than this app, e.g. praising an artist or team",
            false="Any verdict on the app, even one word, slang, emoji, or another language, e.g. 'W app', 'boycott', 'mala', '👏👏', 'love it'",
        ),
    ),
    "is_english": Noul(instructions="Is `review` written mainly in English?"),
}

# ----------------------------------------------------------------- product-area questions

AREA_ISSUE_QUESTION = "Does the reviewer report a problem, complaint, or unmet need about `product_area` in `review`?"
AREA_PRAISE_QUESTION = "Does the reviewer praise or express satisfaction with `product_area` in `review`?"


def _area_spec(area: dict) -> dict:
    spec = {"name": area["name"], "covers": area["covers"]}
    if area.get("not_for"):
        spec["not_for"] = area["not_for"]
    return spec


def area_questions(areas: list[dict]) -> dict[str, Noul]:
    """Two Nouls per area: is there a problem with it, and is it praised. Several areas may apply."""
    questions: dict[str, Noul] = {}
    for area in areas:
        spec = _area_spec(area)
        questions[f"issue__{area['id']}"] = Noul(
            instructions={"product_area": spec, "question": AREA_ISSUE_QUESTION},
            criteria=NoulCriteria(
                true="Says something covered by `product_area` is broken, missing, confusing, annoying, too expensive, or worse than expected",
                false="Does not mention `product_area`, or mentions it only positively or neutrally",
            ),
        )
        questions[f"praise__{area['id']}"] = Noul(
            instructions={"product_area": spec, "question": AREA_PRAISE_QUESTION},
            criteria=NoulCriteria(
                true="Speaks well of something covered by `product_area`",
                false="Does not mention `product_area`, or only complains about it",
            ),
        )
    return questions


# ----------------------------------------------------------------- key sentence (select, don't generate)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\s*\n+\s*")


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text or "") if p and p.strip()]
    return [p for p in parts if len(p) > 1]


def key_sentence_question(sentences: list[str]) -> Choice:
    """Choice over the review's own sentences, so the report can quote the core complaint verbatim."""
    criteria: dict[str, str] = {"none": "No sentence states a problem or a request"}
    for i, sentence in enumerate(sentences[:KEY_SENTENCE_MAX_OPTIONS]):
        criteria[f"s{i}"] = sentence
    return Choice(
        instructions={
            "question": "Which sentence from `review` most directly states the reviewer's main problem or request?",
            "focus": "Prefer the sentence that names what is wrong or what they want, over sentences that only express feelings.",
        },
        criteria=criteria,
    )


def area_quote_question(area: dict, sentences: list[str]) -> Choice:
    """Second pass: which of the review's own sentences describes its problem with one product area."""
    criteria: dict[str, str] = {"none": "No sentence describes a problem with `product_area`"}
    for i, sentence in enumerate(sentences[:KEY_SENTENCE_MAX_OPTIONS]):
        criteria[f"s{i}"] = sentence
    return Choice(
        instructions={
            "product_area": _area_spec(area),
            "question": "Which sentence from `review` most directly describes the reviewer's problem with `product_area`?",
        },
        criteria=criteria,
    )


def area_praise_quote_question(area: dict, sentences: list[str]) -> Choice:
    """Second pass: which of the review's own sentences praises one product area."""
    criteria: dict[str, str] = {"none": "No sentence praises `product_area`"}
    for i, sentence in enumerate(sentences[:KEY_SENTENCE_MAX_OPTIONS]):
        criteria[f"s{i}"] = sentence
    return Choice(
        instructions={
            "product_area": _area_spec(area),
            "question": "Which sentence from `review` most directly praises `product_area`?",
        },
        criteria=criteria,
    )


def build_questions(areas: list[dict], sentences: list[str]) -> dict:
    questions: dict = {"sentiment": SENTIMENT, "severity": SEVERITY, **REVIEW_NOULS, **area_questions(areas)}
    if len(sentences) >= KEY_SENTENCE_MIN_SENTENCES:
        questions["key_sentence"] = key_sentence_question(sentences)
    return questions


def build_state(app_name: str, review: dict) -> dict:
    """Only the reviewer's own words go to Jev. Star rating, date, and version stay in code."""
    body = {"text": review.get("text") or ""}
    if review.get("title"):
        body = {"title": review["title"], **body}
    return {"app": app_name, "review": body}
