"""Render summary.json into a self-contained HTML report in the Signals to Solutions style.

Set in Avenir Next where the system has it (Apple devices); elsewhere Nunito Sans loads from Google Fonts,
the page's only external request, and offline it falls back to the system sans.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from collections import Counter
from datetime import date

SENTIMENT_KEYS = ["very negative", "negative", "mixed / neutral", "positive", "very positive"]


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def pct(part: int | float, whole: int | float) -> str:
    return f"{(100 * part / whole):.0f}%" if whole else "0%"


# Steam has thumbs up/down instead of stars. fetch_reviews.py stores them as rating 5 (recommended) and 1 (not
# recommended) so code can still split by rating, and marks the app block with rating_scale "thumbs". Showing
# those as "4.52★" would invent a star scale Steam doesn't have, so render_report sets THUMBS for the helpers below.
THUMBS = False


def is_thumbs(app: dict) -> bool:
    return app.get("rating_scale") == "thumbs" or (not app.get("rating_scale") and app.get("store") == "Steam")


def rating_label(rating) -> str:
    if rating is None:
        return ""
    if THUMBS:
        return "Recommended" if rating >= 3 else "Not recommended"
    return f"{rating}★"


def recommend_share(mean_rating) -> str:
    """With ratings of only 1 and 5, the mean maps exactly to the share that recommends: (mean - 1) / 4."""
    return "–" if mean_rating is None else f"{(mean_rating - 1) / 4:.0%}"


# ----------------------------------------------------------------- tiny markdown


def _inline(text: str) -> str:
    text = esc(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", text)
    # In-page links only, e.g. [Pricing](#area-pricing_billing) to an area row.
    text = re.sub(r"\[([^\]]+)\]\((#[\w-]+)\)", r'<a href="\2">\1</a>', text)
    return text


def markdown(md: str, sections: bool = False) -> str:
    """With sections=True, each '## ' section folds under its heading; only the first starts open."""
    out: list[str] = []
    list_tag: str | None = None
    paragraph: list[str] = []
    quote: list[str] = []
    in_section = False
    first_section = True

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def close_list() -> None:
        nonlocal list_tag
        if list_tag:
            out.append(f"</{list_tag}>")
            list_tag = None

    def flush_quote() -> None:
        # One quoted review per "> " line, so lines stay separate rather than joining into one paragraph.
        if quote:
            out.append("<blockquote>" + "".join(f"<p>{_inline(q)}</p>" for q in quote) + "</blockquote>")
            quote.clear()

    for raw in md.splitlines():
        line = raw.rstrip()
        heading = re.match(r"^(#{1,4})\s+(.*)", line)
        bullet = re.match(r"^\s*[-*]\s+(.*)", line)
        numbered = re.match(r"^\s*(?:(\d+)[.)])\s+(.*)", line)
        blockquote = re.match(r"^\s*>\s?(.*)", line)
        if blockquote:
            flush_paragraph()
            close_list()
            if blockquote.group(1).strip():
                quote.append(blockquote.group(1).strip())
            continue
        flush_quote()
        if not line.strip():
            flush_paragraph()
            close_list()
        elif heading:
            flush_paragraph()
            close_list()
            depth = len(heading.group(1))
            level = min(depth + 1, 4)
            title = f"<h{level}>{_inline(heading.group(2))}</h{level}>"
            if sections and depth <= 2:
                if in_section:
                    out.append("</details>")
                in_section = depth == 2
                if in_section:
                    out.append(f'<details class="nsec"{" open" if first_section else ""}><summary>{title}</summary>')
                    first_section = False
                    continue
            out.append(title)
        elif bullet or numbered:
            flush_paragraph()
            tag = "ul" if bullet else "ol"
            if list_tag != tag:
                close_list()
                # A blank line between "1." and "2." closes the list; keep the writer's numbering when it reopens.
                start = int(numbered.group(1)) if numbered else 1
                out.append(f'<ol start="{start}">' if start != 1 else f"<{tag}>")
                list_tag = tag
            out.append(f"<li>{_inline(bullet.group(1) if bullet else numbered.group(2))}</li>")
        else:
            close_list()
            paragraph.append(line.strip())
    flush_quote()
    flush_paragraph()
    close_list()
    if in_section:
        out.append("</details>")
    return "\n".join(out)


# ----------------------------------------------------------------- pieces


# One definition per label, shared by the badge tooltips (looked up in JS, so badges stay small) and the glossary.
LABELS = {
    "blocking": "The app or the reviewer's main task doesn't work at all, or they lost data or money.",
    "degraded": "Something works poorly or unreliably, or a feature is missing or was removed.",
    "minor": "A cosmetic issue, small inconvenience, or dislike of a design or business decision.",
    "bug": "Describes something in the app that is broken or behaves incorrectly.",
    "request": "Asks for something to be added, or for a removed feature to come back.",
    "churn risk": "Says they uninstalled, cancelled, or are switching, or will if nothing changes.",
    "since update": "Ties the problem to a recent update, new version, or redesign.",
    "repro detail": "Gives a detail an engineer could use to reproduce the problem: device, OS version, steps, or trigger.",
    "sentiment": "How the reviewer feels, judged from their words, not their rating.",
    "excerpt": "Cut from a longer review. Open Full review to read all of it.",
}
FLAG_NAMES = {"reports_bug": "bug", "churn_signal": "churn risk", "requests_feature": "request"}


def badge(label: str, cls: str = "") -> str:
    return f'<span class="badge{(" " + cls) if cls else ""}" data-label="{esc(label)}">{esc(label)}</span>'


def badges(q: dict) -> str:
    parts = []
    if q.get("severity") in ("minor", "degraded", "blocking"):
        parts.append(badge(q["severity"], f'sev-{q["severity"]}'))
    if q.get("bug"):
        parts.append(badge("bug"))
    if q.get("feature_request"):
        parts.append(badge("request"))
    if q.get("churn"):
        parts.append(badge("churn risk", "churn"))
    if q.get("after_update"):
        parts.append(badge("since update"))
    if q.get("repro_detail"):
        parts.append(badge("repro detail"))
    return "".join(parts)


def unsure_line(q: dict, area_ids: dict) -> str:
    """For borderline reviews: which labels Jev was unsure about, and how sure it was."""
    names = {f"issue:{i}": f"a problem with {n}" for n, i in area_ids.items()}
    probs = q.get("borderline_probs") or {}
    parts = [
        esc(names.get(k) or FLAG_NAMES.get(k) or k) + (f" ({probs[k]:.0%})" if k in probs else "")
        for k in q.get("borderline") or []
    ]
    return f'<div class="unsure">Unsure: {", ".join(parts)}</div>' if parts else ""


ELLIPSIS = '<span class="ellip" data-label="excerpt">…</span>'


def excerpt_text(quote: str, full: str | None) -> str:
    """The quote, escaped, with … wherever it was cut from a longer review (before it, after it, or both)."""
    clipped = quote.endswith("…")
    core = " ".join(quote.rstrip("…").split())
    text = " ".join((full or "").split())
    at = text.find(core) if core else -1
    if at == -1:  # e.g. the review's title stood in for a one-word review
        return esc(quote.rstrip("…")) + ("&thinsp;" + ELLIPSIS if clipped else "")
    before = bool(text[:at].strip())
    after = clipped or bool(text[at + len(core):].strip(" .!?…'\""))
    return (ELLIPSIS + "&thinsp;" if before else "") + esc(core) + ("&thinsp;" + ELLIPSIS if after else "")


def thumb_cell(rating) -> str:
    if rating is None:
        return ""
    label = rating_label(rating)
    return f'<span role="img" aria-label="{label}" title="{label}">{"👍" if rating >= 3 else "👎"}</span>'


def quote_row(q: dict, show_area: bool = False, area_ids: dict | None = None) -> str:
    """One review as a table row: the verbatim quote first, then one column per field, like a spreadsheet."""
    area_ids = area_ids or {}
    title = f'<div class="q-title">{esc(q["title"])}</div>' if q.get("title") else ""
    full = ""
    if q.get("full_text") and q["full_text"] != q["quote"]:
        full = f'<details class="full"><summary>Full review</summary><p>{esc(q["full_text"])}</p></details>'
    sentiment, attrs, feeling = q.get("sentiment"), "", ""
    if sentiment in SENTIMENT_KEYS:
        attrs = f' data-sentiment="{esc(sentiment)}"'
        feeling = f'<span class="senti" data-label="sentiment"><span class="swatch s{SENTIMENT_KEYS.index(sentiment)}"></span>{esc(sentiment)}</span>'
    if q.get("rating") is not None:
        attrs += f' data-rating="{esc(q["rating"])}"'
    cells = [
        f'<td class="c-quote">{title}<p class="q-text">{excerpt_text(q["quote"], q.get("full_text"))}</p>{full}</td>',
        f'<td class="c-stars">{thumb_cell(q.get("rating")) if THUMBS else esc(rating_label(q.get("rating")))}</td>',
        f'<td class="c-date">{esc(q.get("date") or "")}</td>',
        f'<td class="c-ver">{esc(str(q.get("version") or "").lstrip("vV"))}</td>',
    ]
    if show_area:
        area = q.get("area") or ""
        link = f'<a href="#area-{esc(area_ids[area])}">{esc(area)}</a>' if area in area_ids else esc(area)
        cells.append(f'<td class="c-area">{link}</td>')
    cells += [f'<td class="c-senti">{feeling}</td>', f'<td class="c-labels">{badges(q)}{unsure_line(q, area_ids)}</td>']
    return f'<tr class="quote"{attrs}>{"".join(cells)}</tr>'


def quote_table(items: list[dict], show_area: bool = False, area_ids: dict | None = None) -> str:
    heads = [("w-quote", "Quote (verbatim)"), ("w-stars", "Rating"), ("w-date", "Date"), ("w-ver", "Version")]
    if show_area:
        heads.append(("w-area", "Product area"))
    heads += [("w-senti", "Sentiment"), ("w-labels", "Labels")]
    head = "".join(f'<th scope="col" class="{c}">{t}</th>' for c, t in heads)
    rows = "".join(quote_row(q, show_area, area_ids) for q in items)
    return f'<table class="qtable"><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>'


def source_link(app: dict, text: str | None = None) -> str:
    """Link to the review page the reviews came from; empty for a local file."""
    url = app.get("url") or ""
    if not url.startswith(("https://", "http://")):
        return ""
    return f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(text or url)}</a>'


def about_section(summary: dict, has_narrative: bool) -> str:
    """Plain-language intro to what produced the report, hidden until the header's info button opens it."""
    app, o, jev, t = summary["app"], summary["overview"], summary["jev"], summary["thresholds"]
    n = o["reviews_analyzed"]
    store = app.get("store") if app.get("store") not in (None, "file") else "an uploaded export"
    order = f', {app["sort"]}' if app.get("sort") else ""
    dates = f', dated {" to ".join(o["date_range"])}' if o.get("date_range") else ""
    link = source_link(app, "the store's public review page")
    left_out = o.get("reviews_total", n) - n
    reviews = f"{n} reviews from {esc(store)}{esc(order)}{esc(dates)}." + (f" Taken from {link}." if link else "")
    if left_out:
        reviews += f" {left_out} more were left out as off-topic or failed to process."
    cost = jev.get("total_cost_usd") or 0
    cost_note = f" All of Jev's answers cost about ${cost:.2f}." if cost >= 0.01 else ""
    quotes = (
        "Every quote is in the reviewer's own words; nothing is paraphrased or generated. Jev picks the sentence that makes "
        "the point, and when none stands out the review is shown as written, shortened if long."
    )
    if has_narrative:
        quotes += " The summary below was written by Claude from these counts and quotes."
    steps = [
        ("Reviews", reviews),
        (
            "Jev labels each review",
            f'Jev ({esc(jev.get("model") or "jev-latest")}), TypeSafe\'s model for typed judgments, read every review and answered '
            f'narrow questions about it: how the reviewer feels, how severe any problem is, which of {len(summary["areas"])} product '
            f"areas it concerns, and whether it's a bug, a feature request, a sign they'll leave, or tied to an update. "
            f"Each answer comes with a probability.{cost_note}",
        ),
        (
            "Code counts and ranks",
            f'A label counts only when Jev is at least {t["yes"]:.0%} sure; {t["unsure_low"]:.0%}–{t["yes"]:.0%} goes to the borderline '
            "list instead. Every count, ranking, and priority score is computed in code. Star ratings, dates, and versions are never shown to Jev.",
        ),
        ("Quotes stay verbatim", quotes),
    ]
    items = "".join(f"<li><strong>{title}</strong>{text}</li>" for title, text in steps)
    return f"""<section class="about" id="about" tabindex="-1" hidden>
<div class="about-head"><h2>How this report was made</h2><button type="button" class="close" aria-label="Close">×</button></div>
<ol class="steps">{items}</ol>
{glossary(summary)}
<p class="muted about-more"><a href="#method">Full method and caveats ↓</a></p></section>"""


