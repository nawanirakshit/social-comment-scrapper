"""Instagram & Facebook public post scraper (learning project).

Strategy
--------
1. Open each post URL in a real browser (Playwright) through a residential proxy.
2. Capture the JSON responses the page loads in the background (GraphQL / XHR).
   That JSON is far more stable than the HTML, so parsers work on it.
3. As a fallback, read the page's <meta> tags (og:description etc.), which
   usually contain the like/comment counts and caption for public posts.
4. Push one "post" item plus one item per comment to the dataset.

Only public pages are visited; the actor never logs in.
"""

from __future__ import annotations

import json
import math
import random
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from apify import Actor
from crawlee import ConcurrencySettings
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext, PlaywrightPreNavCrawlingContext
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .parsers import (dig, has_more_comments, nest_replies, parse_facebook, parse_instagram, parse_meta_tags,
                      walk)

# Background responses worth keeping. Everything else (images, tracking) is ignored.
INTERESTING_RESPONSE = re.compile(r"/graphql|/api/v1/|/ajax/", re.I)

# "View more comments" / "Load more comments" controls. Instagram uses an icon button
# with an aria-label; Facebook uses a text link inside a role="button" div.
LOAD_MORE_COMMENTS = ", ".join([
    '[aria-label*="more comments" i]',
    '[role="button"]:has-text("more comments")',
    'button:has-text("more comments")',
])
# Measured on Instagram (Oct 2026): a 40-round logged-in run took 254 s (~5.7 s per
# round including page load) and loaded 28 pages of up to 15 comments (~10 per round).
SECONDS_PER_ROUND = 7  # rounded up: running out of time loses the whole post
COMMENTS_PER_ROUND = 7
MAX_ROUNDS_LIMIT = 200
# Rounds without new comments before giving up. Instagram can take 10+ s per page
# late in a long thread, so while it still says has_next_page, wait longer.
STALE_ROUNDS_WHEN_MORE = 5
STALE_ROUNDS_OTHERWISE = 2
# "View all 2 replies" / "View 3 more replies" links under a comment. After a click
# the link turns into "Hide all replies", so each thread is only opened once.
VIEW_REPLIES = re.compile(r"^\s*View (all )?\d+ (more )?repl", re.I)
PAGE_OVERHEAD_SECONDS = 60  # page load, splash screen wait, saving rows
# Text of a dialog that asks the visitor to log in or sign up, else null. A dialog
# alone isn't enough: Facebook opens posts themselves in a dialog.
LOGIN_GATE_JS = """() => {
    for (const d of document.querySelectorAll('div[role="dialog"]')) {
        const text = d.innerText || '';
        if (d.querySelector('input[type="password"]')
                || /\\b(log in|sign up|create new account)\\b/i.test(text)) {
            return text.replace(/\\s+/g, ' ').slice(0, 200);
        }
    }
    return null;
}"""

# True once the page has rendered something to load more comments with. Plain DOM
# code: LOAD_MORE_COMMENTS uses Playwright-only :has-text(), which querySelector rejects.
HAS_LOAD_MORE_TARGET_JS = """() => {
    for (const el of document.querySelectorAll('[aria-label], [role="button"], button')) {
        if (/more comments/i.test(el.getAttribute('aria-label') || el.textContent || '')) return true;
    }
    for (const el of document.querySelectorAll('div, section, ul')) {
        const style = getComputedStyle(el);
        if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 50) return true;
    }
    return false;
}"""

# Find the tallest element with its own scrollbar (e.g. Instagram's logged-in comments
# side panel), bring it into view, and return its centre so the mouse wheel can be
# used over it. Instagram ignores scrollTop changes made from JavaScript; it needs
# real wheel input. Returns null when there is no such panel.
FIND_SCROLL_PANEL_JS = """() => {
    let best = null;
    for (const el of document.querySelectorAll('div, section, ul')) {
        const style = getComputedStyle(el);
        if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 50
                && (!best || el.clientHeight > best.clientHeight)) {
            best = el;
        }
    }
    if (!best) return null;
    best.scrollIntoView({block: 'center'});
    const r = best.getBoundingClientRect();
    return [r.x + r.width / 2, r.y + r.height / 2];
}"""

