# Security

## Reporting a problem

Please report security problems privately through this repository's **Security** tab
(**Report a vulnerability**), not in a public issue. Include the steps to reproduce and
what an attacker could do.

## What this skill defends against

Anyone can write an app review, so review text, titles, app metadata, exports, and every
file made from them are treated as untrusted:

- `report.html` escapes every field and sets a Content-Security-Policy that allows only its
  own script, so text in a review can't run as code in a reader's browser.
- `review_labels.csv` and the report's Download CSV prefix cells that start like a
  spreadsheet formula (`=`, `+`, `-`, `@`) with an apostrophe.
- `fetch_reviews.py` fetches only public `http(s)` addresses (no `file://`, no local or
  private network hosts, also after a redirect), refuses responses over 25 MB or slower
  than 2 minutes, and validates store country, language, and app ids.
- `fetch_reviews.py` and `triage.py` reduce every field to one line of plain text, accept
  only plausible ISO dates and version-like versions, and check the types in
  `reviews.json` and `areas.json` before using them.
- `SKILL.md` tells Claude to treat reviews as data and never follow instructions in them.

## Your API key

The TypeSafe SDK reads `TYPESAFE_API_KEY` from the environment. Nothing in this repository
prints the key or writes it to a file. Never commit it, and never paste it into a chat.
