# /// script
# requires-python = ">=3.10"
# dependencies = ["google-play-scraper>=1.2.7,<2"]
# ///
"""Fetch app reviews from a review-page URL into a normalized reviews.json.

Supported sources:
  * Apple App Store   https://apps.apple.com/<cc>/app/<slug>/id<digits>
  * Google Play       https://play.google.com/store/apps/details?id=<package>
  * Steam             https://store.steampowered.com/app/<appid>/...
  * Any page that embeds schema.org Review objects as JSON-LD (best effort)
  * A local CSV/JSON export (--from-file), e.g. from App Store Connect or a CRM

Exit codes: 2 means the URL is not supported; the caller should extract reviews
another way and write them with --from-file. 3 means the source gave no reviews
(nothing is written): the store returned none, the app wasn't found, the store
couldn't be reached, or the export couldn't be read. The message says why and
what to try instead.

Usage:
  uv run fetch_reviews.py <url> [--max 300] [--country us] [--lang en] --out reviews.json
  uv run fetch_reviews.py --from-file export.csv --app-name "My App" --out reviews.json
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import ipaddress
import json
import math
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


class UnsupportedSource(Exception):
    pass


class NoReviews(Exception):
    """The store answered but returned no reviews; the message says why and what to try instead."""


MAX_RESPONSE_BYTES = 25_000_000  # far above any store page or feed; stops a hostile or broken server from filling memory
MAX_RESPONSE_SECONDS = 120  # the socket timeout is per read, so a server sending a byte at a time needs a deadline too
MAX_CSV_FIELD = 10_000_000  # Python's csv module stops at 128 KB per cell by default; exports can hold long reviews


def web_url(url: str) -> bool:
    """Only http(s) links are fetched: urllib would also open file://, ftp:// and data: addresses."""
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def public_host(host: str) -> bool:
    """False when the host is, or resolves to, a loopback, private, link-local, or other non-public address.

    A review page (or a redirect from one) must not make the fetcher read a service on the user's own
    machine or network, such as a router page or a cloud metadata endpoint, and pass it on as reviews.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return True  # unresolvable: the request itself fails with a clearer error
    return all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)


class _WebOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to public http(s) addresses (urllib's default also follows ftp:// and local hosts)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not web_url(newurl) or not public_host(urllib.parse.urlparse(newurl).hostname):
            raise urllib.error.HTTPError(newurl, code, "redirect to a non-web or local address refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_WebOnlyRedirects)


def _too_slow() -> UnsupportedSource:
    return UnsupportedSource(f"the server took over {MAX_RESPONSE_SECONDS} s to answer; try again later or use an export with --from-file")


def http_get(url: str, accept: str = "application/json") -> bytes:
    """GET a public http(s) address. The whole request, from the lookup to the last byte, gets MAX_RESPONSE_SECONDS.

    The socket timeout covers one read at a time, so a server that sends a byte now and then (in the headers
    or the body) could otherwise keep the fetch open for hours. The request runs in a worker thread, and the
    caller stops waiting for it at the deadline.
    """
    if not web_url(url):
        raise UnsupportedSource(f"only http(s) links can be fetched, not {urllib.parse.urlparse(url).scheme or 'this'}: address")
    if not public_host(urllib.parse.urlparse(url).hostname):
        raise UnsupportedSource("links to local or private network addresses aren't fetched; save the reviews as an export and use --from-file")
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    result: dict = {}

    def work() -> None:
        try:
            result["body"] = _get_with_retries(request)
        except BaseException as error:  # noqa: BLE001 - raised again below, in the caller's thread
            result["error"] = error

    worker = threading.Thread(target=work, daemon=True)  # a daemon, so an abandoned request can't keep the process alive
    worker.start()
    worker.join(MAX_RESPONSE_SECONDS)
    if worker.is_alive():
        raise _too_slow()
    if "error" in result:
        raise result["error"]
    return result["body"]


def _get_with_retries(request: urllib.request.Request) -> bytes:
    for attempt in range(4):
        try:
            with _OPENER.open(request, timeout=30) as response:
                return _read_capped(response)
        except urllib.error.HTTPError as error:
            if error.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(2**attempt)
                continue
            raise
    raise RuntimeError(f"unreachable: {request.full_url}")


def _read_capped(response) -> bytes:
    deadline, chunks, size = time.monotonic() + MAX_RESPONSE_SECONDS, [], 0
    # read1 returns whatever has arrived; read(n) waits until it has all n bytes, so a slow body never reached the checks.
    read = getattr(response, "read1", None) or response.read
    while chunk := read(min(65536, MAX_RESPONSE_BYTES + 1 - size)):
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise UnsupportedSource(f"the response is larger than {MAX_RESPONSE_BYTES // 1_000_000} MB; save the reviews as an export and use --from-file")
        if time.monotonic() > deadline:
            raise _too_slow()
    return b"".join(chunks)


def get_json(url: str) -> dict:
    return json.loads(http_get(url))


def one_line(text: str) -> str:
    """Plain one-line text. Control characters become spaces, bidirectional overrides are removed, and runs of
    whitespace become one space.

    Strangers write review text and app metadata. A newline could start a fake section in sample.txt or brief.md,
    a terminal escape (ESC, BEL, OSC 52 clipboard writes) acts when the text is printed, NUL makes brief.md look
    binary to grep, and a right-to-left override shows text in a different order than it's stored.
    """
    text = re.sub(r"[\u202a-\u202e\u2066-\u2069]", "", re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def clean(text) -> str:
    text = text if isinstance(text, str) else "" if text is None else str(text)  # JSON-LD and exports can hold numbers
    return one_line(html.unescape(text))


APP_TEXT_FIELDS = ("name", "category", "description", "current_version", "store", "sort", "country", "url", "rating_scale")
APP_NUMBER_FIELDS = ("average_rating", "rating_count", "recommended_share")


def _plain_number(value) -> int | float | None:
    """A finite number (an export may write "4.5"); None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def plain_app(app: dict) -> dict:
    """The app block with only the fields the scripts use, each text field one line of plain text.

    The name, description, and category come from the store page or an export, so strangers wrote them too,
    and sample_reviews.py and triage.py print them. An export's app block can hold anything, so other keys go.
    """
    out = {}
    for key in APP_TEXT_FIELDS:
        if key in app:
            # one_line, not clean: html.unescape would turn a link's "&not..." into "¬...", and app names are plain text.
            out[key] = None if app[key] is None else one_line(str(app[key]))[:2000] or None
    for key in APP_NUMBER_FIELDS:
        if key in app:
            out[key] = _plain_number(app[key])
    out["name"] = out.get("name") or "the app"
    return out