def glossary(summary: dict) -> str:
    t = summary["thresholds"]
    w = t["priority_weights"]
    terms = [
        ("Priority", f"How urgent an area is; areas are ranked by it. Each review reporting a problem there adds {w['base']:g}, plus {w['severity']:g}× its "
         f"severity (0–3), plus {w['churn']:g} if the reviewer says they'll leave, plus {w['regression']:g} if they tie it to an update."),
        ("Likely rank", "Where the area could rank with a different sample of reviews: the range it fell in for 90% of 500 resamples "
         "of these reviews. The dot is its rank here. Areas whose ranges overlap aren't clearly ordered."),
        ("By month", "Each bar is the share of that month's reviews (or week's, for short windows) reporting a problem in the area, "
         "on one scale for all areas. Faded bars: periods with fewer than 20 reviews."),
        ("Fewer / more lately", "Complaints about the area fell (or rose) between the earlier and later half of the reviews by more "
         "than chance would usually explain (one-sided Fisher exact test, p < 0.025). A prompt to check releases or analytics, not proof."),
        ("Issues", "Reviews reporting a problem in that area, and their share of all reviews. A review can count in several areas."),
        ("Severity", "The average on a 0–3 scale: 1 minor, 2 degraded, 3 blocking."),
        *((name.capitalize(), LABELS[name]) for name in ("blocking", "degraded", "minor")),
        ("Churn risk", LABELS["churn risk"]),
        ("Since update", LABELS["since update"]),
        ("Bug", LABELS["bug"]),
        ("Request", LABELS["request"]),
        ("Repro detail", LABELS["repro detail"]),
        ("Praise", "Reviews praising that area."),
        ("Sentiment", LABELS["sentiment"] + " Five levels, from very negative to very positive."),
        ("Unsure", f"Jev was {t['unsure_low']:.0%}–{t['yes']:.0%} sure. Unsure labels aren't counted anywhere; they're listed for a human to check."),
    ]
    rows = "".join(f"<dt>{esc(term)}</dt><dd>{esc(text)}</dd>" for term, text in terms)
    return f'<h3 id="glossary">What the labels mean</h3><dl class="glossary">{rows}</dl>'


AREA_PREVIEW = 6  # area rows shown before "Show all"
VERSION_PREVIEW = 6  # version rows shown before "Show all"


THIN_VERSION = 10  # versions with fewer reviews are grayed: one review moves their shares by 10+ points


def version_key(version) -> tuple:
    """Release order for version strings like '12296', 'v2.10.1', or '1.4 (beta)'."""
    return tuple(int(x) for x in re.findall(r"\d+", str(version))) or (0,)


def show_rows(total: int, shown: int, noun: str, less: str = "") -> str:
    """Toggle under a list that shows only its first rows; inert (and every row visible) without JavaScript."""
    more, less = f"Show all {total} {noun}", less or f"Show top {shown} only"
    return f'<button type="button" class="show-rows" aria-expanded="false" data-more="{esc(more)}" data-less="{esc(less)}">{esc(more)}</button>'


def kpi(label: str, value, note: str = "") -> str:
    note_html = f'<div class="kpi-note">{esc(note)}</div>' if note else ""
    value = esc(value)
    if value.endswith("★"):  # a display-size star overpowers the figure
        value = value[:-1] + '<span class="star">★</span>'
    return f'<div class="kpi"><div class="kpi-label">{esc(label)}</div><div class="kpi-value">{value}</div>{note_html}</div>'


def sentiment_bar(dist: dict, total: int, clickable: bool = False) -> str:
    """With clickable=True, each segment and legend entry opens those reviews in the All reviews tab."""
    segments, legend = [], []
    for i, key in enumerate(SENTIMENT_KEYS):
        count = dist.get(key, 0)
        link = f' data-senti="{esc(key)}"' if clickable and count else ""
        if count:
            hint = " Click to read them." if clickable else ""
            segments.append(f'<div class="seg s{i}" style="flex:{count}" data-tip="{esc(key)}: {count} reviews ({pct(count, total)}).{hint}"{link}></div>')
        entry = f'<span class="swatch s{i}"></span>{esc(key)} <strong>{count}</strong> <span class="muted">{pct(count, total)}</span>'
        legend.append(f'<li><button type="button" class="legend-btn"{link}>{entry}</button></li>' if link else f"<li>{entry}</li>")
    return f'<div class="stack" role="img" aria-label="Sentiment distribution">{"".join(segments)}</div><ul class="legend">{"".join(legend)}</ul>'


FEW_IN_BUCKET = 20  # months (or weeks) with fewer reviews draw faded: one review swings their share


def day_label(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}"


def rank_cell(a: dict, rank: int, n_areas: int) -> str:
    """The area's rank with its 90% resampling range drawn on a 1..n track: overlapping ranges aren't clearly ordered."""
    lo, hi = a.get("rank_range") or [rank, rank]
    x = lambda r: 100 * (r - 1) / max(n_areas - 1, 1)  # noqa: E731
    label = f"{lo}–{hi}" if lo != hi else str(lo)
    tip = (f'{a["name"]}: priority {a["priority_score"]:g}, ranked {rank} of {n_areas}. Resampling these reviews 500 times, '
           f"it ranked {label} in 90% of them.")
    return (f'<span class="rr-cell" data-tip="{esc(tip)}"><span class="rr-track"><span class="rr-seg" style="left:{x(lo):.1f}%;width:{x(hi) - x(lo):.1f}%"></span>'
            f'<span class="rr-dot" style="left:{x(rank):.1f}%"></span></span><span class="rr-val">{label}</span></span>')


def trend_note(t: dict | None, split: str) -> tuple[str, str]:
    """(tag shown by the area name, sentence for the area body) about complaints before vs since the split date."""
    if not t:
        return "", ""
    when = day_label(split)
    counts = f"before vs since {when}: {t['earlier']} vs {t['later']}"
    if not t["direction"]:
        clear = f"no clear change (p = {t['p']:.2f})" if t["p"] is not None else "too few to compare"
        return "", f"{counts[0].upper()}{counts[1:]}, {clear}."
    tag, advice = (("Fewer lately", "Check whether a fix or change shipped before acting on it.") if t["direction"] == "fewer"
                   else ("More lately", "Check what changed in recent releases."))
    p = "p < 0.001" if t["p"] < 0.001 else f"p = {t['p']:.3f}"  # with thousands of reviews p can round to 0
    sentence = f"{tag}, {counts} ({p}, unlikely by chance). {advice}"
    return f'<span class="trend {t["direction"]}" data-tip="{esc(sentence)}">{tag}</span>', esc(sentence)


