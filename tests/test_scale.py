"""Scale: runs larger than the default 300 reviews. No real Jev calls; runs in seconds for $0.

Covers what broke or could break when a run is big enough to outlast a command timeout:
answers saved as the run goes, an interrupted run resuming without paying twice, a cache write
that can't be left half-written, the review-count guard, and the fetcher's limits.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from conftest import FIXTURES, FakeJev, default_areas, load_fixture, run_triage, write_reviews

import triage  # noqa: E402


def _stable(summary: dict) -> dict:
    return {k: v for k, v in summary.items() if k not in ("generated_at", "jev")}


def _pass1_texts(log: list[dict]) -> list[str]:
    """Review texts sent in pass 1 (the pass that asks about sentiment), in call order."""
    return [call["text"] for call in log if "sentiment" in call["questions"]]


def _many_reviews(n: int) -> list[dict]:
    """n distinct reviews built from the synthetic Google Play fixture, spread over ~5 months."""
    base = load_fixture("play_app")["reviews"]
    out = []
    for i in range(n):
        r = dict(base[i % len(base)])
        r["id"] = f"r{i:05d}"
        r["text"] = f"{r['text']} (#{i})"
        r["date"] = (date(2026, 10, 1) - timedelta(days=i * 150 // n)).isoformat()
        out.append(r)
    return out


# ----------------------------------------------------------------- checkpoints and resume


@pytest.mark.parametrize("stop", ["credits_run_out", "process_killed"])
def test_interrupted_run_resumes_without_reasking(fake_jev, monkeypatch, tmp_path, stop):
    """A run stops partway (402, or the process dies between checkpoints); the same command finishes it.

    Reviews answered before the stop must not be asked again, and the finished report must match
    a run that was never interrupted.
    """
    monkeypatch.setattr(triage, "CHECKPOINT_EVERY", 3)
    reviews = FIXTURES / "tiny/reviews.json"
    texts = [r["text"] + r["title"] for r in json.loads(reviews.read_text())["reviews"]]
    poison = texts[4]  # in the second batch of 3

    if stop == "credits_run_out":
        fake_jev.fail_texts, fake_jev.fail_status = (poison,), 402
        expected = SystemExit
    else:
        original = FakeJev.system_one

        async def dies_on_poison(self, state, questions):
            if state["review"].get("text", "") + state["review"].get("title", "") == poison:
                raise RuntimeError("process killed")
            return await original(self, state, questions)

        monkeypatch.setattr(FakeJev, "system_one", dies_on_poison)
        expected = RuntimeError

    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["triage.py", str(reviews), "--areas", str(default_areas()), "--out-dir", str(out)])
    with pytest.raises(expected):
        triage.main()
    assert not (out / "report.html").exists(), "an interrupted run writes no report"
    cached = triage.load_cache(out / "jev_raw.jsonl")
    assert len(cached) >= 3, "the first batch's answers were saved before the stop"

    # Resume: same command, nothing failing now.
    fake_jev.fail_texts = ()
    monkeypatch.setattr(FakeJev, "system_one", FakeJev.system_one if stop == "credits_run_out" else original)
    fake_jev.log.clear()
    resumed = run_triage(monkeypatch, reviews, default_areas(), out)
    reasked = _pass1_texts(fake_jev.log)
    assert not set(reasked) & set(texts[:3]), "reviews answered before the stop were asked again"
    assert len(reasked) == len(texts) - len(cached)

    uninterrupted = run_triage(monkeypatch, reviews, default_areas(), tmp_path / "clean")
    assert _stable(resumed) == _stable(uninterrupted)


def test_checkpoints_do_not_change_results(fake_jev, monkeypatch, tmp_path):
    """Saving every 7 reviews and saving once at the end give the same report, with the same Jev calls."""
    reviews = write_reviews(tmp_path / "r.json", "Tasker Pro", _many_reviews(60))
    monkeypatch.setattr(triage, "CHECKPOINT_EVERY", 10_000)
    once = run_triage(monkeypatch, reviews, default_areas(), tmp_path / "once")
    calls_once = len(fake_jev.log)
    monkeypatch.setattr(triage, "CHECKPOINT_EVERY", 7)
    often = run_triage(monkeypatch, reviews, default_areas(), tmp_path / "often")
    assert len(fake_jev.log) == 2 * calls_once
    assert _stable(once) == _stable(often)
    assert (tmp_path / "once/jev_raw.jsonl").read_text().count("\n") == 60


def test_cache_write_is_all_or_nothing(tmp_path, monkeypatch):
    """A crash while the cache is being written leaves the previous cache intact, not a truncated file."""
    path = tmp_path / "jev_raw.jsonl"
    previous = {f"r{i}": {"id": f"r{i}", "answers": {}} for i in range(5)}
    triage.write_cache(path, {}, previous)
    before = path.read_bytes()

    calls = {"n": 0}

    def dumps(value, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise KeyboardInterrupt  # e.g. the command timeout lands mid-write
        return json.dumps(value, **kwargs)

    monkeypatch.setattr(triage, "json", SimpleNamespace(dumps=dumps, loads=json.loads))
    with pytest.raises(KeyboardInterrupt):
        triage.write_cache(path, {"r9": {"id": "r9", "answers": {}}}, previous)
    assert path.read_bytes() == before
    monkeypatch.setattr(triage, "json", json)
    assert len(triage.load_cache(path)) == 5


# ----------------------------------------------------------------- review-count guard


def test_runs_above_the_tested_size_need_allow_large(fake_jev, monkeypatch, tmp_path):
    monkeypatch.setattr(triage, "MAX_REVIEWS", 5)
    reviews = FIXTURES / "tiny/reviews.json"  # 8 reviews
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["triage.py", str(reviews), "--areas", str(default_areas()), "--out-dir", str(out)])
    with pytest.raises(SystemExit) as stopped:
        triage.main()
    message = str(stopped.value)
    assert "--allow-large" in message and "--since" in message and "$" in message
    assert not fake_jev.log and not out.exists(), "nothing is asked or written before the user decides"

    summary = run_triage(monkeypatch, reviews, default_areas(), out, "--allow-large")
    assert summary["overview"]["reviews_total"] == 8
    # --limit brings a large file under the guard without --allow-large.
    run_triage(monkeypatch, reviews, default_areas(), tmp_path / "limited", "--limit", "5")


def test_two_thousand_reviews_end_to_end(fake_jev, monkeypatch, tmp_path):
    """A 2,000-review run completes, counts every review, and its report lists every review."""
    reviews = write_reviews(tmp_path / "r.json", "Tasker Pro", _many_reviews(2000))
    summary = run_triage(monkeypatch, reviews, default_areas(), tmp_path / "out")
    o = summary["overview"]
    assert o["reviews_total"] == 2000 and o["failed"] == 0
    assert o["reviews_analyzed"] + o["off_topic"] == 2000
    assert len(summary["all_reviews"]) == o["reviews_analyzed"]
    assert (tmp_path / "out/review_labels.csv").read_text(encoding="utf-8").count("\n") >= 2001
    assert summary["time"]["grain"] == "month"


# ----------------------------------------------------------------- fetcher limits


def _apple_feed(pages_available: int, start_day: date):
    def entry(n: int) -> dict:
        day = (start_day - timedelta(days=n // 50)).isoformat()
        return {"id": {"label": f"https://x/{n}"}, "im:rating": {"label": "3"}, "title": {"label": "t"},
                "content": {"label": f"review {n}"}, "updated": {"label": day}, "im:version": {"label": "1.0"}}

    def get_json(url: str) -> dict:
        if "lookup" in url:
            return {"results": [{"trackName": "Fake"}]}
        page = int(url.split("page=")[1].split("/")[0])
        if page > pages_available:
            return {"feed": {}}
        return {"feed": {"entry": [entry((page - 1) * 50 + i) for i in range(50)]}}

    return get_json


def test_apple_over_500_says_the_feed_stops_there(monkeypatch, tmp_path, capsys):
    import fetch_reviews

    monkeypatch.setattr(fetch_reviews, "get_json", _apple_feed(10, date(2026, 10, 5)))
    out = tmp_path / "reviews.json"
    monkeypatch.setattr(sys, "argv", ["fetch_reviews.py", "https://apps.apple.com/us/app/x/id1", "--max", "2000", "--out", str(out)])
    assert fetch_reviews.main() == 0
    printed = capsys.readouterr().out
    assert len(json.loads(out.read_text())["reviews"]) == 500
    assert "stops at the newest 500" in printed and "App Store Connect export" in printed
    assert "the store had no more" not in printed


def test_apple_since_beyond_the_feed_says_the_window_is_not_covered(monkeypatch, tmp_path, capsys):
    import fetch_reviews

    monkeypatch.setattr(fetch_reviews, "get_json", _apple_feed(10, date(2026, 10, 5)))  # 50 a day: back to Sep 26
    out = tmp_path / "reviews.json"
    monkeypatch.setattr(sys, "argv", ["fetch_reviews.py", "https://apps.apple.com/us/app/x/id1", "--since", "2026-08-01", "--out", str(out)])
    assert fetch_reviews.main() == 0
    printed = capsys.readouterr().out
    assert "not 2026-08-01" in printed and "not fully covered" in printed


def _steam(n: int, newest: date, per_day: int):
    days = [(newest - timedelta(days=i // per_day)).isoformat() for i in range(n)]

    def get_json(url: str) -> dict:
        if "appdetails" in url:
            return {"1": {"data": {"name": "Fake"}}}
        start = int(url.split("cursor=")[1].split("&")[0].replace("%2A", "0").replace("*", "0"))
        batch = [{"recommendationid": str(i), "voted_up": True, "review": f"review {i}",
                  "timestamp_created": int(datetime.fromisoformat(days[i]).timestamp()) + 43200} for i in range(start, min(start + 100, n))]
        return {"reviews": batch, "cursor": str(start + 100) if batch else None}

    return get_json


def test_since_with_max_above_fetch_limit_still_fills_max(monkeypatch, tmp_path):
    """--since with --max 1000 used to stop at --fetch-limit, silently returning fewer than asked."""
    import fetch_reviews

    monkeypatch.setattr(fetch_reviews, "get_json", _steam(3000, date(2026, 10, 5), per_day=20))
    out = tmp_path / "reviews.json"
    monkeypatch.setattr(sys, "argv", ["fetch_reviews.py", "https://store.steampowered.com/app/1/x", "--since", "2026-08-01",
                                      "--max", "1000", "--fetch-limit", "500", "--out", str(out)])
    assert fetch_reviews.main() == 0
    assert len(json.loads(out.read_text())["reviews"]) == 1000


def test_large_max_without_since_returns_max(monkeypatch, tmp_path):
    import fetch_reviews

    monkeypatch.setattr(fetch_reviews, "get_json", _steam(6000, date(2026, 10, 5), per_day=40))
    out = tmp_path / "reviews.json"
    monkeypatch.setattr(sys, "argv", ["fetch_reviews.py", "https://store.steampowered.com/app/1/x", "--max", "5000", "--out", str(out)])
    assert fetch_reviews.main() == 0
    assert len(json.loads(out.read_text())["reviews"]) == 5000


# ----------------------------------------------------------------- narrative numbers vs. the current brief


def test_stale_narrative_numbers_are_flagged():
    """Round 3: a narrative kept 'borderline 60' from the brief before an area rerun; the new brief said 42."""
    brief = ("Reviews: 300 fetched, 300 analyzed; dates 2026-06-08 to 2026-10-07\n"
             "Sentiment (from text): {'very negative': 16, 'negative': 62}\n"
             "reviews with at least one borderline label 42\n| 1 | Login | 1406 |\n")
    narrative = ("Headline. 300 reviews from Jun 8 to Oct 7, 2026, on v11.2.3. 60 reviews have a borderline label, "
                 "and 1,406 report a problem; see [Login](#area-login_2fa).\n"
                 '> "I paid 99 dollars and it broke"\n'
                 'One said "it lost 77 tasks".\n')
    assert triage.numbers_not_in_brief(narrative, brief) == ["60"]


def test_render_with_narrative_prints_the_number_check(fake_jev, monkeypatch, tmp_path, capsys):
    reviews = FIXTURES / "tiny/reviews.json"
    run_triage(monkeypatch, reviews, default_areas(), tmp_path)
    (tmp_path / "narrative.md").write_text("Headline. 8 reviews, and 4321 of them are about login.\n\n## Fix first\n")
    capsys.readouterr()
    run_triage(monkeypatch, reviews, default_areas(), tmp_path, "--narrative", str(tmp_path / "narrative.md"))
    out = capsys.readouterr().out
    assert "check narrative:" in out and "4321" in out
    (tmp_path / "narrative.md").write_text("Headline. 8 reviews.\n\n## Fix first\n")
    run_triage(monkeypatch, reviews, default_areas(), tmp_path, "--narrative", str(tmp_path / "narrative.md"))
    assert "check narrative:" not in capsys.readouterr().out