def since_arg(value: str) -> str:
    """argparse type for --since: a real YYYY-MM-DD date. Dates are compared as text, so 2026-9-1 would drop every review."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            date.fromisoformat(value)
            return value
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"use a YYYY-MM-DD date like 2026-09-01, not {value!r}")


EARLIEST_YEAR = 2000  # older dates, and dates past next year, are typos or junk


def iso_date(value) -> str | None:
    """The date as given when it's ISO (YYYY-MM-DD, optionally with a time) and plausible; otherwise None.

    Dates drive the timeline, so junk can't be allowed through: two reviews dated 0001 and 9999 made
    ~120,000 monthly buckets and a 138 MB report, and a US-style 01/05/2025 crashed the run after the
    model calls were paid for. A date that isn't ISO is dropped (the review stays, undated) rather than guessed.
    """
    text = re.sub(r"\s+", " ", value if isinstance(value, str) else "").strip()[:40]
    if not re.match(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        day = date.fromisoformat(text[:10])
    except ValueError:
        return None
    if not EARLIEST_YEAR <= day.year <= date.today().year + 1:
        return None
    return text if re.fullmatch(r"([T ][0-9:.]+(Z|[+-]\d{2}:?\d{2})?)?", text[10:]) else text[:10]


def version_text(value) -> str | None:
    """A version string like '7.142.0', 'v12296', or '1.4 (beta)'; anything else (newlines, markup) is dropped."""
    text = re.sub(r"\s+", " ", "" if value is None else str(value)).strip()
    return text if re.fullmatch(r"[0-9A-Za-z._+() -]{1,60}", text) else None


def past(since: str | None, dates) -> bool:
    """True when a page reached reviews older than the start date, so paging can stop."""
    return bool(since) and any((d or "")[:10] < since for d in dates if d)


def window(reviews: list[dict], since: str, max_reviews: int) -> tuple[list[dict], str]:
    """Reviews on or after `since`; if there are more than max_reviews, an even sample across the window.

    Taking only the newest would describe the last few days of a long window, which can differ a lot
    (in testing, the newest 300 of 1,393 Strava reviews averaged 3.70★ against 3.26★ for the window).
    """
    inside = sorted((r for r in reviews if (r.get("date") or "")[:10] >= since), key=lambda r: r["date"], reverse=True)
    if len(inside) <= max_reviews:
        return inside, f"every review since {since}"
    step = len(inside) / max_reviews
    return [inside[int(i * step)] for i in range(max_reviews)], f"evenly sampled from {len(inside):,} reviews since {since}"


def utf8_output() -> None:
    """Print UTF-8 everywhere. On Windows, output sent to a file or pipe defaults to the ANSI code page,
    which can't encode ★ or most app names, and print() would crash after the work is done."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