def time_cell(a: dict, timeline: dict | None, scale: float) -> str:
    if not timeline or not a.get("by_time"):
        return '<span class="spark-cell"></span>'
    bars, parts = [], []
    for b, c in zip(timeline["buckets"], a["by_time"]):
        share = c / b["reviews"] if b["reviews"] else 0
        few = b["reviews"] < FEW_IN_BUCKET
        height = min(100, 100 * share / scale) if scale else 0
        bars.append(f'<i class="few" style="height:{height:.0f}%"></i>' if few else f'<i style="height:{height:.0f}%"></i>')
        parts.append(f'{b["label"]} {c} of {b["reviews"]}' + (" (few reviews)" if few else ""))
    tip = f'{a["name"]}, complaints per {timeline["grain"]}: ' + " · ".join(parts)
    return f'<span class="spark-cell"><span class="spark" data-tip="{esc(tip)}">{"".join(bars)}</span></span>'


def area_rows(areas: list[dict], total: int, timeline: dict | None = None) -> str:
    rows = []
    # One height scale for every area's bars, so a tall bar means a big share everywhere.
    scale = max(
        (c / b["reviews"] for a in areas for b, c in zip((timeline or {}).get("buckets", []), a.get("by_time") or [])
         if b["reviews"] >= FEW_IN_BUCKET),
        default=0,
    )
    for rank, a in enumerate(areas, 1):
        if not a["issue_count"]:
            continue
        tag, trend_sentence = trend_note(a.get("trend"), timeline["split_date"]) if timeline else ("", "")
        by_time = ""
        if timeline and a.get("by_time"):
            by_time = f'<p class="muted by-time"><strong>By {timeline["grain"]}:</strong> ' + " · ".join(
                f'{b["label"]} {c} of {b["reviews"]}' for b, c in zip(timeline["buckets"], a["by_time"])
            ) + (f". {trend_sentence}" if trend_sentence else "") + "</p>"
        quotes = quote_table(a["top_quotes"]) if a["top_quotes"] else ""
        praise = f'<h4>What users like here</h4>{quote_table(a["praise_quotes"])}' if a["praise_quotes"] else ""
        mobile_meta = esc(
            f'{plural(a["issue_count"], "issue")} ({pct(a["issue_count"], total)}) · severity {a["mean_severity"]} · '
            f'{a["blocking_count"]} blocking · {a["churn_count"]} churn · {a["praise_count"]} praise'
        )
        rows.append(
            f"""<details class="area{" extra" if len(rows) >= AREA_PREVIEW else ""}" id="area-{esc(a["id"])}">
<summary>
  <span class="rank">{rank}</span>
  <span class="area-name"><span class="an">{esc(a["name"])}{tag}</span><span class="covers">{esc(a["covers"])}</span><span class="mobile-meta">{mobile_meta}</span></span>
  {rank_cell(a, rank, len(areas))}
  {time_cell(a, timeline, scale)}
  <span class="num">{a["issue_count"]}<small>{pct(a["issue_count"], total)}</small></span>
  <span class="num">{a["mean_severity"] if a["mean_severity"] is not None else "–"}</span>
  <span class="num">{a["blocking_count"]}</span>
  <span class="num">{a["churn_count"]}</span>
  <span class="num">{a["after_update_count"]}</span>
  <span class="num">{a["praise_count"]}</span>
</summary>
<div class="area-body">
  <p class="covers-full"><strong>Covers:</strong> {esc(a["covers"])}</p>
  {by_time}
  <p class="muted">{plural(a["bug_count"], "bug report")} · {plural(a["feature_request_count"], "feature request")} · {f'{recommend_share(a["mean_rating"])} recommend' if THUMBS else f'mean rating {a["mean_rating"] if a["mean_rating"] is not None else "–"}'} · {plural(a["borderline_count"], "borderline review")} not counted</p>
  {quotes}
  {praise}
</div>
</details>"""
        )
    toggle = show_rows(len(rows), AREA_PREVIEW, "areas") if len(rows) > AREA_PREVIEW else ""
    return "\n".join(rows) + toggle or '<p class="muted">No product-area issues crossed the threshold.</p>'


# ----------------------------------------------------------------- CSV view and download


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "report"


def csv_payload(filename: str, columns: list[str], rows: list[list], wide: tuple[str, ...] = (), mid: tuple[str, ...] = (),
                num: tuple[str, ...] = (), drop_empty: bool = False) -> dict:
    """Rows the page shows in CSV view and saves on Download CSV.

    `wide`/`mid` name columns that wrap (long text, short lists) and `num` ones to right-align. With drop_empty,
    columns blank in every row go (e.g. Title for a store without review titles).
    """
    if drop_empty and rows:
        keep = [i for i in range(len(columns)) if any(r[i] not in (None, "") for r in rows)]
        columns, rows = [columns[i] for i in keep], [[r[i] for i in keep] for r in rows]
    pick = lambda names: [i for i, c in enumerate(columns) if c in names]  # noqa: E731
    return {"filename": filename, "columns": columns, "rows": rows, "wide": pick(wide), "mid": pick(mid), "num": pick(num)}


def areas_csv(summary: dict) -> dict:
    """One row per product area: every metric in the ranked view, its likely-rank range, trend, and complaints per period."""
    timeline = summary.get("time")
    buckets = timeline["buckets"] if timeline else []
    when = day_label(timeline["split_date"]) if timeline else ""
    columns = ["Rank", "Product area", "Covers", "Priority score", "Likely rank from", "Likely rank to", "Issue reviews",
               "Share of all reviews", "Share of problem reviews", "Mean severity (0-3)", "Blocking", "Churn risk", "Since update",
               "Bug reports", "Feature requests", "Praise reviews", "Borderline (not counted)", "Recommended" if THUMBS else "Mean rating"]
    if timeline:
        columns += ["Trend", f"Complaints before {when}", f"Complaints since {when}", "Trend p-value"]
        columns += [f'{b["label"]} complaints (of {b["reviews"]} reviews)' for b in buckets]
    rows = []
    for rank, a in enumerate(summary["areas"], 1):
        lo, hi = a.get("rank_range") or [rank, rank]
        row = [rank, a["name"], a["covers"], a["priority_score"], lo, hi, a["issue_count"], f'{a["issue_share"]:.1%}',
               f'{a["share_of_problem_reviews"]:.1%}', a["mean_severity"], a["blocking_count"], a["churn_count"], a["after_update_count"],
               a["bug_count"], a["feature_request_count"], a["praise_count"], a["borderline_count"],
               recommend_share(a["mean_rating"]) if THUMBS else a["mean_rating"]]
        if timeline:
            t = a.get("trend") or {}
            row += [{"fewer": "fewer lately", "more": "more lately"}.get(t.get("direction"), "no clear change" if t.get("p") is not None else "too few to test"),
                    t.get("earlier"), t.get("later"), t.get("p")]
            row += list(a.get("by_time") or [None] * len(buckets))
        rows.append(row)
    num = tuple(c for c in columns if c not in ("Product area", "Covers", "Trend"))
    return csv_payload(f'{slug(summary["app"]["name"])}-product-areas-{summary["generated_at"][:10]}.csv', columns, rows, wide=("Covers",), num=num)


def reviews_csv(summary: dict, area_ids: dict) -> dict:
    """One row per analyzed review, newest first, with its full text verbatim and every label."""
    names = {f"issue:{i}": f"problem with {n}" for n, i in area_ids.items()}
    yes = lambda v: "yes" if v else ""  # noqa: E731
    columns = ["Date", "Version", "Recommended" if THUMBS else "Rating", "Title", "Review (verbatim)", "Sentiment", "Severity", "Main area", "All issue areas",
               "Praised areas", "Bug", "Feature request", "Churn risk", "Since update", "Repro detail", "Unsure labels", "Helpful votes", "Review ID"]
    rows = []
    for q in summary.get("all_reviews") or []:
        probs = q.get("borderline_probs") or {}
        unsure = "; ".join((names.get(k) or FLAG_NAMES.get(k) or k) + (f" ({probs[k]:.0%})" if k in probs else "") for k in q.get("borderline") or [])
        rating = q.get("rating")
        if THUMBS and rating is not None:
            rating = "yes" if rating >= 3 else "no"
        rows.append([q.get("date"), str(q.get("version") or "").lstrip("vV"), rating, q.get("title") or "", q.get("full_text") or q["quote"],
                     q.get("sentiment"), q.get("severity"), q.get("area") or "", "; ".join(q.get("issue_areas") or []),
                     "; ".join(q.get("praise_areas") or []), yes(q.get("bug")), yes(q.get("feature_request")), yes(q.get("churn")),
                     yes(q.get("after_update")), yes(q.get("repro_detail")), unsure, q.get("helpful_count"), q.get("id")])
    return csv_payload(f'{slug(summary["app"]["name"])}-reviews-{summary["generated_at"][:10]}.csv', columns, rows,
                       wide=("Review (verbatim)",), mid=("Title", "All issue areas", "Praised areas", "Unsure labels"),
                       num=("Rating", "Helpful votes"), drop_empty=True)


def view_tools(key: str, extra: str = "") -> str:
    """Report/CSV view switch and the download button; shown only with JavaScript, which both need."""
    return (f'<div class="sec-tools"><div class="viewswitch" role="group" aria-label="View" data-csv="{key}">'
            '<button type="button" data-view="report" aria-pressed="true">Report view</button>'
            '<button type="button" data-view="table" aria-pressed="false">CSV view</button></div>'
            f'<button type="button" class="dl" data-csv="{key}">{DOWNLOAD_ICON}Download CSV</button>{extra}</div>')


def table_view(key: str, data: dict, note: str) -> str:
    """The CSV view's container (the grid is built from the JSON the first time it opens) and the JSON itself."""
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")  # no "</script>" can end the block early
    counts = f'{len(data["rows"])} rows · {len(data["columns"])} columns. {note} This is exactly what Download CSV saves.'
    return (f'<div class="view-table" hidden><p class="csv-note">{esc(counts)}</p>'
            f'<div class="csv-wrap" tabindex="0" role="region" aria-label="{esc(key)} as a table"></div></div>'
            f'<script type="application/json" id="csv-{key}">{blob}</script>')