# Instagram's logged-out comment query. The "load more" button only shows a sign-up
# dialog, but the server still answers this query for the next cursor.
IG_COMMENTS_QUERY = "PolarisLoggedOutDesktopWWWPostCommentsPaginationQuery"
MAX_COMMENT_PAGES = 20
# Request headers the page sends with GraphQL calls; cookies are added by the browser.
REPLAY_HEADERS = ("content-type", "x-asbd-id", "x-csrftoken", "x-fb-friendly-name",
                  "x-fb-lsd", "x-ig-app-id", "x-ig-max-touch-points")


# A secure page served by the browser itself (never hits the network), used to read
# the browser's client-hint details before overriding the user agent.
UA_PROBE_URL = "https://ua-probe.invalid/"
UA_HINTS_JS = """async () => {
    const d = await navigator.userAgentData.getHighEntropyValues(
        ['architecture', 'bitness', 'model', 'platformVersion', 'fullVersionList', 'wow64']);
    return {
        brands: navigator.userAgentData.brands,
        fullVersionList: d.fullVersionList,
        platform: navigator.userAgentData.platform,
        platformVersion: d.platformVersion,
        architecture: d.architecture,
        model: d.model,
        mobile: navigator.userAgentData.mobile,
        bitness: d.bitness,
        wow64: d.wow64,
    };
}"""


async def match_headed_user_agent(page) -> None:
    """Make a headless page identify itself exactly like a visible Chrome.

    Headless Chromium says "HeadlessChrome" in its User-Agent, an easy bot signal.
    Overriding only the User-Agent string drops the sec-ch-ua client-hint headers,
    so the browser's own client hints (which don't mention headless) are passed too.
    """
    ua = await page.evaluate("navigator.userAgent")
    if "HeadlessChrome" not in ua:
        return

    async def serve_probe(route):
        await route.fulfill(body="<html></html>", content_type="text/html")

    await page.route(UA_PROBE_URL, serve_probe)
    await page.goto(UA_PROBE_URL)
    hints = await page.evaluate(UA_HINTS_JS)
    await page.unroute(UA_PROBE_URL)

    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Network.setUserAgentOverride", {
        "userAgent": ua.replace("HeadlessChrome", "Chrome"),
        "userAgentMetadata": hints,
    })


def detect_platform(url: str) -> str | None:
    if "instagram.com" in url:
        return "instagram"
    if "facebook.com" in url or "fb.watch" in url:
        return "facebook"
    return None