SHORT_WINDOW_DAYS = 7  # reviews spanning fewer calendar days than this get a coverage note
VERSION_NOTE_MIN = 10  # reviews with a version needed before the current-version check means anything


def _version(value) -> str | None:
    """'v7.143.0' -> '7.143.0'; None for anything that isn't a version number (Play's "Varies with device")."""
    text = str(value or "").strip().lstrip("vV")
    return text if re.fullmatch(r"\d+(?:\.\d+)*", text) else None


def coverage_notes(app: dict, reviews: list[dict], as_of: str | None = None) -> list[str]:
    """Caveats about what the reviews cover: only a few days, or none on the store's current version.

    In testing, the 300 newest Duolingo App Store reviews covered 3 days, the newest was 2 days old,
    and 298 were on 7.142.0 while the store was on 7.143.0. Nothing said so, and a report like that
    reads as "what users think of the app" rather than "what they said that week about the last release".
    triage.py calls this too, so the notes reach brief.md even for a reviews.json fetched earlier.
    """
    notes = []
    try:
        dates = sorted(date.fromisoformat(r["date"][:10]) for r in reviews if r.get("date"))
    except ValueError:
        dates = []  # an export with another date format; no window to judge
    if len(dates) >= 2 and (days := (dates[-1] - dates[0]).days + 1) < SHORT_WINDOW_DAYS:
        age = ""
        if as_of:
            behind = (date.fromisoformat(as_of[:10]) - dates[-1]).days
            age = f"; the newest is from {behind} day{'s' * (behind != 1)} before the fetch" if behind > 0 else ""
        notes.append(
            f"the {len(reviews)} reviews cover only {days} day{'s' * (days != 1)} ({dates[0]} .. {dates[-1]}{age}). They show what "
            "users said in those days, which one release, outage, or promotion can dominate, not a typical stretch. For a wider "
            "window, fetch more (--max) or use --since"
            + (f"; Apple's public feed stops at the newest {APPLE_FEED_MAX}." if app.get("store") == "Apple App Store" else ".")
        )
    current = _version(app.get("current_version"))
    versions = Counter(v for v in (_version(r.get("version")) for r in reviews) if v)
    total = sum(versions.values())
    if current and total >= VERSION_NOTE_MIN and current not in versions:
        top, count = versions.most_common(1)[0]
        notes.append(
            f"none of the {total} reviews with a version are on the store's current version {current}; {count} are on {top}. "
            f"They describe earlier releases: some problems may already be fixed in {current}, and its own changes aren't reviewed yet."
        )
    return notes


def keep(review: dict, seen: set[str]) -> bool:
    """Skip repeats and reviews with no words while fetching, so --max counts reviews the triage can use.

    Apple's feed repeats reviews across pages, and Steam has text-free reviews; filtering only after
    cutting to --max returned fewer reviews than asked for.
    """
    if review["id"] in seen or not (review["text"] or review["title"]):
        return False
    seen.add(review["id"])
    return True


# --------------------------------------------------------------------------- Apple


APPLE_ALTERNATIVES = (
    "Try again later, or use an App Store Connect export (Ratings and Reviews) with --from-file. "
    "The App Store web page shows only about 10 featured reviews, not the newest."
)
APPLE_FEED_MAX = 500  # the public feed serves 10 pages of 50, newest first
APPLE_FEED_NOTE = (
    f"Apple's public review feed stops at the newest {APPLE_FEED_MAX} reviews (per country). For more, use an App Store "
    "Connect export (Ratings and Reviews) with --from-file."
)