def review_filters(items: list[dict]) -> str:
    """Search box, sentiment chips (any combination), and a star filter for the All reviews tab."""
    counts = Counter(q.get("sentiment") for q in items)
    chips = [f'<button type="button" data-senti="" aria-pressed="true">All<span class="count">{len(items)}</span></button>'] + [
        f'<button type="button" data-senti="{esc(key)}" aria-pressed="false"><span class="swatch s{i}"></span>{esc(key)}<span class="count">{counts[key]}</span></button>'
        for i, key in enumerate(SENTIMENT_KEYS)
        if counts[key]
    ]
    ratings = sorted({q["rating"] for q in items if q.get("rating") is not None})
    options = f'<option value="">{"Recommended or not" if THUMBS else "Any rating"}</option>' + "".join(
        f'<option value="{esc(r)}">{esc(rating_label(r))}</option>' for r in (reversed(ratings) if THUMBS else ratings))
    stars = f'<select class="f-stars" aria-label="{"Recommendation" if THUMBS else "Star rating"}">{options}</select>' if len(ratings) > 1 else ""
    return f"""<div class="filters">
<input type="search" class="f-search" placeholder="Search the reviews, e.g. login" aria-label="Search the review text">{stars}
<div class="f-senti" role="group" aria-label="Sentiment">{"".join(chips)}</div>
<div class="f-status-row"><span class="f-status" aria-live="polite"></span><button type="button" class="f-clear" hidden>Clear filters</button></div>
</div>"""


def review_tabs(tabs: list[tuple[str, str, str, int, list[dict], bool, bool]], area_ids: dict, csv: dict | None = None) -> str:
    """One section of reviews, one tab per kind: (key, label, intro, total, items, show_area, searchable).

    Without JavaScript every panel shows, each under its own heading.
    """
    tabs = [t for t in tabs if t[4]]
    if not tabs:
        return ""
    buttons, panels = [], []
    for i, (key, label, intro, total, items, show_area, searchable) in enumerate(tabs):
        selected = i == 0
        focus = "" if selected else ' tabindex="-1"'
        buttons.append(
            f'<button type="button" role="tab" id="tab-{key}" aria-controls="panel-{key}" aria-selected="{str(selected).lower()}"{focus}>'
            f'{esc(label)}<span class="count">{total}</span></button>'
        )
        showing = f" Showing the {len(items)} with the most impact." if len(items) < total else ""
        panels.append(
            f'<div role="tabpanel" id="panel-{key}" aria-labelledby="tab-{key}" tabindex="0">'
            f'<h3 class="panel-title">{esc(label)} ({total})</h3><p class="muted">{esc(intro)}{showing}</p>'
            + (review_filters(items) if searchable else "")
            + quote_table(items, show_area, area_ids)
            + ('<button type="button" class="f-more" hidden>Show more</button>' if searchable else "")
            + "</div>"
        )
    tools = view_tools("reviews") if csv and csv["rows"] else ""
    table = table_view("reviews", csv, "Every analyzed review, newest first, with its full text.") if tools else ""
    return f"""<section id="reviews" data-view="report"><div class="sec-head"><h2>Reviews</h2>{tools}</div>
<div class="view-report"><p class="muted">Quotes are the reviewer's exact words. <span class="ellip">…</span> marks a quote cut from a longer review; open Full review to read the rest.</p>
<div class="tabs" role="tablist" aria-label="Kind of review">{"".join(buttons)}</div>
{"".join(panels)}</div>{table}</section>"""


def version_table(versions: list[dict]) -> str:
    if len(versions) < 2:
        return ""
    versions = sorted(versions, key=lambda v: version_key(v["version"]), reverse=True)

    def count(v: dict, key: str) -> str:
        n = v.get(f"{key}_count", round(v[f"{key}_share"] * v["reviews"]))  # summaries before the counts existed
        return f'{n}<small>{v[f"{key}_share"]:.0%}</small>'

    rows = []
    for i, v in enumerate(versions):
        cls = " ".join(c for c in ("extra" if i >= VERSION_PREVIEW else "", "thin" if v["reviews"] < THIN_VERSION else "") if c)
        rows.append(
            (f'<tr class="{cls}">' if cls else "<tr>")
            + f'<td>{esc(str(v["version"]).lstrip("vV"))}</td><td class="r">{v["reviews"]}</td>'
            f'<td class="r">{v["mean_rating"] if v["mean_rating"] is not None else "–"}</td>'
            f'<td class="r">{count(v, "negative")}</td><td class="r">{count(v, "bug")}</td><td class="r">{v["after_update_count"]}</td></tr>'
        )
    toggle = show_rows(len(versions), VERSION_PREVIEW, "versions", f"Show newest {VERSION_PREVIEW} only") if len(versions) > VERSION_PREVIEW else ""
    thin = sum(v["reviews"] < THIN_VERSION for v in versions)
    thin_note = f" Gray rows have fewer than {THIN_VERSION} reviews, so a single review moves their percentages by 10 points or more." if thin else ""
    return f"""<section id="versions" class="collapsible"><h2>By app version</h2>
<p class="muted">The most-reviewed versions, newest first. Compare a version with the ones just before and after it.{thin_note}</p>
<div class="table-wrap"><table class="vtable"><thead><tr><th>Version</th><th class="r">Reviews</th><th class="r">Mean ★</th><th class="r">Negative</th><th class="r">Bug reports</th><th class="r">"Since update"</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>{toggle}</section>"""


# ----------------------------------------------------------------- page

