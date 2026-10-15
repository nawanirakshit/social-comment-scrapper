# Instagram & Facebook Post Comments Scraper (Apify actor)

A learning project that scrapes a **public Instagram or Facebook post**, along with its
comments and, optionally, their replies, and writes the results to an Excel file. It is built as an
[Apify](https://apify.com) actor with Python, [Crawlee](https://crawlee.dev/python) and
Playwright, so you can run it on your own machine or deploy it to the Apify platform.

What you get for each post:

| Post | Each comment / reply |
|---|---|
| author, caption, posted time (UTC) | author, text, posted time (UTC) |
| like count, comment count, shares (Facebook) | like count, reply count |
| full-size image URL | which comment it replies to |

Instagram and Facebook URLs can be mixed in one run.

---

## Contents

1. [How it works](#how-it-works)
2. [Requirements](#requirements)
3. [Setup](#setup)
4. [Your Apify account and token](#your-apify-account-and-token)
5. [Configure the input](#configure-the-input)
6. [Optional: use a logged-in Instagram session](#optional-use-a-logged-in-instagram-session)
7. [Run it and get the Excel file](#run-it-and-get-the-excel-file)
8. [Deploy to the Apify platform](#deploy-to-the-apify-platform)
9. [Tests](#tests)
10. [Troubleshooting](#troubleshooting)
11. [Limits and responsible use](#limits-and-responsible-use)

---

## How it works

1. Playwright opens the post in a real Chromium browser.
2. The post itself, plus the first page of comments, is read from JSON that Instagram embeds in the
   page HTML (`<script type="application/json">`).
3. More comments are loaded by scrolling the comments panel (or clicking "load more"),
   and the background GraphQL responses are captured.
4. `src/parsers.py` turns that JSON into rows. Meta tags (`og:description`) are a fallback.
5. Each post and each comment becomes a row in the Apify dataset; `scripts/export_excel.py`
   turns the dataset into `results.xlsx`.

**Logged out vs logged in.**
- **Instagram, logged out:** at most about 30–45 comments, and the counts are Instagram-only.
  Logged in, the counts include engagement from the cross-posted Facebook post, and the
  scraper can page through all top-level comments and replies.
- **Facebook, logged out:** roughly the first 10 comments, and Facebook soon shows a login
  wall instead of the post. Logged in, the scraper switches the comment list from "Most
  relevant" to **"All comments"** and loads them all.

Logging in needs a session from a **spare** account (see below).

## Requirements

- **Python 3.12+**
- **Node.js 18+** (for the Apify CLI)
- **macOS or Linux** (Windows works through WSL)
- An **Apify account**. It's free, and only needed for deploying or using Apify Proxy (see below).

## Setup

```bash
git clone https://github.com/<your-username>/<this-repo>.git
cd <this-repo>

# 1. Python environment and dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt     # actor deps + pytest + openpyxl

# 2. Browser used by Playwright
playwright install chromium

# 3. Apify CLI (runs the actor locally, deploys it to Apify)
npm install -g apify-cli
```

## Your Apify account and token

You **don't need an Apify token to run locally.** `apify run` works without one.

You **do** need it to:
- deploy the actor to your Apify account (`apify push`), or
- use **Apify Proxy** (`"useApifyProxy": true` in the input), including the residential proxies
  that work best against Instagram.

To add it:

1. Sign up at [apify.com](https://apify.com).
2. Copy your token from **Apify Console → Settings → API & Integrations**.
3. Log in once with the CLI. This stores the token in `~/.apify`, outside this repo:
   ```bash
   apify login
   ```
   Or, for one shell session only:
   ```bash
   export APIFY_TOKEN=your_token_here
   ```

Never commit your token. `.env` files are already in `.gitignore` if you keep it in one.

## Configure the input

The actor reads its input from `storage/key_value_stores/default/INPUT.json`.
`storage/` is not in the repo, because it holds your scraped data. The first time you run
`./run.sh`, it creates that file from [`INPUT.example.json`](INPUT.example.json) and stops.
Edit it, then run again.

```json
{
  "startUrls": [{ "url": "https://www.instagram.com/p/POST_SHORTCODE/" }],
  "maxComments": 200,
  "includeReplies": false,
  "saveRawResponses": false,
  "proxyConfiguration": { "useApifyProxy": false },
  "sessionStatePath": ""
}
```

| Field | What it does | Default |
|---|---|---|
| `startUrls` | Instagram post or reel URLs (`/p/...` or `/reel/...`). | required |
| `maxComments` | Maximum **top-level** comments per post. Replies come on top. | `50` |
| `includeReplies` | Click every "View all N replies" link and save the replies too. One extra request per thread. | `false` |
| `maxLoadMoreRounds` | Optional. Maximum scroll/"load more" rounds. Each round adds roughly 7–10 comments. | worked out from `maxComments` |
| `requestTimeoutSecs` | Optional. Time limit per post. **If it runs out, that post's rows are lost** and it is retried. | about 7 s per round + 60 s (doubled with replies) |
| `saveRawResponses` | Save every captured JSON response to the key-value store. Useful when Instagram changes its format. | `true` |
| `headless` | Run the browser without a window. Set `false` to watch it while debugging. Either way the browser identifies itself as regular Chrome, not "HeadlessChrome". | `true` |
| `proxyConfiguration` | Apify Proxy settings. Needs a token; see above. | no proxy |
| `sessionStatePath` | Path to a saved **Instagram** session file for local runs; see below. Empty means logged out. | empty |
| `sessionState` | The same Instagram session, pasted as JSON. Used on the Apify platform, where it's stored encrypted. | empty |
| `facebookSessionStatePath` | Path to a saved **Facebook** session file for local runs. | empty |
| `facebookSessionState` | The same Facebook session, pasted as JSON, for the Apify platform. | empty |

The loop stops early when Instagram reports there are no more comments, when
`maxComments` is reached, or when a login or verification dialog appears.

## Optional: use logged-in sessions

Use **spare accounts, never your personal ones.** Automated browsing can get an account
rate-limited, sent to "verify it's you" checks, or banned.

1. Save a session by logging in by hand. A browser window opens, and the script only keeps the
   resulting cookies, never your password:
   ```bash
   .venv/bin/python scripts/save_session.py            # Instagram -> .secrets/ig_state.json
   .venv/bin/python scripts/save_session.py facebook   # Facebook  -> .secrets/fb_state.json
   ```
   Log in, including any 2FA prompt. `.secrets/` is git-ignored. **Treat these files like
   passwords.**
2. Point the input at them. You can use either one or both:
   ```json
   "sessionStatePath": ".secrets/ig_state.json",
   "facebookSessionStatePath": ".secrets/fb_state.json"
   ```
3. When the session expires or Instagram asks the account to verify itself, the actor writes
   an error row `session_expired` and stops. Run `save_session.py` again. The actor never
   tries to get past these checks automatically.

## Run it and get the Excel file

```bash
./run.sh
```

This runs the actor (`apify run --purge`) and then writes **`results.xlsx`** in the project
root, with these sheets:

- **Posts**: one row per post.
- **Comments**: one row per comment. Replies sit directly under their parent, with the parent's ID in **Reply to**.
- **Errors**: only present if something failed, for example `login_wall` or `session_expired`.

Times are real Excel dates in UTC, so you can sort and filter them.

Other commands:

```bash
apify run --purge                          # scrape only
.venv/bin/python scripts/export_excel.py   # re-export the last run to results.xlsx
```

The run log ends with a line such as
`Saved post + 690 comments + 45 replies (… 80 load-more rounds, 46 reply clicks)`.
Raw rows are in `storage/datasets/default/`, one JSON file per row.

## Deploy to the Apify platform

```bash
apify login     # once, with your token
apify push      # builds the Docker image and creates/updates the actor in your account
```

Then open the actor in Apify Console, fill in the input form, and run it.

- For a logged-in run, paste the **contents** of `.secrets/ig_state.json` into the
  **Session state JSON** field. It's a secret field, so Apify stores it encrypted.
- The platform runs in a datacenter, and Instagram blocks datacenter IPs quickly, so use
  Apify Proxy's **RESIDENTIAL** group. Ideally use the same country you logged in from.
- `run.sh` and the Excel export are for local use. On the platform, download results from
  the run's **Storage → Dataset** tab, which supports Excel and CSV export.

## Tests

```bash
.venv/bin/pytest
```

The parser tests run against trimmed copies of real Instagram responses in
`tests/fixtures/`. Commenter usernames, @mentions and account IDs in those fixtures are
replaced with pseudonyms (`user_01`, …).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Multiple Python packages found` from `apify run` | A folder other than `src/` has an `__init__.py`. Delete it. `tests/` and `scripts/` must not be packages. |
| Only ~15–45 comments | You're logged out; that's Instagram's limit. Use a session. |
| Log says `Stopped at the round limit` | Raise `maxLoadMoreRounds` (and `requestTimeoutSecs` if you set it). |
| Log says `…although Instagram says more exist` | Instagram is slowing the account down. Wait before running again. |
| `debug_no_scroll_panel` screenshot in the key-value store | The comments panel never rendered. Open the screenshot to see what the page showed. |
| `session_expired` row | Run `scripts/save_session.py` again (add `facebook` for a Facebook session). |
| `login_wall` row on Facebook, with a `debug_login_wall` screenshot | Facebook showed its login form instead of the post. Use a Facebook session, or wait before retrying. |
| Log says `Page never went network-idle` | Normal for logged-in Facebook, which keeps chat connections open. The run continues. |
| `Can't write results.xlsx` | Close the file in Excel and rerun the export. |

## Limits and responsible use

- **Instagram changes its page format often.** Field locations are documented at the top of the
  Instagram section in `src/parsers.py`. Run with `saveRawResponses: true` to find the new ones.
- **Meta's terms of service restrict automated collection,** and logging in means you have
  agreed to them. Using a session is at your own risk, and the account may be banned.
- **Comment authors' usernames and text are personal data** under laws such as the GDPR and
  India's DPDP Act. Collect only what you need, keep it secure, and delete it when you're done.
- This project doesn't solve CAPTCHAs or get around login or verification checks, and it
  shouldn't. Keep concurrency low: the defaults are 1–2 pages at a time with random waits.