def fetch_apple(url: str, max_reviews: int, country: str | None, since: str | None = None) -> dict:
    match = re.search(r"/id(\d+)", url)
    if not match:
        raise UnsupportedSource("App Store URL has no /id<digits> segment")
    app_id = match.group(1)
    path_cc = re.match(r"https?://apps\.apple\.com/([a-z]{2})/", url)
    cc = (country or (path_cc.group(1) if path_cc else "us")).lower()
    if not re.fullmatch(r"[a-z]{2}", cc):
        raise UnsupportedSource(f"country must be a two-letter code like us or gb, not {cc!r}")

    lookup = get_json(f"https://itunes.apple.com/lookup?id={app_id}&country={cc}")
    meta = (lookup.get("results") or [{}])[0]
    app = {
        "name": meta.get("trackName") or f"App Store app {app_id}",
        "category": meta.get("primaryGenreName"),
        "description": (meta.get("description") or "")[:1500],
        "current_version": meta.get("version"),
        "average_rating": meta.get("averageUserRating"),
        "rating_count": meta.get("userRatingCount"),
        "store": "Apple App Store",
        "sort": "newest first (public feed, max 500)",
        "country": cc,
        "url": url,
    }

    reviews: list[dict] = []
    seen: set[str] = set()
    # The public RSS feed serves at most 10 pages of 50 reviews, newest first.
    for page in range(1, APPLE_FEED_MAX // 50 + 1):
        if len(reviews) >= max_reviews:
            break
        feed_url = f"https://itunes.apple.com/{cc}/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json"
        try:
            entries = get_json(feed_url).get("feed", {}).get("entry", [])
        except urllib.error.HTTPError as error:
            if page == 1:
                raise NoReviews(f"Apple's review feed answered HTTP {error.code} for app {app_id} ({cc}). {APPLE_ALTERNATIVES}") from error
            break  # past the last page
        if isinstance(entries, dict):
            entries = [entries]
        entries = [e for e in entries if "im:rating" in e]
        if not entries:
            if page == 1:
                raise NoReviews(
                    f"Apple's public review feed returned no reviews for app {app_id} ({cc}). It sometimes comes back empty "
                    f"for every app, so this may not mean the app has no reviews. {APPLE_ALTERNATIVES}"
                )
            break
        for e in entries:
            review = {
                "id": "apple-" + e.get("id", {}).get("label", str(len(reviews))).rsplit("/", 1)[-1],
                "rating": int(e["im:rating"]["label"]),
                "title": clean(e.get("title", {}).get("label")),
                "text": clean(e.get("content", {}).get("label")),
                "date": e.get("updated", {}).get("label"),
                "version": e.get("im:version", {}).get("label"),
                "helpful_count": int(e.get("im:voteSum", {}).get("label") or 0),
            }
            if keep(review, seen):
                reviews.append(review)
        if past(since, (e.get("updated", {}).get("label") for e in entries)):
            break
    return {"app": app, "reviews": reviews[:max_reviews]}


# --------------------------------------------------------------------------- Google Play


def fetch_google_play(url: str, max_reviews: int, country: str | None, lang: str | None, since: str | None = None) -> dict:
    from google_play_scraper import Sort, app as play_app, reviews as play_reviews
    from google_play_scraper.exceptions import NotFoundError

    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    package = (query.get("id") or [None])[0]
    if not package or not re.fullmatch(r"[A-Za-z0-9_.]+", package):
        raise UnsupportedSource("Google Play URL has no valid ?id=<package> parameter")
    cc = (country or (query.get("gl") or ["us"])[0]).lower()
    hl = (lang or (query.get("hl") or ["en"])[0]).split("_")[0].lower()
    if not re.fullmatch(r"[a-z]{2}", cc) or not re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,4})?", hl):
        raise UnsupportedSource(f"country and language must be codes like us and en, not {cc!r} and {hl!r}")

    try:
        meta = play_app(package, lang=hl, country=cc)
    except NotFoundError as error:
        raise NoReviews(
            f"Google Play has no app {package} for country {cc}. Check the id= in the link; an app that isn't "
            "available in that country needs --country with one where it is."
        ) from error
    app = {
        "name": meta.get("title") or package,
        "category": meta.get("genre"),
        "description": clean(re.sub(r"<[^>]+>", " ", meta.get("description") or ""))[:1500],  # Play descriptions carry <b>, <br>
        "current_version": meta.get("version"),
        "average_rating": meta.get("score"),
        "rating_count": meta.get("ratings"),
        "store": "Google Play",
        "sort": "newest first",
        "country": cc,
        "url": url,
    }
    want = max_reviews + max_reviews // 10 + 10  # a little extra, since repeats and empty reviews are dropped
    if since:
        # Walk back in pages until the reviews are older than the start date.
        result, token = [], None
        while len(result) < want:
            batch, token = play_reviews(package, lang=hl, country=cc, sort=Sort.NEWEST, count=200, continuation_token=token)
            result += batch
            if not batch or token is None or past(since, (r["at"].isoformat() for r in batch if r.get("at"))):
                break
    else:
        result, _ = play_reviews(package, lang=hl, country=cc, sort=Sort.NEWEST, count=want)
    seen: set[str] = set()
    reviews = [
        {
            "id": "play-" + r["reviewId"],
            "rating": int(r["score"]),
            "title": "",
            "text": clean(r.get("content")),
            # google-play-scraper builds `at` with datetime.fromtimestamp, i.e. naive local time.
            "date": r["at"].astimezone(timezone.utc).isoformat() if r.get("at") else None,
            "version": r.get("appVersion") or r.get("reviewCreatedVersion"),
            "helpful_count": int(r.get("thumbsUpCount") or 0),
        }
        for r in result
    ]
    reviews = [r for r in reviews if keep(r, seen)]
    return {"app": app, "reviews": reviews[:max_reviews]}