CSS = """
:root{--nav-h:50px;color-scheme:light;--sans:"Avenir Next",Avenir,"Nunito Sans","Segoe UI",system-ui,sans-serif;--ease:cubic-bezier(.22,.61,.36,1);
--page:#F3EBE0;--card:#FFFFFF;--warm:#FFFAF5;--sunken:#F6F1EA;--hover:#FBF7F1;--ink:#14213A;--ink-2:#4B5670;--muted:#69717C;
--line:#E6E3DD;--line-strong:#CDD5E1;--divider:#EEEEEE;--orange:#F08B49;--rust:#C2521A;--rust-ink:#A6431A;--tint:#FBE7DB;
--shadow:0 1px 2px rgba(20,33,58,.04),0 6px 20px rgba(20,33,58,.06);
--s0:#B9561F;--s1:#F28A48;--s2:#DDD6CC;--s3:#8FB3E8;--s4:#2E5DEF;--sev-minor:#F0C27B}
*{box-sizing:border-box}
html{scroll-padding-top:calc(var(--nav-h) + 12px)}
body{margin:0;background:linear-gradient(180deg,#F3EBE0 0%,#F2EDE5 100%) fixed;color:var(--ink-2);font:400 15px/1.55 var(--sans);letter-spacing:.005em;-webkit-font-smoothing:antialiased}
main{max-width:1120px;margin:0 auto;padding:24px 16px 48px}
h1,h2,h3{color:var(--ink);letter-spacing:-.01em}
h2{font-size:24px;line-height:1.2;font-weight:600;color:var(--rust);margin:0 0 8px}
h3{font-size:19px;line-height:1.25;font-weight:700;margin:18px 0 6px}
h4{font-size:11px;line-height:1.3;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin:20px 0 8px}
strong{font-weight:600;color:var(--ink)}
.muted{color:var(--ink-2)}
.sr-only{position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
a{color:var(--rust);font-weight:500;text-decoration:underline;text-decoration-color:rgba(194,82,26,.35);text-underline-offset:3px;transition:color .12s var(--ease),text-decoration-color .12s var(--ease)}
a:hover{color:var(--rust-ink);text-decoration-color:var(--orange)}
:focus-visible{outline:2px solid var(--orange);outline-offset:2px}
button{font-family:inherit;transition:background-color .12s var(--ease),color .12s var(--ease),border-color .12s var(--ease)}
button:active{transform:translateY(1px)}
.masthead{position:relative;overflow:hidden;color:#fff;background:linear-gradient(105deg,#2C2C37 0%,#14213A 42%,#183355 100%);border-radius:24px;padding:30px 40px 34px;box-shadow:0 10px 30px rgba(20,33,58,.18)}
.mast-art{position:absolute;right:0;bottom:0;width:62%;height:100%;pointer-events:none}
.masthead>:not(.mast-art){position:relative}
.mast-top{display:flex;align-items:baseline;justify-content:space-between;gap:8px 16px;flex-wrap:wrap;margin-bottom:22px}
.kicker,.stamp{font-size:13px;line-height:1.3;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:#B7C1D1}
.title-row{display:flex;align-items:flex-start;gap:16px}
.masthead h1{flex:1;margin:0;font-size:48px;line-height:1.08;font-weight:800;letter-spacing:-.035em;color:#fff;max-width:28ch;text-wrap:balance}
.masthead h1 em{display:block;width:fit-content;font-style:normal;color:var(--orange);text-decoration:underline;text-decoration-thickness:3px;text-underline-offset:10px;padding-bottom:6px}
.sub{font-size:18px;line-height:1.5;color:#D5DCE6;margin:18px 0 0;max-width:62ch}
.source-link{margin:14px 0 0;font-size:14px;font-weight:600;color:#B7C1D1}
.masthead a{color:#fff;font-weight:600;text-decoration-color:rgba(255,255,255,.4)}
.masthead a:hover{color:var(--orange);text-decoration-color:var(--orange)}
.info{flex:none;display:inline-flex;align-items:center;justify-content:center;width:38px;height:38px;margin-top:6px;padding:0;border:1px solid rgba(255,255,255,.25);border-radius:10px;background:rgba(255,255,255,.08);color:#fff;cursor:pointer}
.info:hover{background:rgba(255,255,255,.16)}
.info[aria-expanded="true"]{background:#fff;color:var(--ink)}
section{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px 28px;margin-top:20px;box-shadow:var(--shadow)}
.narrative{background:var(--warm);border-color:rgba(240,139,73,.55);padding:30px 36px}
.narrative>:first-child{margin-top:0}.narrative>:last-child{margin-bottom:0}
.narrative h2{font-size:26px;margin-bottom:12px;max-width:34ch;text-wrap:balance}
.narrative p,.narrative li{font-size:16.5px;line-height:1.6;max-width:68ch}
.narrative li{margin-bottom:6px}
.narrative blockquote{margin:6px 0 14px;padding:2px 0 2px 16px;border-left:3px solid var(--orange);font-style:italic;color:var(--ink-2)}
.narrative blockquote p{margin:0 0 4px}.narrative blockquote p:last-child{margin-bottom:0}
.steps{list-style:none;counter-reset:step;padding:0;margin:14px 0 0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 40px}
.steps li{counter-increment:step;font-size:14.5px;color:var(--ink-2);padding:16px 0}
.steps li:nth-child(n+3){border-top:1px solid var(--divider)}
.steps li::before{content:counter(step) ". ";font-size:17px;font-weight:700;color:var(--ink)}
.steps strong{font-size:17px;font-weight:700}
.steps strong::after{content:"\\A";white-space:pre}
.about-more{margin:10px 0 0;font-size:13px}
.about:focus{outline:none}
.about-head{display:flex;align-items:center;justify-content:space-between;gap:12px}
.about-head h2{margin:0}
.close{border:0;background:none;color:var(--ink-2);font-size:24px;line-height:1;padding:2px 9px;border-radius:8px;cursor:pointer}
.close:hover{background:var(--sunken);color:var(--ink)}
.nsec{border-top:1px solid var(--divider);margin-top:18px}
.nsec>summary{display:flex;align-items:center;gap:12px;cursor:pointer;list-style:none;padding-top:14px}
.nsec>summary::-webkit-details-marker{display:none}
.nsec>summary h3{margin:0;transition:color .12s var(--ease)}
.nsec>summary:hover h3{color:var(--rust)}
.area{scroll-margin-top:40px}
.area.hit>summary{box-shadow:inset 3px 0 0 var(--orange)}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-top:20px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px 18px;box-shadow:var(--shadow)}
.kpi-label{font-size:11px;line-height:1.3;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.kpi-value{font-size:34px;line-height:1;font-weight:700;letter-spacing:-.02em;color:var(--ink);font-variant-numeric:tabular-nums;margin:12px 0 6px}
.kpi-value .star{font-size:.6em;margin-left:3px;vertical-align:.3em;color:var(--sev-minor)}
.kpi-note{font-size:13px;line-height:1.4;color:var(--ink-2)}
.stack{display:flex;gap:2px;height:22px;margin:14px 0 12px}
.seg{border-radius:4px;min-width:3px}
.s0{background:var(--s0)}.s1{background:var(--s1)}.s2{background:var(--s2)}.s3{background:var(--s3)}.s4{background:var(--s4)}
.legend{list-style:none;padding:0;margin:0;display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px}
.legend strong{font-weight:600}
.swatch{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;vertical-align:-1px}
.area-head,.area>summary{display:grid;grid-template-columns:40px minmax(0,2.3fr) minmax(0,1.3fr) 76px repeat(6,minmax(44px,.55fr));gap:10px;align-items:center}
.area-head{position:sticky;top:var(--nav-h);z-index:2;background:var(--card);font-size:10.5px;line-height:1.3;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);padding:10px 8px;border-bottom:1px solid var(--line)}
.area-head span:nth-child(n+5){text-align:right}
.area{border-bottom:1px solid var(--divider)}
.area>summary{cursor:pointer;list-style:none;padding:12px 8px;border-radius:10px;transition:background-color .12s var(--ease)}
.area>summary::-webkit-details-marker{display:none}
.area>summary:hover,.area[open]>summary{background:var(--hover)}
.rank{display:flex;align-items:center;gap:9px;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}
.rank::before,.nsec>summary::before{content:"";flex:none;width:6px;height:6px;border-right:1.5px solid var(--muted);border-bottom:1.5px solid var(--muted);transform:rotate(-45deg);transition:transform .22s var(--ease)}
.area[open] .rank::before,.nsec[open]>summary::before{transform:rotate(45deg)}
.area-name{font-weight:600;color:var(--ink);display:flex;flex-direction:column;min-width:0}
.mobile-meta{display:none;font-weight:400;font-size:12px;color:var(--ink-2)}
.covers{font-weight:400;font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.an{display:flex;align-items:center;flex-wrap:wrap;gap:4px 8px}
.trend{cursor:help;font-size:10px;line-height:1;font-weight:600;letter-spacing:.08em;text-transform:uppercase;padding:4px 7px;border-radius:999px;white-space:nowrap}
.trend.fewer{color:#1F4FD1;background:#E8EEFD}
.trend.more{color:var(--rust-ink);background:var(--tint)}
.rr-cell{display:flex;align-items:center;gap:10px;min-width:0;cursor:help}
.rr-track{position:relative;flex:1;min-width:0;height:12px;background:linear-gradient(var(--line-strong),var(--line-strong)) center/100% 1px no-repeat}
.rr-seg{position:absolute;top:3px;height:6px;min-width:6px;border-radius:3px;background:rgba(240,139,73,.45)}
.rr-dot{position:absolute;top:2px;width:8px;height:8px;margin-left:-4px;border-radius:50%;background:var(--rust);box-shadow:0 0 0 2px var(--card)}
.rr-val{flex:none;min-width:34px;font-size:12px;color:var(--ink-2);font-variant-numeric:tabular-nums}
.spark-cell{display:flex;align-items:center}
.spark{display:inline-flex;align-items:flex-end;gap:2px;height:22px;padding-bottom:1px;border-bottom:1px solid var(--line-strong);cursor:help}
.spark i{display:block;width:8px;min-height:1px;background:var(--orange);border-radius:2px 2px 0 0}
.spark i.few{opacity:.4}
.by-time{font-size:13px;margin:6px 0}
.overall-trend{font-size:13px;margin:-2px 0 6px}
.ellip{color:var(--rust);font-weight:600;cursor:help;padding:0 1px}
.vtable small{display:inline;margin-left:6px;font-size:11px;color:var(--muted)}
.vtable tr.thin td{color:var(--muted)}
.num{text-align:right;color:var(--ink);font-variant-numeric:tabular-nums}
.num small{display:block;font-size:11px;color:var(--muted)}
.area-body{padding:6px 8px 20px 58px}
.covers-full{font-size:14px;color:var(--ink-2);margin:6px 0;max-width:90ch}
.qtable{width:100%;border-collapse:collapse;table-layout:fixed;margin-top:12px;font-size:13px}
.qtable th{position:sticky;top:var(--nav-h);z-index:1;background:var(--card);text-align:left;font-size:10px;line-height:1.3;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);padding:9px 10px;border-bottom:1px solid var(--line-strong)}
.area-body .qtable th{position:static;background:none}
.qtable .w-stars{width:58px}.qtable .w-date{width:92px}.qtable .w-ver{width:64px}.qtable .w-area{width:136px}.qtable .w-senti{width:116px}.qtable .w-labels{width:148px}
.qtable td{padding:8px 10px;border-bottom:1px solid var(--divider);vertical-align:top;color:var(--ink-2);overflow-wrap:anywhere}
.qtable td+td,.qtable th+th{border-left:1px solid var(--divider)}
.qtable tbody tr:hover{background:var(--hover)}
.qtable .c-quote{color:var(--ink);font-size:14.5px;line-height:1.5}
.qtable .c-stars,.qtable .c-date,.qtable .c-ver{font-variant-numeric:tabular-nums;white-space:nowrap}
.qtable .c-area a{font-weight:500}
.qtable .badge{display:inline;padding:0;border:0;border-radius:0;background:none;font-size:12.5px;line-height:1.5;font-weight:500;letter-spacing:0;text-transform:none;color:var(--ink-2)}
.qtable .badge:not(:last-of-type)::after{content:", ";font-weight:400}
.qtable .badge.sev-blocking::before,.qtable .badge.sev-degraded::before,.qtable .badge.sev-minor::before{display:inline-block;margin-right:5px;vertical-align:1px}
.qtable .badge.churn{background:none;color:var(--rust-ink);font-weight:600}
.q-title{font-weight:600;font-size:14px;color:var(--ink)}
.q-text{margin:0}
.badge{cursor:help;display:inline-flex;align-items:center;gap:5px;font-size:10px;line-height:1;font-weight:600;letter-spacing:.1em;text-transform:uppercase;padding:4px 8px;border-radius:999px;color:var(--ink-2);background:var(--card);border:1px solid var(--line-strong)}
.badge.sev-blocking::before,.badge.sev-degraded::before,.badge.sev-minor::before{content:"";width:7px;height:7px;border-radius:50%}
.badge.sev-blocking::before{background:var(--s0)}
.badge.sev-degraded::before{background:var(--s1)}
.badge.sev-minor::before{background:var(--sev-minor)}
.badge.churn{background:var(--tint);color:var(--rust-ink);border-color:transparent}
details.full summary{font-size:12px;font-weight:500;color:var(--rust);cursor:pointer;margin-top:6px}
details.full p{font-size:14px;color:var(--ink-2)}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:8px;border-bottom:1px solid var(--divider);text-align:left}
td{color:var(--ink)}
th{font-size:10.5px;line-height:1.3;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--muted);border-bottom-color:var(--line)}
.r{text-align:right;font-variant-numeric:tabular-nums}
code{font-size:13px;background:var(--sunken);padding:0 4px;border-radius:4px}
.toc{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:2px;height:var(--nav-h);margin:14px -16px 0;padding:0 16px;background:var(--page);border-bottom:1px solid var(--line);overflow-x:auto;scrollbar-width:none;white-space:nowrap}
.toc::-webkit-scrollbar{display:none}
.toc a{flex:none;padding:8px 12px;color:var(--ink-2);font-size:14px;font-weight:500;text-decoration:none;border-radius:8px}
.toc a:hover{color:var(--ink);background:rgba(255,255,255,.6)}
.toc a[aria-current]{color:var(--rust);font-weight:600;box-shadow:inset 0 -2px 0 var(--orange);border-radius:0}
.sec-head{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px 12px;margin-bottom:8px}
.sec-tools{display:flex;align-items:center;flex-wrap:wrap;gap:8px}
.viewswitch,.dl{display:none}
.js .viewswitch{display:inline-flex;border:1px solid var(--line-strong);border-radius:8px;overflow:hidden;background:var(--card)}
.viewswitch button{font-size:13px;line-height:1;font-weight:500;color:var(--ink-2);background:none;border:0;padding:9px 12px;cursor:pointer}
.viewswitch button+button{border-left:1px solid var(--line-strong)}
.viewswitch button:hover{background:var(--hover)}
.viewswitch button[aria-pressed=true]{background:var(--warm);color:var(--rust-ink);font-weight:600;box-shadow:inset 0 -2px 0 var(--orange)}
.js .dl{display:inline-flex;align-items:center;gap:7px;font-size:13px;line-height:1;font-weight:600;color:#fff;background:var(--rust);border:1px solid var(--rust);border-radius:8px;padding:9px 14px;cursor:pointer}
.js .dl:hover{background:var(--rust-ink);border-color:var(--rust-ink)}
section[data-view=table] .expand-all{display:none}
.csv-note{font-size:13px;color:var(--ink-2);margin:2px 0 10px}
.csv-wrap{max-height:70vh;overflow:auto;border:1px solid var(--line-strong);border-radius:8px;background:var(--card)}
.csvt{border-collapse:separate;border-spacing:0;font-size:12.5px;line-height:1.45;color:var(--ink)}
.csvt th,.csvt td{padding:6px 10px;border-right:1px solid var(--divider);border-bottom:1px solid var(--divider);white-space:nowrap;vertical-align:top;text-align:left}
.csvt thead th{position:sticky;top:0;z-index:2;background:var(--sunken);font-size:12px;font-weight:600;letter-spacing:0;text-transform:none;color:var(--ink);border-bottom-color:var(--line-strong)}
.csvt .rn{position:sticky;left:0;z-index:1;min-width:42px;background:var(--sunken);color:var(--muted);text-align:right;font-variant-numeric:tabular-nums;border-right-color:var(--line-strong)}
.csvt thead .rn{z-index:3}
.csvt td.wide{white-space:normal;min-width:340px;max-width:560px}
.csvt th.wide{min-width:340px}
.csvt td.mid{white-space:normal;min-width:180px;max-width:280px}
.csvt .n{text-align:right;font-variant-numeric:tabular-nums}
.csvt tbody tr:hover td:not(.rn){background:var(--hover)}
.sec-head h2{margin:0}
.expand-all,.js .f-more:not([hidden]),.js .show-rows{flex:none;font-size:13px;line-height:1;font-weight:600;color:var(--rust);background:var(--card);border:1px solid var(--line-strong);border-radius:8px;padding:9px 14px;cursor:pointer}
.expand-all:hover,.js .f-more:not([hidden]):hover,.js .show-rows:hover{border-color:var(--orange);background:var(--warm)}
.tabs{display:none;gap:6px;overflow-x:auto;scrollbar-width:none;margin:14px 0 4px;padding:2px}
.tabs::-webkit-scrollbar{display:none}
.js .tabs{display:flex}
.tabs [role=tab]{flex:none;display:inline-flex;align-items:center;gap:6px;font-size:13px;line-height:1;font-weight:500;color:var(--ink-2);background:var(--card);border:1px solid var(--line-strong);border-radius:8px;padding:9px 12px;cursor:pointer}
.tabs [role=tab]:hover{background:var(--hover)}
.tabs [role=tab][aria-selected=true]{background:var(--warm);color:var(--rust-ink);border-color:var(--orange);font-weight:600;box-shadow:inset 0 0 0 .5px var(--orange)}
.tabs .count,.f-senti .count{font-size:12px;font-variant-numeric:tabular-nums;opacity:.75}
.panel-title{font-size:19px;margin:22px 0 4px}
.js .panel-title{display:none}
[role=tabpanel]:focus:not(:focus-visible){outline:none}
.legend-btn{display:inline-block;font:inherit;font-size:13px;color:inherit;background:none;border:0;padding:2px 6px;margin:-2px -6px;border-radius:6px;cursor:pointer}
.legend-btn:hover{background:var(--sunken)}
.seg[data-senti]{cursor:pointer}
.seg[data-senti]:hover{opacity:.85}
.senti{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--ink-2);cursor:help}
.senti .swatch,.f-senti .swatch{margin-right:0}
.filters,.f-more,.show-rows{display:none}
.js .collapsible:not(.expanded) .extra{display:none}
.js .filters{display:flex;flex-wrap:wrap;align-items:center;gap:8px 10px;margin:12px 0 2px}
.f-search{flex:1 1 260px;min-width:0;font:inherit;font-size:14px;padding:9px 12px;border:1px solid var(--line-strong);border-radius:8px;background:var(--card);color:var(--ink)}
.f-search::placeholder{color:var(--muted)}
.f-search:focus,.f-stars:focus{outline:2px solid var(--orange);outline-offset:-1px}
.f-stars{font:inherit;font-size:13px;padding:9px 8px;border:1px solid var(--line-strong);border-radius:8px;background:var(--card);color:var(--ink)}
.f-senti{flex-basis:100%;display:flex;flex-wrap:wrap;gap:6px}
.f-senti button{display:inline-flex;align-items:center;gap:6px;font-size:12px;line-height:1;color:var(--ink-2);background:var(--card);border:1px solid var(--line-strong);border-radius:999px;padding:6px 10px;cursor:pointer}
.f-senti button:hover{background:var(--hover)}
.f-senti button[aria-pressed=true]{color:var(--ink);background:var(--warm);border-color:var(--orange);font-weight:600}
.f-status-row{flex-basis:100%;display:flex;align-items:center;gap:12px;font-size:13px;color:var(--ink-2)}
.f-clear{font:inherit;font-size:13px;font-weight:500;color:var(--rust);background:none;border:0;padding:0;text-decoration:underline;text-underline-offset:3px;cursor:pointer}
.js .f-more:not([hidden]),.js .show-rows{display:block;margin:16px auto 0}
.unsure{font-size:12px;color:var(--ink-2);background:var(--sunken);border-radius:8px;padding:5px 8px;margin-top:4px}
.about h3{font-size:19px;margin:22px 0 10px;padding-top:18px;border-top:1px solid var(--divider)}
.glossary{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:8px 18px;margin:0;font-size:14px}
.glossary dt{font-weight:600;color:var(--ink)}
.glossary dd{margin:0;color:var(--ink-2)}
.totop{position:fixed;right:16px;bottom:16px;z-index:6;display:flex;align-items:center;justify-content:center;width:40px;height:40px;border-radius:10px;background:var(--card);border:1px solid var(--line);color:var(--ink);text-decoration:none;box-shadow:var(--shadow);opacity:0;pointer-events:none;transition:opacity .22s var(--ease)}
.totop:hover{color:var(--rust);border-color:var(--orange)}
.totop.show{opacity:1;pointer-events:auto}
#tip{position:fixed;pointer-events:none;background:var(--ink);color:#fff;font-size:12px;padding:5px 9px;border-radius:8px;opacity:0;transition:opacity .1s;z-index:9;max-width:320px}
footer{margin-top:28px}
.credit{font-size:14px;line-height:1.5;color:var(--ink-2);margin:0 2px}
.gen{font-size:12px;line-height:1.5;color:#6F675F;margin:14px 2px 0;overflow-wrap:anywhere}
.gen a{font-weight:400;color:inherit}
@media (max-width:760px){
 .masthead{padding:22px 20px 26px;border-radius:18px}
 .mast-art{width:100%;opacity:.35}
 .masthead h1{font-size:30px}
 .masthead h1 em{font-size:.8em;margin-top:2px;text-decoration-thickness:2px;text-underline-offset:6px;padding-bottom:4px}
 .sub{font-size:16px}
 .narrative{padding:22px 20px}
 .narrative h2{font-size:22px}
 .narrative p,.narrative li{font-size:16px}
 section{padding:20px}
 .area-head{display:none}
 .kpis{grid-template-columns:repeat(2,minmax(0,1fr))}
 .steps{grid-template-columns:minmax(0,1fr)}
 .steps li:nth-child(n+2){border-top:1px solid var(--divider)}
 .kpi-value{font-size:28px}
 .area>summary{grid-template-columns:36px minmax(0,1fr);row-gap:4px}
 .glossary{grid-template-columns:minmax(0,1fr);gap:2px}
 .glossary dd{margin-bottom:8px}
 .mobile-meta{display:block;margin-top:2px}
 .area>summary .rr-cell{grid-column:2}
 .area>summary .spark-cell{display:none}
 .area>summary .num{display:none}
 .area-body{padding-left:8px}
 .qtable,.qtable tbody{display:block}
 .qtable thead{display:none}
 .qtable tr.quote{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;padding:12px 0;border-bottom:1px solid var(--divider)}
 .qtable tr.quote[hidden]{display:none}
 .qtable td,.qtable td+td{display:block;padding:0;border:0;font-size:12px}
 .qtable td:empty{display:none}
 .qtable .c-quote{flex-basis:100%;font-size:15px;margin-bottom:2px}
 .qtable .c-labels{flex-basis:100%}
}
"""

