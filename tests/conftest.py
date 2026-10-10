"""Shared helpers: a deterministic fake Jev client, synthetic review fixtures, and a way to run triage.py in-process.

Every review here is made up. The fixtures are generated when the tests start (nothing is fetched and no
real review text is stored), with the shapes the tests need: a 4-month Google Play window without titles,
a 3-day App Store window with titles and a few older versions, Steam thumbs up/down, Japanese text without
". " sentence breaks, a hand-written adversarial set, and both CSV export layouts.
"""

from __future__ import annotations

import atexit
import contextlib
import csv
import hashlib
import io
import json
import random
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
DATA = Path(__file__).resolve().parent / "data"
sys.path.insert(0, str(SKILL_DIR / "scripts"))


def _unit(*parts: str) -> float:
    # A deterministic stand-in for model probabilities, not a security use.
    return int(hashlib.sha1("|".join(parts).encode(), usedforsecurity=False).hexdigest()[:8], 16) / 0xFFFFFFFF


class FakeJev:
    """Stands in for AsyncTypeSafeClient. Answers are a pure function of (model, review text, question id).

    Class attributes are the knobs: `latest` is what "jev-latest" resolves to, `fail_texts` makes
    reviews containing any of those strings fail with an HTTP error (`fail_status`; "connection" or
    "timeout" raise the SDK's errors for a request that got no response), `stop_after` sends this
    process SIGTERM after that many calls (a command timeout), and `log` records every call.
    """

    latest = "jev-1.13.0"
    fail_texts: tuple[str, ...] = ()
    fail_status: int | str = 500
    stop_after: int | None = None
    log: list[dict] = []

    def __init__(self, model: str | None = None, **_):
        self.model = FakeJev.latest if model in (None, "jev-latest") else model

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def system_one(self, state, questions):
        import asyncio
        import os
        import signal

        import httpx2
        from typesafe_sdk import TypeSafeAPIConnectionError, TypeSafeAPIError, TypeSafeAPITimeoutError

        text = state["review"].get("text", "") + state["review"].get("title", "")
        FakeJev.log.append({"text": text, "model": self.model, "questions": {qid: type(q).__name__ for qid, q in questions.items()}})
        if FakeJev.stop_after is not None:
            if len(FakeJev.log) == FakeJev.stop_after:
                os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(0.002)  # a real request waits on the network, which lets the loop see the signal
        if any(f in text for f in FakeJev.fail_texts):
            if FakeJev.fail_status == "connection":
                raise TypeSafeAPIConnectionError("Connection reset by peer")
            if FakeJev.fail_status == "timeout":
                raise TypeSafeAPITimeoutError(10.0)
            raise TypeSafeAPIError(FakeJev.fail_status, None, httpx2.Headers())
        answers = {}
        for qid, q in questions.items():
            h = _unit(self.model, text, qid)
            kind = type(q).__name__
            if kind == "Noul":
                answers[qid] = SimpleNamespace(type="noul", noul=h)
            elif kind == "Score":
                n = len(q.criteria)
                answers[qid] = SimpleNamespace(type="score", score=h * (n - 1), confidence=0.8, probabilities={i: 1 / n for i in range(n)})
            else:
                keys = list(q.criteria)
                answers[qid] = SimpleNamespace(type="choice", choice=keys[min(len(keys) - 1, int(h * len(keys)))], confidence=0.7, probabilities={k: 1 / len(keys) for k in keys})
        return SimpleNamespace(answers=answers, model=self.model, usage=SimpleNamespace(input_tokens=1000))


@pytest.fixture
def fake_jev(monkeypatch):
    import typesafe_sdk

    FakeJev.latest, FakeJev.fail_texts, FakeJev.fail_status, FakeJev.stop_after, FakeJev.log = "jev-1.13.0", (), 500, None, []
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", FakeJev)
    return FakeJev


def run_triage(monkeypatch, reviews: Path, areas: Path, out: Path, *extra: str) -> dict:
    import triage

    monkeypatch.setattr(sys, "argv", ["triage.py", str(reviews), "--areas", str(areas), "--out-dir", str(out), *extra])
    assert triage.main() == 0
    return json.loads((out / "summary.json").read_text(encoding="utf-8"))


def default_areas() -> Path:
    return SKILL_DIR / "references/default_areas.json"


def write_reviews(path: Path, app_name: str, reviews: list[dict]) -> Path:
    path.write_text(json.dumps({"app": {"name": app_name, "store": "test"}, "reviews": reviews}, ensure_ascii=False), encoding="utf-8")
    return path


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name / "reviews.json").read_text(encoding="utf-8"))


def all_quotes(summary: dict) -> list[dict]:
    quotes = []
    for area in summary["areas"]:
        quotes += area["top_quotes"] + area["praise_quotes"]
    for key in ("bugs", "feature_requests", "churn", "after_update", "needs_review"):
        quotes += summary[key]
    return quotes + summary["unassigned_problems"]["examples"]


# ----------------------------------------------------------------- synthetic fixtures