# --------------------------------------------------------------------------- Steam

# Steam reviews are written in BBCode. The tags are markup, not the reviewer's words; text like "[1]" or "[sic]" stays.
STEAM_MARKUP = re.compile(
    r"\[/?(?:h[1-6]|b|i|u|s|strike|spoiler|noparse|hr|code|quote|url|list|olist|\*|table|tr|td|th|img|previewyoutube)(?:=[^\]\n]{0,500})?\]",
    re.IGNORECASE,
)


def fetch_steam(url: str, max_reviews: int, lang: str | None, since: str | None = None) -> dict:
    match = re.search(r"/app/(\d+)", url)
    if not match:
        raise UnsupportedSource("Steam URL has no /app/<id> segment")
    app_id = match.group(1)
    details = get_json(f"https://store.steampowered.com/api/appdetails?appids={app_id}&l=english")
    data = (details.get(app_id) or {}).get("data") or {}
    app = {
        "name": data.get("name") or f"Steam app {app_id}",
        "category": ", ".join(g["description"] for g in data.get("genres", [])[:3]) or None,
        "description": clean(data.get("short_description"))[:1500],
        "current_version": None,
        "average_rating": None,
        "rating_count": None,
        "store": "Steam",
        "rating_scale": "thumbs",  # recommended / not recommended, stored as rating 5 / 1 below
        "recommended_share": None,
        "sort": "most recent first",
        "country": None,
        "url": url,
    }
    language = {"en": "english"}.get((lang or "en").lower(), "all")
    reviews: list[dict] = []
    seen: set[str] = set()
    cursor = "*"
    while len(reviews) < max_reviews:
        params = urllib.parse.urlencode(
            {"json": 1, "filter": "recent", "language": language, "num_per_page": 100, "cursor": cursor, "purchase_type": "all"}
        )
        page = get_json(f"https://store.steampowered.com/appreviews/{app_id}?{params}")
        summary = page.get("query_summary") or {}
        if cursor == "*" and isinstance(summary.get("total_reviews"), int) and summary["total_reviews"] > 0:
            # All-time counts for the same language filter; only the first page carries them.
            app["rating_count"] = summary["total_reviews"]
            app["recommended_share"] = round(int(summary.get("total_positive") or 0) / summary["total_reviews"], 4)
        batch = page.get("reviews") or []
        if not batch:
            break
        for r in batch:
            review = {
                "id": f"steam-{r['recommendationid']}",
                # Steam has thumbs up/down instead of stars; map to 5 / 1 so code can still split by rating.
                "rating": 5 if r.get("voted_up") else 1,
                "title": "",
                "text": clean(STEAM_MARKUP.sub(" ", str(r.get("review") or ""))),
                "date": datetime.fromtimestamp(r["timestamp_created"], tz=timezone.utc).isoformat(),
                "version": None,
                "helpful_count": int(r.get("votes_up") or 0),
            }
            if keep(review, seen):
                reviews.append(review)
        if page.get("cursor") in (None, cursor):
            break
        if past(since, (datetime.fromtimestamp(r["timestamp_created"], tz=timezone.utc).isoformat() for r in batch)):
            break
        cursor = page["cursor"]
    return {"app": app, "reviews": reviews[:max_reviews]}


# --------------------------------------------------------------------------- JSON-LD fallback


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _rating_or_none(value) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except (TypeError, ValueError, OverflowError):
        return None


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")  # keeps every index, unlike str.lower()


def _jsonld_blocks(page: str) -> tuple[list[str], str | None]:
    """The text of each <script type="application/ld+json"> block, and the page <title>, in one forward pass.

    A regex like <script ...>(.*?)</script> rescans the rest of the page for every opening tag that never closes,
    so a hostile page full of them took minutes. Here each search starts where the last one ended.
    """
    lowered, blocks, at = page.translate(_ASCII_LOWER), [], 0
    while (start := lowered.find("<script", at)) != -1:
        tag_end = lowered.find(">", start)
        close = lowered.find("</script", tag_end) if tag_end != -1 else -1
        if close == -1:
            break
        if re.search(r"""type\s*=\s*["']?application/ld\+json""", lowered[start:tag_end]):
            blocks.append(page[tag_end + 1 : close])
        at = close + 1
    title_start = lowered.find("<title>")
    title_end = lowered.find("</title>", title_start) if title_start != -1 else -1
    return blocks, page[title_start + 7 : title_end] if title_end != -1 else None