JS = r"""
document.documentElement.classList.add('js');
const tip=document.getElementById('tip');
document.addEventListener('pointermove',e=>{const t=e.target.closest('[data-tip],[data-label]');
if(!t){tip.style.opacity=0;return}tip.textContent=t.dataset.tip||LABELS[t.dataset.label];tip.style.opacity=1;
const x=Math.min(e.clientX+12,innerWidth-tip.offsetWidth-8);tip.style.left=x+'px';tip.style.top=(e.clientY+14)+'px'});
const about=document.getElementById('about'),info=document.querySelector('.info');
function setAbout(open){about.hidden=!open;info.setAttribute('aria-expanded',open);(open?about:info).focus()}
info.addEventListener('click',()=>setAbout(about.hidden));
about.querySelector('.close').addEventListener('click',()=>setAbout(false));
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!about.hidden)setAbout(false)});
// Tabs for the example reviews; arrow keys move between them.
const tabs=[...document.querySelectorAll('[role=tab]')];
function selectTab(tab,focus){tabs.forEach(t=>{const on=t===tab;t.setAttribute('aria-selected',on);t.tabIndex=on?0:-1;
document.getElementById(t.getAttribute('aria-controls')).hidden=!on});if(focus)tab.focus()}
tabs.forEach((t,i)=>{t.addEventListener('click',()=>selectTab(t));t.addEventListener('keydown',e=>{const d={ArrowRight:1,ArrowLeft:-1}[e.key];
if(d){e.preventDefault();selectTab(tabs[(i+d+tabs.length)%tabs.length],true)}})});
if(tabs.length)selectTab(tabs[0]);
// All reviews: search the text, filter by any mix of sentiments and by stars, and show 30 at a time.
const allPanel=document.getElementById('panel-all');
if(allPanel){
const cards=[...allPanel.querySelectorAll('tr.quote')],search=allPanel.querySelector('.f-search'),stars=allPanel.querySelector('.f-stars'),
chips=[...allPanel.querySelectorAll('.f-senti button')],status=allPanel.querySelector('.f-status'),clear=allPanel.querySelector('.f-clear'),
more=allPanel.querySelector('.f-more'),picked=new Set(),FIRST=10,STEP=40;let limit=FIRST;
cards.forEach(c=>c.searchText=[...c.querySelectorAll('.q-title,.q-text,details.full p')].map(e=>e.textContent).join(' ').toLowerCase());
function apply(reset){if(reset)limit=FIRST;const terms=search.value.toLowerCase().split(/\s+/).filter(Boolean),star=stars?stars.value:'';let n=0;
cards.forEach(c=>{const ok=(!picked.size||picked.has(c.dataset.sentiment))&&(!star||c.dataset.rating===star)&&terms.every(t=>c.searchText.includes(t));
if(ok)n++;c.hidden=!ok||n>limit});
chips.forEach(b=>b.setAttribute('aria-pressed',b.dataset.senti?picked.has(b.dataset.senti):!picked.size));
const filtered=picked.size||star||terms.length;
status.textContent=n?`${n} review${n===1?'':'s'}${filtered?(n===1?' matches':' match'):''}${n>limit?`, showing ${limit}`:''}`:'No reviews match.';
clear.hidden=!filtered;more.hidden=n<=limit;more.textContent=`Show ${Math.min(STEP,n-limit)} more`}
search.addEventListener('input',()=>apply(true));if(stars)stars.addEventListener('change',()=>apply(true));
chips.forEach(b=>b.addEventListener('click',()=>{const s=b.dataset.senti;if(!s)picked.clear();else if(!picked.delete(s))picked.add(s);apply(true)}));
clear.addEventListener('click',()=>{picked.clear();search.value='';if(stars)stars.value='';apply(true);search.focus()});
more.addEventListener('click',()=>{limit+=STEP;apply(false)});
// The sentiment bar and its legend open the All reviews tab filtered to that sentiment.
document.addEventListener('click',e=>{const t=e.target.closest('[data-senti]');if(!t||allPanel.contains(t))return;
setView(document.getElementById('reviews'),'report');picked.clear();picked.add(t.dataset.senti);search.value='';if(stars)stars.value='';apply(true);selectTab(document.getElementById('tab-all'));
requestAnimationFrame(()=>document.getElementById('reviews').scrollIntoView({behavior:'smooth',block:'start'}))});
apply(true)}
// Long tables show their top rows; the toggle under each shows or hides the rest.
function setRows(sec,open){const b=sec.querySelector('.show-rows');if(!b)return;sec.classList.toggle('expanded',open);
b.setAttribute('aria-expanded',open);b.textContent=open?b.dataset.less:b.dataset.more}
document.querySelectorAll('.show-rows').forEach(b=>b.addEventListener('click',()=>{const sec=b.closest('.collapsible'),open=!sec.classList.contains('expanded');
setRows(sec,open);if(!open)requestAnimationFrame(()=>b.scrollIntoView({block:'nearest'}))}));
function reveal(t){const sec=t.closest('.collapsible');if(sec&&t.closest('.extra'))setRows(sec,true);
if(t.closest('.view-report'))setView(t.closest('section'),'report')}
// CSV view and download. Each section's rows are embedded as JSON; the grid is built the first time it's shown.
const csvData=k=>JSON.parse(document.getElementById('csv-'+k).textContent);
function csvText(d){const cell=v=>{let s=v==null?'':String(v);
// A cell starting like a formula would run in a spreadsheet; review text is written by strangers.
if(/^[\t\r]|^\s*[=+\-@\uFF1D\uFF0B\uFF0D\uFF20]/.test(s))s="'"+s;return /[",\r\n]/.test(s)?'"'+s.replace(/"/g,'""')+'"':s};
return '\ufeff'+[d.columns,...d.rows].map(r=>r.map(cell).join(',')).join('\r\n')+'\r\n'}
document.querySelectorAll('.dl').forEach(b=>b.addEventListener('click',()=>{const d=csvData(b.dataset.csv),
url=URL.createObjectURL(new Blob([csvText(d)],{type:'text/csv;charset=utf-8'})),a=document.createElement('a');
a.href=url;a.download=d.filename;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),2000)}));
function buildGrid(wrap,d){const t=document.createElement('table'),head=t.createTHead().insertRow(),body=t.createTBody(),
cls=i=>d.wide.includes(i)?'wide':(d.mid||[]).includes(i)?'mid':d.num.includes(i)?'n':'';const corner=document.createElement('th');corner.className='rn';head.appendChild(corner);
d.columns.forEach((c,i)=>{const th=document.createElement('th');th.textContent=c;th.className=cls(i);head.appendChild(th)});
d.rows.forEach((r,n)=>{const tr=body.insertRow(),rn=tr.insertCell();rn.className='rn';rn.textContent=n+1;
r.forEach((v,i)=>{const td=tr.insertCell();td.textContent=v==null?'':v;td.className=cls(i)})});t.className='csvt';wrap.appendChild(t)}
function setView(sec,view){const g=sec&&sec.querySelector('.viewswitch');if(!g||sec.dataset.view===view)return;const table=view==='table';
sec.dataset.view=view;g.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.view===view));
sec.querySelector('.view-report').hidden=table;const v=sec.querySelector('.view-table'),wrap=v.querySelector('.csv-wrap');v.hidden=!table;
if(table&&!wrap.firstChild)buildGrid(wrap,csvData(g.dataset.csv))}
document.querySelectorAll('.viewswitch button').forEach(b=>b.addEventListener('click',()=>setView(b.closest('section'),b.dataset.view)));
// In-page links: open what they point at (an area row, the glossary), then scroll to it.
function go(id){const t=id&&document.getElementById(id);if(!t)return false;
if(about.contains(t)&&about.hidden){about.hidden=false;info.setAttribute('aria-expanded',true)}
reveal(t);
if(t.tagName==='DETAILS'){t.open=true;t.classList.add('hit');setTimeout(()=>t.classList.remove('hit'),1600)}
// Scroll after the browser lays out what just opened, or it aims at the old position.
requestAnimationFrame(()=>t.scrollIntoView({behavior:'smooth',block:'start'}));return true}
document.addEventListener('click',e=>{const a=e.target.closest('a[href^="#"]');if(a&&go(a.getAttribute('href').slice(1)))e.preventDefault()});
let hashed=null;try{hashed=document.getElementById(decodeURIComponent(location.hash.slice(1)))}catch(e){}if(hashed&&hashed.tagName==='DETAILS'){reveal(hashed);hashed.open=true;requestAnimationFrame(()=>hashed.scrollIntoView())}
const expand=document.querySelector('.expand-all'),rows=[...document.querySelectorAll('details.area')];
if(expand)expand.addEventListener('click',()=>{const open=rows.some(r=>!r.open);if(open)setRows(expand.closest('.collapsible'),true);rows.forEach(r=>r.open=open);expand.textContent=open?'Collapse all':'Expand all'});
// Highlight the section in view in the nav, and show the back-to-top button after the first screen.
const nav=document.querySelector('.toc'),links=[...nav.querySelectorAll('a')],targets=links.map(a=>document.getElementById(a.getAttribute('href').slice(1))),
totop=document.querySelector('.totop');let current=-2;
function spy(){let cur=-1;const y=nav.offsetHeight+40;targets.forEach((t,i)=>{if(t&&t.getBoundingClientRect().top<=y)cur=i});
if(innerHeight+scrollY>=document.documentElement.scrollHeight-4)cur=targets.length-1;
if(cur!==current){current=cur;links.forEach((a,i)=>i===cur?a.setAttribute('aria-current','location'):a.removeAttribute('aria-current'));
const l=links[cur];if(l&&(l.offsetLeft<nav.scrollLeft||l.offsetLeft+l.offsetWidth>nav.scrollLeft+nav.clientWidth))nav.scrollLeft=l.offsetLeft-16}
totop.classList.toggle('show',scrollY>innerHeight)}
addEventListener('scroll',spy,{passive:true});spy();
"""

