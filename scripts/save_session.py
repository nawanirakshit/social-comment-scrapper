"""Log in to Instagram by hand once and save the browser session for the actor.

Usage (from the project root):
    .venv/bin/python scripts/save_session.py [output path]

A browser window opens on the Instagram login page. Log in yourself, including any
2FA or "Was this you?" checks. The script waits until it sees a logged-in session
cookie, then saves cookies + local storage to .secrets/ig_state.json (by default).

Use a spare account, never your personal one. The password is never seen or
stored by this script; only the resulting session is.
"""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

LOGIN_URL = "https://www.instagram.com/accounts/login/"
TIMEOUT_MINUTES = 10


async def main(out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        # Plain Chromium with no fingerprint changes, matching what the actor uses
        # when a session is loaded, so Instagram sees the same browser both times.
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(LOGIN_URL)
        print(f"Log in in the browser window. Waiting up to {TIMEOUT_MINUTES} minutes...")

        for _ in range(TIMEOUT_MINUTES * 60):
            cookies = await context.cookies("https://www.instagram.com")
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                break
            await asyncio.sleep(1)
        else:
            print("No logged-in session seen; nothing saved.")
            await browser.close()
            sys.exit(1)

        # Let Instagram finish post-login redirects and set its remaining cookies.
        await page.wait_for_timeout(5_000)
        await context.storage_state(path=str(out))
        out.chmod(0o600)
        await browser.close()
        print(f"Saved session to {out}. Treat this file like a password.")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".secrets/ig_state.json")))