def fetch_jsonld(url: str, max_reviews: int) -> dict:
    try:
        page = http_get(url, accept="text/html").decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError) as error:
        raise UnsupportedSource(f"could not download the page directly ({error})") from error
    blocks, title = _jsonld_blocks(page)
    reviews: list[dict] = []
    name = None
    for block in blocks:
        try:
            nodes = list(_walk(json.loads(block)))
        except (ValueError, RecursionError):  # not JSON, or nested too deeply to be structured data
            continue
        for node in nodes:
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if name is None and any(k in ("SoftwareApplication", "MobileApplication", "Product", "Organization", "LocalBusiness") for k in kinds):
                name = clean(node.get("name")) or None
            if "Review" in kinds and (node.get("reviewBody") or node.get("description")):
                rating = node["reviewRating"].get("ratingValue") if isinstance(node.get("reviewRating"), dict) else None
                reviews.append(
                    {
                        "id": f"web-{len(reviews)}",
                        "rating": _rating_or_none(rating),
                        "title": clean(node.get("name") or node.get("headline")),
                        "text": clean(node.get("reviewBody") or node.get("description")),
                        "date": _text_or_none(node.get("datePublished")),
                        "version": None,
                        "helpful_count": 0,
                    }
                )
    if not reviews:
        raise UnsupportedSource("no schema.org Review objects found in the page's JSON-LD")
    app = {
        "name": name or clean(title) or urllib.parse.urlparse(url).netloc,
        "category": None,
        "description": "",
        "current_version": None,
        "average_rating": None,
        "rating_count": None,
        "store": urllib.parse.urlparse(url).netloc,
        "sort": "as listed on the page",
        "country": None,
        "url": url,
    }
    return {"app": app, "reviews": reviews[:max_reviews]}


# --------------------------------------------------------------------------- local files

FIELD_ALIASES = {
    "text": ["text", "review", "review_text", "review_body", "body", "content", "comment", "reviewbody", "description"],
    "title": ["title", "review_title", "subject", "headline", "summary"],
    "rating": ["rating", "stars", "star_rating", "score", "review_rating"],
    "date": ["date", "created_at", "review_date", "review_last_update_date", "review_last_update_date_and_time", "review_submit_date_and_time", "last_updated", "updated", "submitted", "submitted_at", "timestamp"],
    "version": ["version", "app_version", "app_version_name", "build"],
    "id": ["id", "review_id", "reviewid", "review_link"],
}


def _pick(row: dict, field: str):
    lowered = {re.sub(r"[\s\-]+", "_", k.strip().lower()): v for k, v in row.items() if k}
    for alias in FIELD_ALIASES[field]:
        if lowered.get(alias) not in (None, ""):
            return lowered[alias]
    return None


def _text_or_none(value) -> str | None:
    return None if value in (None, "") else str(value)


def _count(value) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError, OverflowError):
        return 0


def load_file(path: Path, app_name: str | None) -> dict:
    try:
        return _load_file(path, app_name)
    except RecursionError as error:
        raise NoReviews(f"{path.name} is nested too deeply to be a review export; nothing written.") from error
    except (OSError, ValueError, csv.Error) as error:  # ValueError covers broken JSON and undecodable text
        raise NoReviews(
            f"{path.name} could not be read as a review export ({error}). Check that it's the CSV or JSON file the store "
            "or tool exported; nothing written."
        ) from error