INFO_ICON = (
    '<svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true"><circle cx="10" cy="10" r="8" fill="none" '
    'stroke="currentColor" stroke-width="1.6"/><path d="M10 9v5" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round"/><circle cx="10" cy="6.1" r="1.1" fill="currentColor"/></svg>'
)
# Lucide arrow-up, the brand's icon set (inlined so the page needs no icon requests).
ARROW_UP = (
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12 7-7 7 7"/><path d="M12 19V5"/></svg>'
)
DOWNLOAD_ICON = (
    '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 15V3"/>'
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/></svg>'
)
FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Nunito+Sans:ital,wght@0,400;0,500;0,600;0,700;0,800;1,400&amp;display=swap">'
)
# The header's signal motif: curves resolving toward one orange point.
MAST_ART = (
    '<svg class="mast-art" viewBox="0 0 640 280" preserveAspectRatio="xMaxYMax slice" aria-hidden="true" fill="none">'
    '<path d="M-40 268C160 262 260 120 440 112S600 150 680 178" stroke="rgba(205,213,225,.5)" stroke-width="1.5"/>'
    '<path d="M0 222C170 205 300 64 470 56S610 92 680 118" stroke="rgba(205,213,225,.3)" stroke-width="1.3" stroke-dasharray="7 8"/>'
    '<path d="M110 280C260 262 400 196 680 210" stroke="rgba(205,213,225,.28)" stroke-width="1.3" stroke-dasharray="7 8"/>'
    '<circle cx="440" cy="112" r="15" fill="rgba(240,139,73,.2)"/><circle cx="440" cy="112" r="6" fill="#F08B49"/>'
    '<circle cx="207" cy="191" r="3.5" fill="#CDD5E1"/></svg>'
)
CREDIT = '<p class="credit">Created by Ying Chen, UX Researcher &amp; writer of Signals to Solutions newsletter.</p>'