SENTENCES = {
    "crash": ["The app crashes every time I open the calendar.", "It freezes on the loading screen and never recovers.",
              "Since the latest version it force closes when I add a photo.", "It won't open at all on my tablet anymore."],
    "login": ["I keep getting logged out every morning.", "Sign in with my email fails with an unknown error.",
              "The password reset link never arrives.", "Two-step login loops back to the start screen."],
    "pricing": ["The yearly plan doubled in price without warning.", "I was charged after I cancelled the trial.",
                "Too many features are locked behind the premium plan.", "Please bring back the cheaper monthly option."],
    "ads": ["There is an ad after every single task.", "The video ads are louder than anything else in the app.",
            "Ads keep showing even though I pay for the subscription."],
    "sync": ["My notes don't sync between my phone and laptop.", "Half of my lists vanished after syncing.",
             "Offline changes are lost when I reconnect."],
    "notifications": ["Reminders arrive hours late or not at all.", "I get five notifications a day that I never asked for.",
                      "Turning off marketing notifications doesn't stick."],
    "design": ["The new layout hides the search button.", "Buttons moved around again and I can't find settings.",
               "The text is tiny and there's no way to make it bigger."],
    "support": ["Support never answered my emails.", "The help center has no way to contact a person."],
    "praise": ["I love how simple it is to add things.", "Best app I have tried for this, hands down.",
               "The widgets are beautiful and fast.", "It has kept me organized for years.", "Works perfectly on every device I own."],
    "request": ["Please add a dark mode for the widget.", "I wish I could share lists with my family.",
                "It would be great to have a calendar view.", "Could you add an export to spreadsheet?"],
    "churn": ["I'm switching to another app.", "Cancelling my subscription today.", "Uninstalled until this is fixed."],
    "update": ["Everything was fine before the last update.", "The update broke what used to work."],
}
TITLES = ["Frustrating lately", "Great app", "Not worth the price", "Almost perfect", "Please fix", "Changed my routine",
          "Used to love it", "Solid", "Disappointed", "Five stars", "Needs work", "Helpful every day"]
JA_SENTENCES = ["アプリがすぐに落ちます。", "シャッフルが正しく動きません。", "広告が多すぎます。", "音質はとても良いです。",
                "プレイリストが消えました。", "毎日使っています！", "ログインできないことがあります。", "オフライン再生が便利です。"]
PLAY_CONSOLE_COLUMNS = [
    "Package Name", "App Version Code", "App Version Name", "Reviewer Language", "Device",
    "Review Submit Date and Time", "Review Submit Millis Since Epoch", "Review Last Update Date and Time",
    "Review Last Update Millis Since Epoch", "Star Rating", "Review Title", "Review Text",
    "Developer Reply Date and Time", "Developer Reply Millis Since Epoch", "Developer Reply Text", "Review Link",
]


def _text(rng: random.Random, i: int) -> tuple[str, int]:
    """A review of 1–6 made-up sentences, made unique by a usage detail; returns (text, rating)."""
    happy = rng.random() < 0.45
    topics = ["praise", "praise", "request"] if happy else [k for k in SENTENCES if k != "praise"]
    parts = [rng.choice(SENTENCES[rng.choice(topics)]) for _ in range(rng.randint(1, 4 if rng.random() < 0.9 else 6))]
    parts.insert(rng.randint(0, len(parts)), f"I have used it for {i + 2} days.")
    rating = rng.choice([4, 5, 5]) if happy else rng.choice([1, 1, 2, 2, 3])
    return " ".join(dict.fromkeys(parts)), rating