def _load_file(path: Path, app_name: str | None) -> dict:
    if path.suffix.lower() == ".csv":
        raw = path.read_bytes()
        # Play Console review exports are UTF-16; most other tools write UTF-8, often with a BOM.
        encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            text = raw.decode("cp1252", errors="replace")  # Excel on Windows saves "CSV" in the ANSI code page
        try:
            # Excel in many European locales separates columns with semicolons. Only the delimiter is taken from the
            # sniffer: it also guesses quoting from the sample, and when the first 20,000 characters had no doubled
            # quote mark ("") it read every later review that has one as broken rows.
            delimiter = csv.Sniffer().sniff(text[:20000], delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
        csv.field_size_limit(max(csv.field_size_limit(), MAX_CSV_FIELD))
        rows = list(csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter))
        app = {"name": app_name or path.stem}
    else:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(data, dict):
            rows = data.get("reviews", [])
            app = data.get("app") if isinstance(data.get("app"), dict) else {"name": app_name or path.stem}
        else:
            rows, app = data, {"name": app_name or path.stem}
    if not isinstance(rows, list):
        rows = []
    if app_name:
        app["name"] = app_name
    app.setdefault("name", path.stem)
    reviews = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue  # a stray string or number in a JSON list isn't a review
        text = clean(str(_pick(row, "text") or ""))
        rating = _pick(row, "rating")
        try:
            rating = int(round(float(rating))) if rating is not None else None
        except (TypeError, ValueError, OverflowError):
            rating = None
        reviews.append(
            {
                "id": str(_pick(row, "id") or f"file-{i}"),
                "rating": rating,
                "title": clean(str(_pick(row, "title") or "")),
                "text": text,
                "date": _text_or_none(_pick(row, "date")),
                "version": _text_or_none(_pick(row, "version")),
                "helpful_count": _count(row.get("helpful_count")),
            }
        )
    app.setdefault("store", "file")
    app.setdefault("sort", "as provided in the file")
    # Only the file's name: a full path would put the user's folders (and username) into a report they share.
    app.setdefault("url", path.name)
    return {"app": app, "reviews": reviews}


# --------------------------------------------------------------------------- main


def _on(host: str, *domains: str) -> bool:
    """The host is one of the domains or a subdomain of one (evilsteamcommunity.com is neither)."""
    return any(host == d or host.endswith("." + d) for d in domains)


def fetch(url: str, max_reviews: int, country: str | None, lang: str | None, since: str | None = None) -> dict:
    if not web_url(url):
        raise UnsupportedSource("give an http(s) link to a review page, or a local export with --from-file")
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if _on(host, "apps.apple.com", "itunes.apple.com"):
        return fetch_apple(url, max_reviews, country, since)
    if _on(host, "play.google.com"):
        return fetch_google_play(url, max_reviews, country, lang, since)
    if _on(host, "store.steampowered.com", "steamcommunity.com"):
        return fetch_steam(url, max_reviews, lang, since)
    return fetch_jsonld(url, max_reviews)