def content_security_policy(script: str) -> str:
    """Only the report's own script may run, and the page may load nothing but Google Fonts.

    Every piece of review text is escaped already; this is the second lock. If some text ever slipped through
    as markup, a browser would still refuse to run it or to send anything anywhere.
    """
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
    return (f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src data:; base-uri 'none'; form-action 'none'")


def render_report(summary: dict, narrative: str | None = None) -> str:
    global THUMBS
    app, o, jev = summary["app"], summary["overview"], summary["jev"]
    THUMBS = is_thumbs(app)
    rated = sum((o.get("rating_distribution") or {}).values())
    up = (o.get("rating_distribution") or {}).get("5", 0)
    store_share = f'Steam all-time {app["recommended_share"]:.0%}' if app.get("recommended_share") is not None else f"{up} of {rated} reviews"
    n = o["reviews_analyzed"]
    negative = o["sentiment_distribution"].get("very negative", 0) + o["sentiment_distribution"].get("negative", 0)
    date_range = " to ".join(o["date_range"]) if o.get("date_range") else "unknown dates"
    subtitle = f'{app.get("store") or "Reviews"} · {n} reviews · {date_range}' + (f' · {app["sort"]}' if app.get("sort") else "")
    page_link = source_link(app, f'View reviews on {app.get("store") or "the source page"}')
    generated = summary["generated_at"][:10]

    narrative_html = f'<section class="narrative" id="summary">{markdown(narrative, sections=True)}</section>' if narrative else ""
    kpis = "".join(
        [
            kpi("Reviews analyzed", n, f'{o["off_topic"]} off-topic excluded' if o["off_topic"] else ""),
            kpi("Recommended", pct(up, rated), store_share) if THUMBS else
            kpi("Mean rating", f'{o["mean_rating"]}★' if o["mean_rating"] is not None else "–", f'store avg {app["average_rating"]:.2f}★' if app.get("average_rating") else ""),
            kpi("Negative sentiment", pct(negative, n), f"{negative} reviews"),
            kpi("Report a problem", pct(o["with_problem"], n), f'{o["blocking"]} blocking'),
            kpi("Bug reports", o["bugs"], f'{o["repro_detail"]} with repro detail'),
            kpi("Since an update", o["after_update"], "problem tied to a new version"),
            kpi("Churn signals", o["churn_risk"], "say they'll leave or left"),
            kpi("Feature requests", o["feature_requests"]),
        ]
    )

    area_ids = {a["name"]: a["id"] for a in summary["areas"]}
    timeline = summary.get("time")  # absent in summaries written before it existed
    problem_trend = ""
    if timeline:
        t, when = timeline["problem_trend"], day_label(timeline["split_date"])
        e, l = timeline["earlier_reviews"], timeline["later_reviews"]
        change = {"fewer": "fell", "more": "rose"}.get(t["direction"])
        shares = f'{t["earlier"]} of {e} ({pct(t["earlier"], e)}) before {when} and {t["later"]} of {l} ({pct(t["later"], l)}) since'
        problem_trend = (f'<p class="muted overall-trend"><strong>All areas:</strong> reviews reporting a problem {change} from {shares.replace(" and ", " to ")} '
                         f'(p = {t["p"]:.3f}).</p>' if change else
                         f'<p class="muted overall-trend"><strong>All areas:</strong> reviews reporting a problem: {shares}; no clear change.</p>')
    unassigned = summary["unassigned_problems"]
    all_reviews = summary.get("all_reviews") or []  # absent in summaries written before it existed
    tabs = review_tabs(
        [
            ("all", "All reviews", "Every analyzed review, newest first. Sentiment is judged from the words, not the rating.",
             len(all_reviews), all_reviews, True, True),
            ("bugs", "Bugs", "Reviews describing something broken.", o["bugs"], summary["bugs"][:12], True, False),
            ("requests", "Feature requests", "What reviewers ask to add, bring back, or change.", o["feature_requests"], summary["feature_requests"][:12], True, False),
            ("churn", "Say they'll leave", "Reviewers who say they left, cancelled, or will switch.", o["churn_risk"], summary["churn"][:9], True, False),
            ("update", "Since an update", "Reviewers tying a problem to a recent version or redesign.", o["after_update"], summary["after_update"][:9], True, False),
            ("no-area", "Fit no area", "Reviews that describe a problem but match no product area: candidates for a new area.",
             unassigned["count"], unassigned["examples"][:9], False, False),
            ("unsure", "Unsure labels", "Jev was close to 50/50 on a label for these reviews, so that label isn't counted. Worth a human look.",
             o["borderline_reviews"], summary["needs_review"][:9], False, False),
        ],
        area_ids,
        reviews_csv(summary, area_ids),
    )
    versions = version_table(summary["versions"])
    nav = [("summary", "Summary") if narrative else None, ("numbers", "Key numbers"), ("areas", "Product areas"),
           ("reviews", "Reviews") if tabs else None, ("versions", "Versions") if versions else None, ("method", "Method")]
    nav_html = "".join(f'<a href="#{key}">{label}</a>' for key, label in filter(None, nav))
    caveats = []
    if o["non_english"]:
        caveats.append(f'{o["non_english"]} reviews are not in English; Jev is most accurate on English, so treat their labels with more caution.')
    if o["rating_sentiment_mismatch"]:
        example = "a thumbs-up with a complaint" if THUMBS else "5★ with a complaint"
        caveats.append(f'{o["rating_sentiment_mismatch"]} reviews have a rating that disagrees with the text (e.g. {example}); labels follow the text.')
    if jev.get("failed_reviews"):
        caveats.append(f'{jev["failed_reviews"]} reviews failed to process and are excluded.')
    caveat_html = "".join(f"<li>{esc(c)}</li>" for c in caveats)
    script = f"const LABELS={json.dumps(LABELS)};{JS}"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{esc(content_security_policy(script))}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(app["name"])} Review Triage</title>{FONTS}<style>{CSS}</style></head>
<body><main>
<header id="top" class="masthead">{MAST_ART}
<div class="mast-top"><span class="kicker">App review triage</span><span class="stamp">{esc(generated)}</span></div>
<div class="title-row"><h1>{esc(app["name"])}<span class="sr-only">:</span> <em>what reviewers want fixed</em></h1>
<button type="button" class="info" aria-controls="about" aria-expanded="false" aria-label="How this report was made" data-tip="How this report was made">{INFO_ICON}</button></div>
<p class="sub">{esc(subtitle)}</p>
{f'<p class="source-link">{page_link}</p>' if page_link else ""}</header>
<nav class="toc" aria-label="Sections">{nav_html}</nav>
{about_section(summary, bool(narrative))}
{narrative_html}
<div class="kpis" id="numbers">{kpis}</div>

<section><h2>Sentiment</h2><p class="muted">How reviewers feel, judged from their words (not their rating).{" Click a sentiment to read those reviews." if all_reviews else ""}</p>
{sentiment_bar(o["sentiment_distribution"], n, clickable=bool(all_reviews))}</section>

<section id="areas" class="collapsible" data-view="report"><div class="sec-head"><h2>Product areas, ranked</h2>{view_tools("areas", '<button type="button" class="expand-all">Expand all</button>')}</div>
<div class="view-report"><p class="muted">Click a row to read what reviewers said. <span class="ellip">…</span> marks a quote cut from a longer review. <a href="#glossary">What do the columns mean?</a></p>{problem_trend}
<div class="area-head"><span>#</span><span>Product area</span><span>Likely rank</span><span>{"By " + timeline["grain"] if timeline else ""}</span><span>Issues</span><span>Severity</span><span>Blocking</span><span>Churn</span><span>Since update</span><span>Praise</span></div>
{area_rows(summary["areas"], n, timeline)}
</div>{table_view("areas", areas_csv(summary), "Every product area, including any with no complaints.")}
</section>

{tabs}
{versions}

<section id="method"><h2>Method and caveats</h2>
<ul>
<li>Each review was sent to Jev ({esc(jev.get("model") or "jev-latest")}) with every question asked in parallel: overall sentiment (5-level Score), problem severity (4-level Score), one yes/no question per product area for problems and for praise, plus bug, feature request, churn, "since update", repro detail, off-topic, and language checks, and which of the review's own sentences states its main problem.</li>
<li>A second pass asked Jev, for each review quoted under an area, which of its sentences describes the problem with that area. Quotes are always the reviewer's own words; nothing is generated.</li>
<li>Jev input tokens for all answers: {jev.get("total_input_tokens", 0):,} (about ${jev.get("total_cost_usd", 0)}).</li>
<li>A label counts when Jev's probability is ≥ {summary["thresholds"]["yes"]}; {summary["thresholds"]["unsure_low"]}–{summary["thresholds"]["yes"]} is reported as borderline. Counts, ranking, and priority weights are computed in code from those answers.</li>
<li>Star ratings, dates, and versions are never sent to Jev; they are joined back in code.</li>
{caveat_html}
</ul></section>
<footer>{CREDIT}
<p class="gen">Generated {esc(summary["generated_at"])} · source {source_link(app) or esc(app.get("url") or "")}</p></footer>
</main><a class="totop" href="#top" aria-label="Back to top">{ARROW_UP}</a><div id="tip" role="tooltip"></div><script>{script}</script></body></html>
"""
