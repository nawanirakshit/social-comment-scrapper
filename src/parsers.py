"""Turn captured page data into clean records.

This is the part to learn on. Run the actor with saveRawResponses=true, open
the raw_* records in the run's Key-value store, find where the caption, counts
and comments live, and fill in the TODOs below.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterator


# ---------------------------------------------------------------- helpers

def walk(obj: Any) -> Iterator[dict]:
    """Yield every dict nested anywhere inside obj. Handy for hunting keys."""
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def parse_count(text: str | None) -> int | None:
    """'1,234' -> 1234, '12.5K' -> 12500, '3M' -> 3000000."""
    if not text:
        return None
    m = re.match(r"([\d.,]+)\s*([KkMm]?)", text.strip())
    if not m:
        return None
    number = float(m.group(1).replace(",", ""))
    return int(number * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1))


async def parse_meta_tags(page) -> dict:
    """Read Open Graph / meta tags; they exist even when JSON capture fails."""
    return await page.evaluate("""() => {
        const get = (sel) => document.querySelector(sel)?.getAttribute('content') || null;
        return {
            title: get('meta[property="og:title"]'),
            description: get('meta[property="og:description"]') || get('meta[name="description"]'),
            image: get('meta[property="og:image"]'),
        };
    }""")


# ---------------------------------------------------------------- Instagram
#
# Where the data lives (logged-out web, checked against raw_* records Oct 2026):
#
#   Post: an inline <script type="application/json"> blob in the page HTML, deep
#   inside Meta's preloader wrapper:
#     require[0][3][0].__bbox.require[0][3][1].__bbox.result.data.xig_polaris_media
#       .if_not_gated_logged_out.{like_count, comment_count, taken_at,
#                                 caption.text, user.username,
#                                 image_versions2.candidates[0].url, display_uri}
#   Comments page 1: same blob, xig_polaris_media.comments_connection.edges[].node
#   Comments page 2+: /api/graphql responses, data.xig_polaris_media.comments_connection
#   Comment node (__typename "XIGComment"): pk, text, created_at, comment_like_count,
#     child_comment_count, parent_comment_id, user.username
#
# Logged-in web (spare-account session) uses different containers, same field names:
#   Post: ...__bbox.result.data.xdt_api__v1__media__shortcode__web_info.items[0]
#   Comments: data.xdt_api__v1__media__media_id__comments__connection.edges[].node
#     (__typename "XDTCommentDict"), page 1 inline, later pages via /graphql.
#   Counts differ: logged-out JSON and meta tags served stale counts (685/687)
#   while the logged-in item said 763/753 at the same time.
#   Avoid xdt_api__v1__profile_timeline: it holds the author's other posts.
#   Replies (after clicking "View all N replies", PolarisPostChildCommentsQuery):
#     data.xdt_api__v1__media__media_id__comments__parent_comment_id__child_comments__connection
#     .edges[].node, same XDTCommentDict shape with parent_comment_id set.
#
# The wrapper paths above are fragile, so we search for these container keys
# anywhere instead of hard-coding the full path.

def parse_instagram(responses: list[dict], meta: dict) -> tuple[dict, list[dict]]:
    post: dict = {"author": None, "text": None, "likes": None, "commentsCount": None,
                  "timestamp": None, "imageUrl": meta.get("image")}
    comments: list[dict] = []

    # 1) Fallback from meta tags. Instagram's og:description reads like:
    #    '685 likes, 687 comments - username on September 27, 2026: "caption...". '
    desc = meta.get("description") or ""
    if m := re.search(r"([\d.,KkMm]+)\s+likes?", desc):
        post["likes"] = parse_count(m.group(1))
    if m := re.search(r"([\d.,KkMm]+)\s+comments?", desc):
        post["commentsCount"] = parse_count(m.group(1))
    if m := re.search(r"-\s*(\S+)\s+on\s+([^:]+):\s*(.*)", desc, re.S):
        post["author"] = m.group(1)
        post["timestamp"] = meta_date_to_iso(m.group(2).strip())
        post["text"] = re.sub(r'^"|"\.?\s*$', "", m.group(3).strip())

    # 2) Richer data from captured JSON. Only overwrite a field when JSON has a value.
    for node in (d for r in responses for d in walk(r.get("body"))):
        media = node.get("xig_polaris_media")  # logged out
        if isinstance(media, dict):
            # Logged-out pages wrap the post details in "if_not_gated_logged_out".
            details = media.get("if_not_gated_logged_out")
            if isinstance(details, dict) and "like_count" in details:
                fill_instagram_post(post, details)
            comments += instagram_comments(media.get("comments_connection"))

        if isinstance(info := node.get("xdt_api__v1__media__shortcode__web_info"), dict):  # logged in
            if isinstance(details := dig(info, "items", 0), dict):
                fill_instagram_post(post, details)
        comments += instagram_comments(node.get("xdt_api__v1__media__media_id__comments__connection"))
        comments += instagram_comments(
            node.get("xdt_api__v1__media__media_id__comments__parent_comment_id__child_comments__connection"))

    return post, dedupe(comments)


def fill_instagram_post(post: dict, details: dict) -> None:
    fill(post, "likes", details.get("like_count"))
    fill(post, "commentsCount", details.get("comment_count"))
    fill(post, "timestamp", unix_to_iso(details.get("taken_at")))
    fill(post, "author", dig(details, "user", "username"))
    fill(post, "text", dig(details, "caption", "text"))
    fill(post, "imageUrl", dig(details, "image_versions2", "candidates", 0, "url")
         or details.get("display_uri"))


def instagram_comments(connection: Any) -> list[dict]:
    out = []
    for edge in dig(connection, "edges") or []:
        if isinstance(c := dig(edge, "node"), dict) and c.get("pk"):
            out.append({
                "id": str(c["pk"]),
                "author": dig(c, "user", "username"),
                "text": c.get("text"),
                "likes": c.get("comment_like_count"),
                "replies": c.get("child_comment_count"),
                "parentId": c.get("parent_comment_id"),
                "timestamp": unix_to_iso(c.get("created_at")),
            })
    return out


INSTAGRAM_COMMENT_CONNECTIONS = ("xdt_api__v1__media__media_id__comments__connection",  # logged in
                                 "comments_connection")  # logged out, inside xig_polaris_media


def instagram_has_more_comments(responses: list[dict]) -> bool | None:
    """has_next_page of the most recent top-level comments page, or None if none seen."""
    latest = None
    for node in (d for r in responses for d in walk(r.get("body"))):
        for key in INSTAGRAM_COMMENT_CONNECTIONS:
            page_info = dig(node, key, "page_info")
            if isinstance(page_info, dict) and "has_next_page" in page_info:
                latest = bool(page_info["has_next_page"])
    return latest


def dig(obj: Any, *keys: str | int) -> Any:
    """Safe nested lookup: dig(d, "a", 0, "b") -> d["a"][0]["b"] or None."""
    for key in keys:
        if isinstance(key, int) and isinstance(obj, list) and -len(obj) <= key < len(obj):
            obj = obj[key]
        elif isinstance(key, str) and isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj


def fill(record: dict, field: str, value: Any) -> None:
    if value is not None:
        record[field] = value


def unix_to_iso(ts: Any) -> str | None:
    """1790578499 -> '2026-09-27T05:34:59Z'."""
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def meta_date_to_iso(text: str) -> str | None:
    """'September 27, 2026' -> '2026-09-27' (meta tags only give the date)."""
    try:
        return datetime.strptime(text, "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- Facebook

def parse_facebook(responses: list[dict], meta: dict) -> tuple[dict, list[dict]]:
    post: dict = {"author": meta.get("title"), "text": meta.get("description"),
                  "likes": None, "commentsCount": None, "shares": None,
                  "timestamp": None, "imageUrl": meta.get("image")}
    comments: list[dict] = []

    for node in (d for r in responses for d in walk(r["body"])):
        # Reaction / comment / share totals usually sit together in a "feedback" object.
        # TODO: verify against raw_* records; Facebook renames these often.
        if "reaction_count" in node and isinstance(node["reaction_count"], dict):
            post["likes"] = node["reaction_count"].get("count", post["likes"])
        if "comment_rendering_instance" in node:
            total = (node["comment_rendering_instance"] or {}).get("comments", {}).get("total_count")
            post["commentsCount"] = total or post["commentsCount"]
        if "share_count" in node and isinstance(node["share_count"], dict):
            post["shares"] = node["share_count"].get("count", post["shares"])

        # Comment nodes: __typename == "Comment" with a body and author.
        if node.get("__typename") == "Comment":
            comments.append({
                "id": node.get("id"),
                "author": (node.get("author") or {}).get("name"),
                "text": (node.get("body") or {}).get("text"),
                "likes": ((node.get("feedback") or {}).get("reactors") or {}).get("count"),
                "timestamp": node.get("created_time"),
            })

    return post, dedupe(comments)


def nest_replies(comments: list[dict], max_top_level: int) -> list[dict]:
    """Keep the first max_top_level top-level comments, each followed by its replies.

    maxComments limits top-level comments only; replies come on top of that. Replies
    whose parent isn't kept are dropped.
    """
    top = [c for c in comments if not c.get("parentId")][:max_top_level]
    replies: dict[str, list[dict]] = {}
    for c in comments:
        if c.get("parentId"):
            replies.setdefault(str(c["parentId"]), []).append(c)
    out = []
    for c in top:
        out.append(c)
        out += sorted(replies.get(str(c.get("id")), []), key=lambda r: r.get("timestamp") or "")
    return out


def dedupe(items: list[dict]) -> list[dict]:
    seen, out = set(), []
    for item in items:
        key = item.get("id") or (item.get("author"), item.get("text"))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out
