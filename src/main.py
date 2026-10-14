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

from .parsers import (dig, instagram_has_more_comments, nest_replies, parse_facebook, parse_instagram,
                      parse_meta_tags)

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

        # Optional logged-in session (spare account), made by scripts/save_session.py.
        session_state: dict | str | None = None
        if actor_input.get("sessionState"):
            session_state = json.loads(actor_input["sessionState"])
        elif path := actor_input.get("sessionStatePath"):
            if not Path(path).is_file():
                await Actor.fail(status_message=f"sessionStatePath not found: {path}")
                return
            session_state = path
        if session_state:
            Actor.log.info("Using a saved logged-in session.")

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
            headless=False,
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

            # Let lazy content and background requests finish.
            await page.wait_for_load_state("networkidle", timeout=30_000)

            # Detect login walls early so you know why data is missing. With a session,
            # a redirect means it expired or Instagram wants the account verified: stop
            # and refresh it by hand with scripts/save_session.py, never automatically.
            if any(part in page.url for part in ("/accounts/login", "/login", "/challenge", "/accounts/suspended")):
                error = "session_expired" if session_state else "login_wall"
                context.log.warning(f"Redirected to {page.url} for {url} ({error}).")
                await context.push_data({"platform": platform, "type": "error", "url": url, "error": error})
                return

            # Close cookie / login popups if they appear (best effort).
            for selector in ['button:has-text("Decline optional cookies")',
                             'button:has-text("Allow all cookies")',
                             'div[role="dialog"] [aria-label="Close"]']:
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
            if platform == "instagram" and not session_state:
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

            clicks = stale = 0
            while clicks < max_rounds and (have := count_comments()) < max_comments:
                more = instagram_has_more_comments(inline + captured.get(url, [])) if platform == "instagram" else None
                if more is False:
                    context.log.info(f"All comments loaded: Instagram says there is no next page ({have} comments).")
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
                if dialog := await page.locator('div[role="dialog"]').all_inner_texts():
                    context.log.info(f"Round {clicks} ({action}): got a dialog instead of more comments "
                                     f"({have} comments): {dialog[0][:80]!r}")
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
                    if dialog := await page.locator('div[role="dialog"]').all_inner_texts():
                        context.log.info(f"Reply click {reply_clicks}: got a dialog, stopping: {dialog[0][:80]!r}")
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