async def main() -> None:
    async with Actor:
        actor_input = await Actor.get_input() or {}
        start_urls = [u["url"] for u in actor_input.get("startUrls", []) if u.get("url")]
        max_comments: int = actor_input.get("maxComments", 50)
        save_raw: bool = actor_input.get("saveRawResponses", True)
        headless: bool = actor_input.get("headless", True)

        # Optional logged-in sessions (spare accounts), made by scripts/save_session.py.
        # Each platform's cookies only apply to its own domain, so both are merged
        # into one browser storage state and a run can mix Instagram and Facebook URLs.
        session_state: dict | None = None
        session_platforms: set[str] = set()
        for platform_name, secret_key, path_key in (
            ("instagram", "sessionState", "sessionStatePath"),
            ("facebook", "facebookSessionState", "facebookSessionStatePath"),
        ):
            if actor_input.get(secret_key):
                state = json.loads(actor_input[secret_key])
            elif path := actor_input.get(path_key):
                if not Path(path).is_file():
                    await Actor.fail(status_message=f"{path_key} not found: {path}")
                    return
                state = json.loads(Path(path).read_text())
            else:
                continue
            session_state = session_state or {"cookies": [], "origins": []}
            session_state["cookies"] += state.get("cookies", [])
            session_state["origins"] += state.get("origins", [])
            session_platforms.add(platform_name)
        if session_platforms:
            Actor.log.info(f"Using saved logged-in sessions for: {', '.join(sorted(session_platforms))}.")

        if not start_urls:
            await Actor.fail(status_message="No startUrls provided.")
            return

        # How long to keep loading comments. Both default from maxComments; set them
        # in the input to override. Rows are only saved once a post finishes, so the
        # timeout must cover every round or that post's work is lost and retried.
        max_rounds: int = actor_input.get("maxLoadMoreRounds")
        if max_rounds is None:
            max_rounds = min(math.ceil(max_comments / COMMENTS_PER_ROUND) + 2, MAX_ROUNDS_LIMIT)
        timeout_secs: int = actor_input.get("requestTimeoutSecs")
        if timeout_secs is None:
            timeout_secs = PAGE_OVERHEAD_SECONDS + max_rounds * SECONDS_PER_ROUND
        include_replies: bool = actor_input.get("includeReplies", False)
        if include_replies and actor_input.get("requestTimeoutSecs") is None:
            # Reply clicks are capped at max_rounds too, at about the same pace.
            timeout_secs += max_rounds * SECONDS_PER_ROUND
        Actor.log.info(f"Up to {max_comments} comments per post"
                       f"{' plus their replies' if include_replies else ''}: at most {max_rounds} "
                       f"load-more rounds, {timeout_secs} s per post.")

        proxy = await Actor.create_proxy_configuration(
            actor_proxy_input=actor_input.get("proxyConfiguration"),
        )

        crawler = PlaywrightCrawler(
            proxy_configuration=proxy,
            # No browser window by default. Set "headless": false in the input to watch it.
            headless=headless,
            # Headless mode of the full Chromium build, which behaves like the visible one.
            browser_launch_options={"channel": "chromium"} if headless else None,
            # With a session, load its cookies and keep the browser identical to the
            # one used to log in: a new random fingerprint each run looks like the
            # account hopping between devices, which triggers Instagram's checks.
            # Crawlee only applies storage_state to fresh per-page (incognito) contexts.
            # Without a fingerprint Crawlee still injects random User-Agent / sec-ch-ua /
            # Accept headers into every request; empty extra_http_headers turns that off
            # so the browser matches the one used to log in (with them, the logged-in
            # app often stalled on its splash screen).
            browser_new_context_options={"storage_state": session_state, "extra_http_headers": {}}
            if session_state else None,
            **({"fingerprint_generator": None, "use_incognito_pages": True} if session_state else {}),
            max_requests_per_crawl=len(start_urls) * 2,
            max_request_retries=3,
            request_handler_timeout=timedelta(seconds=timeout_secs),
            # Keep it gentle: Meta blocks bursts of traffic quickly.
            concurrency_settings=ConcurrencySettings(desired_concurrency=1, max_concurrency=2),
        )

        async def page_instagram_comments(context: PlaywrightCrawlingContext, bucket: list[dict],
                                          count_comments) -> int:
            """Repeat the page's own comment pagination query with the next cursor.

            Runs fetch() inside the page so the request carries the same anonymous
            session as the page itself. Returns the number of pages fetched.
            """
            pages = 0
            while pages < MAX_COMMENT_PAGES and count_comments() < max_comments:
                last = next((r for r in reversed(bucket)
                             if f"fb_api_req_friendly_name={IG_COMMENTS_QUERY}" in ((r.get("request") or {}).get("post_data") or "")),
                            None)
                page_info = dig(last, "body", "data", "xig_polaris_media", "comments_connection", "page_info")
                if not page_info or not page_info.get("has_next_page") or not page_info.get("end_cursor"):
                    context.log.info(f"Comment paging stopped: no next page ({pages} pages fetched).")
                    break

                form = parse_qs(last["request"]["post_data"], keep_blank_values=True)
                variables = json.loads(form["variables"][0])
                variables["after"] = page_info["end_cursor"]
                form["variables"] = [json.dumps(variables, separators=(",", ":"))]
                post_data = urlencode(form, doseq=True)
                headers = {k: v for k, v in last["request"]["headers"].items() if k in REPLAY_HEADERS}

                await context.page.wait_for_timeout(random.randint(1_500, 3_500))
                before = count_comments()
                result = await context.page.evaluate(
                    """async ([body, headers]) => {
                        const r = await fetch('/api/graphql', {method: 'POST', body, headers, credentials: 'include'});
                        return {status: r.status, text: await r.text()};
                    }""",
                    [post_data, headers],
                )
                pages += 1
                try:
                    body = json.loads(result["text"].removeprefix("for (;;);"))
                except json.JSONDecodeError:
                    context.log.warning(f"Comment paging stopped: HTTP {result['status']}, non-JSON reply.")
                    break
                if body.get("errors") or not dig(body, "data", "xig_polaris_media", "comments_connection"):
                    context.log.warning(f"Comment paging stopped: HTTP {result['status']}, "
                                        f"{dig(body, 'errors', 0, 'message') or 'no comments in reply'}.")
                    break
                # The response listener usually records this reply too; add it only if not.
                await context.page.wait_for_timeout(300)
                if not any((r.get("request") or {}).get("post_data") == post_data for r in bucket):
                    bucket.append({"url": "replay:/api/graphql", "body": body,
                                   "request": {"post_data": post_data, "headers": last["request"]["headers"]}})
                if count_comments() == before:
                    context.log.info("Comment paging stopped: page added no new comments.")
                    break
            return pages

        # Captured JSON bodies, keyed by request URL being crawled.
        captured: dict[str, list[dict]] = {}

        @crawler.pre_navigation_hook
        async def attach_listener(context: PlaywrightPreNavCrawlingContext) -> None:
            if headless:
                await match_headed_user_agent(context.page)
            bucket = captured.setdefault(context.request.url, [])

            async def on_response(response) -> None:
                if not INTERESTING_RESPONSE.search(response.url):
                    return
                try:
                    text = await response.text()
                except Exception:  # body not available (redirects, aborted requests)
                    return
                # Facebook prefixes JSON with "for (;;);" and sometimes sends several
                # JSON objects separated by newlines.
                text = text.removeprefix("for (;;);")
                # Keep the GraphQL request too, so a query (e.g. comment pagination)
                # can be repeated later with a different cursor.
                request = None
                if "/graphql" in response.url and response.request.method == "POST":
                    headers = await response.request.all_headers()
                    request = {"post_data": response.request.post_data,
                               "headers": {k: v for k, v in headers.items() if k in REPLAY_HEADERS}}
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("{"):
                        try:
                            bucket.append({"url": response.url, "body": json.loads(line), "request": request})
                        except json.JSONDecodeError:
                            pass

            context.page.on("response", on_response)

        @crawler.router.default_handler
        async def handle_post(context: PlaywrightCrawlingContext) -> None:
            url = context.request.url
            page = context.page
            platform = detect_platform(url)
            context.log.info(f"Processing {platform} post: {url}")

            # Let lazy content and background requests finish. Logged-in Facebook keeps
            # live connections (chat, notifications) open and never goes idle, so a
            # timeout here is normal; the comments-panel wait below is the real signal.
            try:
                await page.wait_for_load_state("networkidle", timeout=30_000)
            except PlaywrightTimeoutError:
                context.log.info("Page never went network-idle (normal for logged-in Facebook); continuing.")

            # Detect login walls early so you know why data is missing. With a session,
            # a redirect means it expired or Instagram wants the account verified: stop
            # and refresh it by hand with scripts/save_session.py, never automatically.
            if any(part in page.url for part in ("/accounts/login", "/login", "/challenge", "/accounts/suspended")):
                error = "session_expired" if platform in session_platforms else "login_wall"
                context.log.warning(f"Redirected to {page.url} for {url} ({error}).")
                await context.push_data({"platform": platform, "type": "error", "url": url, "error": error})
                return

            # Close cookie / login popups if they appear (best effort). Not dialogs on
            # Facebook: it shows the post itself in a dialog.
            popups = ['button:has-text("Decline optional cookies")', 'button:has-text("Allow all cookies")']
            if platform == "instagram":
                popups.append('div[role="dialog"] [aria-label="Close"]')
            for selector in popups:
                try:
                    await page.click(selector, timeout=1_500)
                except Exception:
                    pass

            # Scroll a bit to trigger comment loading.
            for _ in range(3):
                await page.mouse.wheel(0, 1500)
                await page.wait_for_timeout(1_200)

            # Instagram embeds the post itself (caption, counts, owner) and the first
            # page of comments in the initial HTML as <script type="application/json">
            # blobs, not in a background request, so the response listener above never
            # sees it. They go first because they were loaded first.
            inline: list[dict] = []
            for blob in await page.eval_on_selector_all(
                'script[type="application/json"]', "els => els.map(e => e.textContent)"
            ):
                try:
                    inline.append({"url": "inline:script", "body": json.loads(blob)})
                except (json.JSONDecodeError, TypeError):
                    pass

            # Facebook can show its login form at the post's own URL instead of
            # redirecting, so look for a password field when the post isn't in the page.
            if platform == "facebook" and not any(
                    isinstance(d.get("node_v2"), dict) for r in inline for d in walk(r["body"])):
                if await page.locator('input[type="password"]').count():
                    error = "session_expired" if platform in session_platforms else "login_wall"
                    context.log.warning(f"Facebook showed a login form instead of {url} ({error}).")
                    if save_raw:
                        await Actor.set_value("debug_login_wall", await page.screenshot(), content_type="image/png")
                    await context.push_data({"platform": platform, "type": "error", "url": url, "error": error})
                    return

            meta = await parse_meta_tags(page)
            if platform == "instagram":
                parse = parse_instagram
            elif platform == "facebook":
                parse = parse_facebook
            else:
                context.log.warning(f"Unsupported URL: {url}")
                return

            pages = 0
            # Logged out only: with a session the page pages through comments itself,
            # so the account only ever sends requests the real page sends.
            if platform == "instagram" and "instagram" not in session_platforms:
                bucket = captured.setdefault(url, [])
                pages = await page_instagram_comments(
                    context, bucket, lambda: len(parse(inline + bucket, meta)[1]))

            # Load more comments: click a "load more" button if there is one, otherwise
            # scroll every scrollable panel to its bottom (logged-in Instagram loads more
            # as the comments side panel scrolls). Stop at maxComments, after
            # max_rounds rounds, on a login dialog, or when two rounds in a row
            # add nothing. Each round's background request is captured by the listener.
            def count_comments() -> int:
                return len(parse(inline + captured.get(url, []), meta)[1])

            # networkidle can fire while the logged-in app still shows its splash screen,
            # so wait until a comments panel or load-more control has been rendered.
            try:
                await page.wait_for_function(HAS_LOAD_MORE_TARGET_JS, timeout=20_000)
            except Exception:
                context.log.info("No comments panel or load-more control rendered within 20 s.")

            # Logged-in Facebook lists "Most relevant" comments by default, which hides
            # some. Switch the list to "All comments" before loading more.
            if platform == "facebook":
                try:
                    await page.locator('[role="button"]:has-text("Most relevant")').first.click(timeout=5_000)
                    await page.wait_for_timeout(random.randint(800, 1_500))
                    await page.locator('[role="menuitem"]:has-text("All comments")').first.click(timeout=5_000)
                    await page.wait_for_timeout(random.randint(2_000, 3_500))
                    context.log.info("Switched the comment list to 'All comments'.")
                except Exception:
                    context.log.info("No 'Most relevant' filter to switch; keeping the default comment list.")

            clicks = stale = 0
            while clicks < max_rounds and (have := count_comments()) < max_comments:
                more = has_more_comments(platform, inline + captured.get(url, []))
                if more is False:
                    context.log.info(f"All comments loaded: {platform} says there is no next page ({have} comments).")
                    break
                button = page.locator(LOAD_MORE_COMMENTS).first
                try:
                    await button.click(timeout=2_000)
                    action = "clicked load-more"
                except Exception:
                    # Re-find the panel each round: once it hits bottom, extra wheel
                    # input scrolls the whole page and moves the panel away.
                    if centre := await page.evaluate(FIND_SCROLL_PANEL_JS):
                        await page.mouse.move(*centre)
                        action = "scrolled panel"
                    else:
                        action = "scrolled page"
                        if clicks == 0:  # once per post: show what the page looked like
                            await Actor.set_value("debug_no_scroll_panel", await page.screenshot(),
                                                  content_type="image/png")
                    for _ in range(4):
                        await page.mouse.wheel(0, 600)
                        await page.wait_for_timeout(random.randint(150, 300))
                clicks += 1
                await page.wait_for_timeout(random.randint(1_500, 3_500))
                # Logged-out Instagram answers with a "View all comments" sign-up dialog
                # instead of loading more (seen Oct 2026), so stop rather than retry.
                # Facebook shows the post itself in a dialog, so only login prompts count.
                if gate := await page.evaluate(LOGIN_GATE_JS):
                    context.log.info(f"Round {clicks} ({action}): got a login prompt instead of more comments "
                                     f"({have} comments): {gate[:80]!r}")
                    break
                now = count_comments()
                context.log.info(f"Round {clicks} ({action}): {have} -> {now} comments")
                stale = stale + 1 if now == have else 0
                if stale >= (STALE_ROUNDS_WHEN_MORE if more else STALE_ROUNDS_OTHERWISE):
                    context.log.warning(f"Stopped after {stale} rounds with no new comments"
                                        + (" although Instagram says more exist (it may be slowing us down)."
                                           if more else "."))
                    break
            else:
                if clicks >= max_rounds:
                    context.log.info(f"Stopped at the round limit ({max_rounds}); raise maxLoadMoreRounds for more.")

            # Open reply threads: click each "View all N replies" link. Same pacing and
            # stop rules as above; capped at max_rounds clicks.
            reply_clicks = 0
            if include_replies:
                def count_replies() -> int:
                    return sum(1 for c in parse(inline + captured.get(url, []), meta)[1] if c.get("parentId"))

                stale = 0
                while reply_clicks < max_rounds:
                    link = page.get_by_text(VIEW_REPLIES).first
                    have = count_replies()
                    try:
                        await link.scroll_into_view_if_needed(timeout=2_000)
                        await link.click(timeout=2_000)
                    except Exception:
                        break  # no unopened reply threads left
                    reply_clicks += 1
                    await page.wait_for_timeout(random.randint(1_500, 3_500))
                    if gate := await page.evaluate(LOGIN_GATE_JS):
                        context.log.info(f"Reply click {reply_clicks}: got a login prompt, stopping: {gate[:80]!r}")
                        break
                    stale = stale + 1 if count_replies() == have else 0
                    if stale >= 2:
                        context.log.info("Reply clicks stopped: two clicks in a row added no replies.")
                        break
                context.log.info(f"Opened {reply_clicks} reply threads ({count_replies()} replies).")

            responses = inline + captured.pop(url, [])
            if save_raw:
                key = re.sub(r"[^a-zA-Z0-9_-]", "_", url)[-200:]
                await Actor.set_value(f"raw_{key}", responses)

            post, comments = parse(responses, meta)

            await context.push_data({"platform": platform, "type": "post", "url": url, **post})
            comments = nest_replies(comments, max_comments)
            for comment in comments:
                await context.push_data({"platform": platform, "type": "comment", "postUrl": url, **comment})

            n_replies = sum(1 for c in comments if c.get("parentId"))
            context.log.info(f"Saved post + {len(comments) - n_replies} comments + {n_replies} replies "
                             f"({len(responses)} JSON responses captured, {pages} comment pages fetched, {clicks} load-more rounds, {reply_clicks} reply clicks)")

        await crawler.run(start_urls)
