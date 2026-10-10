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
  private network hosts, also after a redirect), refuses responses over 25 MB and requests
  that take over 2 minutes from start to finish (headers included), and validates store
  country, language, and app ids.
- `fetch_reviews.py` and `triage.py` reduce every field, including the app's name,
  description, and the rest of an export's `app` block, to one line of plain text with
  control characters and bidirectional overrides removed. They accept only plausible ISO
  dates and version-like versions, and check the types in `reviews.json` and `areas.json`
  before using them.
- `brief.md` lists the code's labels before each quote and writes titles and quotes as JSON
  strings, so a review can't close its quote and add labels of its own.
- Every line `triage.py` prints starts with the code's own text, so an app's name can't pose
  as the `open in browser:` command, and `--open` opens the report without a shell.
- `SKILL.md` tells Claude to treat reviews as data, never follow instructions in them, and
  never put review text or script output into a command, and to tell the user to keep
  Claude Code's permission prompts on. This lowers the risk of prompt injection through a
  review but can't rule it out.

## Known limits

- The private-address check resolves the host just before the request, and the request
  resolves it again. A domain built to answer with a public address and then a local one
  (DNS rebinding) could get one request through to the user's own network. Its answer is
  used only if it holds schema.org review data.
- Prompt injection: review text reaches Claude, and instructions can't make a model ignore
  everything it reads. Keep Claude Code's permission prompts on while using this skill.

## Your API key

The TypeSafe SDK reads `TYPESAFE_API_KEY` from the environment. Nothing in this repository
prints the key or writes it to a file. Never commit it, and never paste it into a chat.