def main() -> int:
    utf8_output()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?")
    parser.add_argument("--from-file", type=Path, help="normalize a local CSV/JSON export instead of fetching")
    parser.add_argument("--app-name", help="app name when loading from a file")
    parser.add_argument("--max", type=int, help="maximum reviews to keep (default 300 for a link; every review in a --from-file export)")
    parser.add_argument("--country", help="store country code, e.g. us, gb (default: from URL or us)")
    parser.add_argument("--lang", help="review language for Google Play/Steam (default: from URL or en)")
    parser.add_argument("--since", type=since_arg, help="only reviews on/after this date (YYYY-MM-DD); pages back to it, then samples --max evenly across the window")
    parser.add_argument("--fetch-limit", type=int, default=5000, help="with --since, stop paging back after this many reviews (default 5000)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if not args.url and not args.from_file:
        parser.error("give a URL or --from-file")
    if args.max is not None and args.max < 1:
        parser.error("--max must be at least 1")
    if args.max is None and not args.from_file:
        args.max = 300
    # With --since, page back far enough to cover the window, and never fetch fewer than --max asks for.
    # (An export is read whole, and --max may be unset for one.)
    limit = max(args.fetch_limit, args.max or 0) if args.since else args.max
    try:
        data = load_file(args.from_file, args.app_name) if args.from_file else fetch(args.url, limit, args.country, args.lang, args.since)
    except UnsupportedSource as error:
        print(f"UNSUPPORTED: {error}", file=sys.stderr)
        return 2
    except NoReviews as error:
        print(f"NO REVIEWS: {error}", file=sys.stderr)
        return 3
    except urllib.error.HTTPError as error:
        print(f"NO REVIEWS: the store answered HTTP {error.code} ({error.reason}). Check the link, or try again later; nothing written.", file=sys.stderr)
        return 3
    except OSError as error:  # offline, DNS failure, refused or reset connection, socket timeout
        reason = getattr(error, "reason", None) or error
        print(f"NO REVIEWS: could not reach the store ({reason}). Check the internet connection and the link, then try again; nothing written.", file=sys.stderr)
        return 3
    data["app"] = plain_app(data["app"])

    seen: set[str] = set()
    reviews = []
    undated = 0
    for review in data["reviews"]:
        # Every field a page or export controls is reduced to plain one-line text before anything reads it.
        review["id"] = clean(review.get("id"))[:200]
        review["title"], review["text"] = clean(review.get("title")), clean(review.get("text"))
        review["version"] = version_text(review.get("version"))
        date_given = review.get("date")
        review["date"] = iso_date(date_given)
        undated += bool(date_given) and review["date"] is None
        # Review sites often use the text's opening as the title ("I had a great experience with…");
        # Jev would read it twice. Short store headlines like "Love it" are kept.
        lead = review["title"].rstrip(" .…")
        if len(lead) >= 20 and review["text"].startswith(lead):
            review["title"] = ""
        if not review["text"] and not review["title"]:
            continue
        if review["id"] in seen:
            continue
        seen.add(review["id"])
        reviews.append(review)
    fetched, every = len(reviews), reviews
    oldest = min((r["date"] for r in reviews if r.get("date")), default=None)
    if args.since:
        reviews, data["app"]["sort"] = window(reviews, args.since, args.max or len(reviews))
    elif args.from_file and args.max and len(reviews) > args.max:
        # Like a --since window: an even sample across the export's dates, not just its first rows.
        ordered = sorted(reviews, key=lambda r: r.get("date") or "", reverse=True)
        step = len(ordered) / args.max
        reviews = [ordered[int(i * step)] for i in range(args.max)]
        data["app"]["sort"] = f"evenly sampled {args.max:,} of the file's {fetched:,} reviews"
    data["reviews"] = reviews
    if not reviews:
        # An empty reviews.json would run through triage as a report about nothing.
        source = "the file" if args.from_file else "the page"
        if args.since and fetched:
            # Say why the window is empty: an export's US-style dates (09/20/2026) count as undated, for one.
            undated_count = sum(not r.get("date") for r in every)
            newest = max((r["date"][:10] for r in every if r.get("date")), default=None)
            why = []
            if undated_count:
                why.append(f"{undated_count:,} have no YYYY-MM-DD date")
            if newest:
                why.append(f"the newest dated one is from {newest}")
            print(f"NO REVIEWS: {source} gave {fetched:,} reviews, but none dated on or after {args.since} ({'; '.join(why)}); nothing written.", file=sys.stderr)
        else:
            print(f"NO REVIEWS: {source} gave no reviews with text; nothing written.", file=sys.stderr)
        return 3
    data["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # Notes about what the fetch could not get. They are saved in reviews.json so triage.py can put them in
    # brief.md's caveats; the coverage notes (window length, current version) triage.py recomputes itself.
    notes = []
    apple = data["app"].get("store") == "Apple App Store"
    if args.since:
        if not args.from_file and oldest and fetched >= limit and oldest[:10] >= args.since:
            notes.append(f"stopped at --fetch-limit {limit} before reaching {args.since} (oldest fetched {oldest[:10]}); the window is not fully covered")
        elif not args.from_file and oldest and oldest[:10] > args.since:
            if apple:
                notes.append(f"{APPLE_FEED_NOTE} The fetched reviews reach back to {oldest[:10]}, not {args.since}; the window is not fully covered.")
            else:
                notes.append(f"the source's reviews reach back only to {oldest[:10]}, not {args.since}")
    elif args.url and not args.from_file and len(reviews) < args.max:
        if apple and args.max > APPLE_FEED_MAX:
            notes.append(f"{APPLE_FEED_NOTE} Got {len(reviews)} of the {args.max} requested.")
        else:
            notes.append(f"only {len(reviews)} of the {args.max} requested reviews were available (the store had no more, or some were duplicates or empty)")
    if args.from_file and not args.since and len(reviews) < fetched:
        notes.append(f"the export held {fetched:,} reviews; {len(reviews):,} were evenly sampled across its dates (--max)")
    if undated:
        notes.append(f"{undated} reviews had dates that aren't YYYY-MM-DD (or are implausible), so they count as undated in the timeline")
    data["fetch_notes"] = notes

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    ratings = [r["rating"] for r in reviews if r["rating"] is not None]
    dates = sorted(r["date"] for r in reviews if r.get("date"))
    print(f"app: {data['app']['name']} ({data['app'].get('store')})")
    if ratings and data["app"].get("rating_scale") == "thumbs":
        print(f"reviews: {len(reviews)}, {sum(r >= 3 for r in ratings) / len(ratings):.0%} recommended")
    else:
        print(f"reviews: {len(reviews)}" + (f", mean rating {sum(ratings) / len(ratings):.2f}" if ratings else ""))
    if args.since:
        print(f"window: {data['app']['sort']} ({fetched} fetched)")
    for note in notes + coverage_notes(data["app"], reviews, data["fetched_at"]):
        print(f"note: {note}")
    if dates:
        print(f"date range: {dates[0][:10]} .. {dates[-1][:10]}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