def _dates(n: int, newest: datetime, span_days: int) -> list[str]:
    return [(newest - timedelta(minutes=i * span_days * 1440 // n)).isoformat() for i in range(n)]


def _app(name: str, store: str, **extra) -> dict:
    return {"name": name, "category": "Productivity", "description": f"{name} helps you plan your day.",
            "average_rating": 4.5, "rating_count": 12345, "store": store, "sort": "newest first", "country": "us",
            "url": None, **extra}


def _play_app() -> dict:
    rng = random.Random(1)
    dates = _dates(300, datetime(2026, 10, 4, 23, 0, tzinfo=timezone.utc), 124)
    reviews = []
    for i in range(300):
        text, rating = _text(rng, i)
        version = None if rng.random() < 0.15 else f"v12{rng.choice([180, 210, 240, 282, 296, 298])}"
        reviews.append({"id": f"play-{i:04d}", "rating": rating, "title": "", "text": text, "date": dates[i],
                        "version": version, "helpful_count": rng.randint(0, 9)})
    app = _app("Tasker Pro", "Google Play", current_version="Varies with device",
               url="https://play.google.com/store/apps/details?id=com.example.taskerpro&hl=en&gl=US")
    return {"app": app, "reviews": reviews, "fetched_at": "2026-10-05T23:47:39+00:00"}


def _ios_app() -> dict:
    rng = random.Random(2)
    dates = _dates(300, datetime(2026, 10, 4, 22, 0, tzinfo=timezone.utc), 2)
    versions = ["7.142.0"] * 297 + ["7.101.1", "7.101.1", "7.58.1"]
    reviews = []
    for i in range(300):
        text, rating = _text(rng, i)
        title = rng.choice(TITLES)
        if i % 60 == 7:  # the problem is only in the title; the body says almost nothing
            title, text, rating = f"Lost my {i + 2}-day streak to the energy change", "Not recommended", 1
        reviews.append({"id": f"apple-{1000000 + i}", "rating": rating, "title": title, "text": text, "date": dates[i],
                        "version": versions[i], "helpful_count": rng.randint(0, 5)})
    app = _app("LinguaLeap", "Apple App Store", current_version="7.142.0", sort="newest first (public feed, max 500)",
               url="https://apps.apple.com/us/app/lingualeap/id1234567890")
    return {"app": app, "reviews": reviews, "fetched_at": "2026-10-05T23:48:10+00:00"}


def _steam_game() -> dict:
    rng = random.Random(3)
    dates = _dates(299, datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc), 13)
    reviews = []
    for i in range(299):
        text, rating = _text(rng, i)
        text = " ".join([text] * rng.randint(1, 3))  # Steam reviews run long
        reviews.append({"id": f"steam-{i}", "rating": 5 if rating >= 4 else 1, "title": "", "text": text, "date": dates[i],
                        "version": None, "helpful_count": rng.randint(0, 50)})
    app = _app("City Builder X", "Steam", current_version=None, average_rating=None, rating_count=None, country=None,
               sort="most recent first", url="https://store.steampowered.com/app/123456/City_Builder_X/")
    return {"app": app, "reviews": reviews, "fetched_at": "2026-10-05T23:47:45+00:00"}


def _play_ja() -> dict:
    rng = random.Random(4)
    dates = _dates(150, datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc), 9)
    reviews = []
    for i in range(150):
        text = "".join(rng.sample(JA_SENTENCES, rng.randint(1, 4))) + f"{i + 2}日使いました。"
        reviews.append({"id": f"play-ja-{i}", "rating": rng.randint(1, 5), "title": "", "text": text, "date": dates[i],
                        "version": f"9.1.{rng.choice([84, 86, 88])}.{rng.randint(2000, 2500)}", "helpful_count": 0})
    app = _app("MusicBox", "Google Play", current_version="Varies with device", country="jp",
               url="https://play.google.com/store/apps/details?id=com.example.musicbox&hl=ja&gl=JP")
    return {"app": app, "reviews": reviews, "fetched_at": "2026-10-05T23:47:46+00:00"}


def _fetch_from_file(source: Path, app_name: str, out: Path) -> None:
    """Normalize an export the way users do: fetch_reviews.py --from-file."""
    import fetch_reviews

    argv = sys.argv
    sys.argv = ["fetch_reviews.py", "--from-file", str(source), "--app-name", app_name, "--out", str(out)]
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            assert fetch_reviews.main() == 0
    finally:
        sys.argv = argv


def _build_fixtures(root: Path) -> None:
    def save(name: str, data: dict) -> None:
        (root / name).mkdir(parents=True, exist_ok=True)
        (root / name / "reviews.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    play, ios = _play_app(), _ios_app()
    for name, data in (("play_app", play), ("ios_app", ios), ("steam_game", _steam_game()), ("play_ja", _play_ja())):
        save(name, data)
    save("tiny", {"app": play["app"], "reviews": play["reviews"][:8], "fetched_at": play["fetched_at"]})

    (root / "adversarial").mkdir()
    _fetch_from_file(DATA / "adversarial_source.json", "Tasker Pro", root / "adversarial/reviews.json")

    (root / "csv_appstore").mkdir()
    with (root / "csv_appstore/source.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Review ID", "Title", "Review Body", "Star Rating", "Last Updated", "App Version", "Territory"])
        for r in ios["reviews"][:60]:
            writer.writerow([r["id"], r["title"], r["text"], r["rating"], r["date"], r["version"], "USA"])
    _fetch_from_file(root / "csv_appstore/source.csv", "LinguaLeap", root / "csv_appstore/reviews.json")

    (root / "csv_play_console").mkdir()
    with (root / "csv_play_console/source.csv").open("w", newline="", encoding="utf-16") as handle:
        writer = csv.writer(handle)
        writer.writerow(PLAY_CONSOLE_COLUMNS)
        for i, r in enumerate(play["reviews"][:60]):
            row = dict.fromkeys(PLAY_CONSOLE_COLUMNS, "")
            row.update({"Package Name": "com.example.taskerpro", "App Version Name": r["version"] or "", "Reviewer Language": "en",
                        "Review Submit Date and Time": r["date"], "Review Last Update Date and Time": r["date"],
                        "Star Rating": r["rating"], "Review Text": r["text"], "Review Link": f"https://play.google.com/console/reviews/{i}"})
            writer.writerow([row[c] for c in PLAY_CONSOLE_COLUMNS])


FIXTURES = Path(tempfile.mkdtemp(prefix="review-triage-fixtures-"))
atexit.register(shutil.rmtree, FIXTURES, True)
_build_fixtures(FIXTURES)
