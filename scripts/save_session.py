"""Log in to Instagram or Facebook by hand once and save the browser session.

Usage (from the project root):
    .venv/bin/python scripts/save_session.py                # Instagram -> .secrets/ig_state.json
    .venv/bin/python scripts/save_session.py facebook       # Facebook  -> .secrets/fb_state.json
    .venv/bin/python scripts/save_session.py facebook out.json

A browser window opens on the login page. Log in yourself, including any 2FA or
"Was this you?" checks. The script waits until it sees the site's logged-in cookie,
then saves cookies + local storage. Point the actor at the file with
sessionStatePath (Instagram) or facebookSessionStatePath (Facebook).

Use a spare account, never your personal one. The password is never seen or
stored by this script; only the resulting session is.
"""

import asyncio
import sys
from pathlib import Path

from playwright.async_api import async_playwright

# site: (login page, cookie domain, cookie that only exists when logged in, default output)
SITES = {
    "instagram": ("https://www.instagram.com/accounts/login/", "https://www.instagram.com",
                  "sessionid", ".secrets/ig_state.json"),
    "facebook": ("https://www.facebook.com/login/", "https://www.facebook.com",
                 "c_user", ".secrets/fb_state.json"),
}
TIMEOUT_MINUTES = 10


async def main(site: str, out: Path) -> None:
    login_url, cookie_url, cookie_name, _ = SITES[site]
    out.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        # Plain Chromium with no fingerprint changes, matching what the actor uses
        # when a session is loaded, so the site sees the same browser both times.
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(login_url)
        print(f"Log in to {site} in the browser window. Waiting up to {TIMEOUT_MINUTES} minutes...")

        for _ in range(TIMEOUT_MINUTES * 60):
            cookies = await context.cookies(cookie_url)
            if any(c["name"] == cookie_name and c["value"] for c in cookies):
                break
            await asyncio.sleep(1)
        else:
            print("No logged-in session seen; nothing saved.")
            await browser.close()
            sys.exit(1)

        # Let the site finish post-login redirects and set its remaining cookies.
        await page.wait_for_timeout(5_000)
        await context.storage_state(path=str(out))
        out.chmod(0o600)
        await browser.close()
        print(f"Saved {site} session to {out}. Treat this file like a password.")


if __name__ == "__main__":
    args = sys.argv[1:]
    site = args.pop(0) if args and args[0] in SITES else "instagram"
    out = Path(args[0]) if args else Path(SITES[site][3])
    asyncio.run(main(site, out))
