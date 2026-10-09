# app-review-triage

A Claude Code skill that turns an app-store review page into a ranked "what to fix first"
report for a product team. Paste a link to an App Store, Google Play, or Steam page (or a
CSV/JSON export of reviews), and Claude:

1. fetches the reviews,
2. drafts product areas specific to the app (e.g. "Free-tier limits", "Editor and typing"),
3. has [TypeSafe's Jev model](https://docs.typesafe.ai) label every review for sentiment,
   severity, product-area problems and praise, bugs, feature requests, churn signals, and
   "since an update" regressions,
4. ranks the areas in code and writes a narrative with verbatim quotes.

300 reviews take about 6 seconds of model time and cost roughly $0.06–0.10. One run handles
up to 10,000 reviews; see [How many reviews](#how-many-reviews).

## Output

- `report.html`: a self-contained report with sentiment, ranked areas and their strongest
  quotes, bugs, feature requests, churn signals, version breakdown, and borderline cases.
  Claude ends with a clickable link to it.
- `review_labels.csv`: one row per review with every label and probability.
- `brief.md` / `summary.json`: the aggregates behind the report.

## Install

You need [Claude Code](https://docs.claude.com/en/docs/claude-code/overview) and a TypeSafe
API key. Nothing else from other repositories is required.

**1. Clone this repository into your Claude Code skills folder.**

```bash
git clone https://github.com/Ying8109/app-review-triage.git ~/.claude/skills/app-review-triage
```

To use it in one project only, clone it into that project's `.claude/skills/app-review-triage`
instead.

**2. Install uv** (recommended). It runs the scripts and installs their Python
dependencies on first use, including a suitable Python if you don't have one.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh    # or: brew install uv
```

See the [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/) for Windows and other options.

Without uv you need Python 3.10 or newer. Create a virtual environment inside the skill
folder once, and Claude will use it:

```bash
cd ~/.claude/skills/app-review-triage && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

**3. Set your TypeSafe API key.** Create one at https://console.typesafe.ai/keys, then add
it to your shell profile (`~/.zshrc` or `~/.bashrc`) so Claude Code can see it:

```bash
export TYPESAFE_API_KEY="your-key-here"
```

Open a new terminal (or `source` your profile) and start Claude Code from it. In the Claude
desktop app, which doesn't start from a terminal, Claude runs the model steps through an
interactive shell so they can read the key from your profile. Never paste the key into a
chat or commit it to a repository.

**4. Start a new Claude Code session.** The skill is picked up automatically.

## Use

In Claude Code, run `/app-review-triage` or just ask:

> Here's our App Store page: https://apps.apple.com/us/app/... — what should the product team fix?

Before running anything, Claude asks three things and waits for your answers:

1. whether your TypeSafe API key is set as `TYPESAFE_API_KEY` (never the key itself; don't
   paste it into chat),
2. which product and review page (or export) to use,
3. how many reviews to analyze, with the time, cost, and limits below.

## How many reviews

Measured live with `jev-1.13.0` and about 18 product areas. Model time only; Claude's own
steps (drafting product areas, writing the summary) add a few minutes.

| Reviews | Model time | Jev cost | Report size |
| --- | --- | --- | --- |
| 300 (default) | ~6 s | ~$0.10 | ~0.5 MB |
| 1,000 | ~20 s | ~$0.30 | ~1.2 MB |
| 5,000 | ~1.5 min | ~$1.50 | ~5 MB |
| 10,000 (maximum per run) | ~3 min | ~$3 | ~10 MB |

- Apple App Store: the public feed stops at the newest 500 reviews. For more, export them
  from App Store Connect (Ratings and Reviews) and give Claude the file.
- Google Play and Steam: up to 10,000 per run. Exports: any size up to 10,000.
- Over 10,000, `triage.py` stops before calling the model and suggests an even sample across
  a date window (`--since`). `--allow-large` runs them all; the report gets slow to open.
- Answers are saved every 500 reviews, so an interrupted run resumes where it stopped and
  nothing is paid for twice.

## Layout

| Path | What it is |
| --- | --- |
| `SKILL.md` | The workflow Claude follows |
| `scripts/fetch_reviews.py` | Fetches App Store, Google Play, Steam, JSON-LD pages, or normalizes exports |
| `scripts/sample_reviews.py` | Prints a review sample for drafting product areas |
| `scripts/triage.py` | Runs Jev (two passes, cached per question), aggregates, and writes outputs |
| `scripts/jev_questions.py` | Every Jev question and threshold, in one place for review |
| `scripts/report_html.py` | Renders the HTML report |
| `references/default_areas.json` | Generic product areas to start from |
| `requirements.txt` | Python dependencies, for setups without uv |
| `tests/` | Tests with a fake model and made-up reviews; no API key or network needed |
| `SECURITY.md` | How to report a security problem, and what the skill defends against |
| `LICENSE` | MIT |

## Security and privacy

- **What leaves your machine.** Each review's title and text, plus the app's name, go to
  TypeSafe's API for labeling. Star ratings, dates, and versions stay local. The fetcher
  contacts only the store or page you link to, and the report's only outside request is
  the Google Fonts stylesheet.
- **Your API key.** The TypeSafe SDK reads `TYPESAFE_API_KEY` from your environment. No
  script prints it or writes it to a file. Don't paste it into chat.
- **Reviews are untrusted input.** Anyone can write a review, so the scripts treat review
  text as hostile:
  - every field is escaped in `report.html`;
  - the report's Content-Security-Policy lets only its own script run;
  - CSV cells that start like a spreadsheet formula (`=`, `+`, `-`, `@`) get a leading
    apostrophe, in both `review_labels.csv` and the report's Download CSV;
  - Claude is told to read reviews as data and never follow instructions inside them.
- **Fetching.** Only public `http(s)` links are fetched: never `file://` or `ftp://`, and
  never local or private network addresses, including through a redirect. A response over
  25 MB or slower than 2 minutes is refused. For an export, only the file's name is
  recorded, not its folder.
- **Input checks.** Every field from a page or export becomes one line of plain text. Dates
  must be plausible `YYYY-MM-DD` dates and versions must look like versions; anything else
  is dropped rather than guessed.
- **Before you share a report**, remember it contains every analyzed review in full. A
  reviewer may have included personal details such as an email address or order number.
- **Private exports.** If you load support tickets or CRM feedback with `--from-file`, their
  text is sent to TypeSafe and appears in the report, just like public reviews. Make sure
  you're allowed to do that before you run it.

## Limits

- The Apple public feed returns at most the 500 newest reviews, which can cover only a
  day or two for very popular apps.
- A large newest-first pull can reach back years (5,000 Todoist reviews on Google Play went
  back almost 5 years). Give a start date when you want to know what's wrong now.
- Jev is most accurate on English; non-English reviews are labeled but flagged.
- Labels are probabilistic. Near-50/50 labels are excluded from counts and listed for
  human review, and a few reviews that raise several issues will land in an unexpected area.

## Tests

The tests replace Jev with a deterministic fake and use only made-up reviews, so they need
no API key, no network, and cost nothing:

```bash
uv run --python 3.12 --with pytest --with "typesafe-sdk>=0.7.2,<0.8" --with "google-play-scraper>=1.2.7,<2" pytest tests -q -p no:cacheprovider
```

Without uv: `.venv/bin/pip install pytest && .venv/bin/python -m pytest tests -q`.

## License

[MIT](LICENSE). Reports end with a one-line credit: "Created by Ying Chen, UX Researcher &
writer of Signals to Solutions newsletter."
