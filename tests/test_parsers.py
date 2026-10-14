"""Parser tests against trimmed copies of real captured responses.

Fixtures come from a logged-out run on https://www.instagram.com/p/Dd0lsFvo4c0/
(Oct 2026), trimmed to a few comments. Commenter usernames, @mentions and account
IDs are replaced with pseudonyms (user_01, ...); swissmilitaryindia is the post owner.

- instagram_post_inline.json: the inline <script type="application/json"> blob
  holding the post, comments page 1, and two of the author's other posts (decoys).
- instagram_comments_page2.json: the /api/graphql response with comments page 2.
- instagram_noise_responses.json: an "experiments" response and an
  "Unauthorized logged out query." error, which must be ignored.
"""

import json
from pathlib import Path

import pytest

from src.parsers import dig, instagram_has_more_comments, nest_replies, parse_count, parse_instagram, unix_to_iso

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def all_responses() -> list[dict]:
    return (load("instagram_post_inline.json")
            + load("instagram_comments_page2.json")
            + load("instagram_noise_responses.json"))


def test_post_fields_from_inline_json(all_responses):
    post, _ = parse_instagram(all_responses, {})
    assert post["author"] == "swissmilitaryindia"
    assert post["likes"] == 685
    assert post["commentsCount"] == 687
    assert post["timestamp"] == "2026-09-28T06:54:59Z"
    assert post["text"].startswith("It’s time for a Giveaway Alert!")
    assert post["text"].endswith("Travel Giveaway | Giveaway")
    assert post["imageUrl"].startswith("https://")


def test_json_overrides_meta_fallback(all_responses):
    meta = {"description": '1 likes, 2 comments - someone on May 1, 2020: "old"', "image": "meta.jpg"}
    post, _ = parse_instagram(all_responses, meta)
    assert (post["likes"], post["commentsCount"], post["author"]) == (685, 687, "swissmilitaryindia")
    assert post["imageUrl"] != "meta.jpg"


def test_comments_from_both_pages_in_order(all_responses):
    _, comments = parse_instagram(all_responses, {})
    assert len(comments) == 6  # 3 inline (page 1) + 3 graphql (page 2), as trimmed
    assert [c["id"] for c in comments[:3]] == ["18152951770490418", "18152512774545957", "17895816513421099"]
    assert comments[3] == {
        "id": "18568984945072704",
        "author": "user_01",
        "text": "46 wheels,@user_02",
        "likes": 0,
        "replies": None,
        "parentId": None,
        "timestamp": "2026-10-07T09:27:29Z",
    }


def test_duplicate_comments_are_dropped():
    page2 = load("instagram_comments_page2.json")
    _, comments = parse_instagram(page2 + page2, {})
    assert len(comments) == 3


def test_meta_fallback_only():
    meta = {
        "description": '685 likes, 687 comments - swissmilitaryindia on September 27, 2026: '
                       '"It’s time for a Giveaway Alert!\nLine two". ',
        "image": "https://example.invalid/og.jpg",
    }
    post, comments = parse_instagram(load("instagram_noise_responses.json"), meta)
    assert post == {
        "author": "swissmilitaryindia",
        "text": "It’s time for a Giveaway Alert!\nLine two",
        "likes": 685,
        "commentsCount": 687,
        "timestamp": "2026-09-27",
        "imageUrl": "https://example.invalid/og.jpg",
    }
    assert comments == []


@pytest.mark.parametrize("responses", [
    [],
    [{"url": "x", "body": None}],
    [{"url": "x"}],
    [{"url": "x", "body": {"data": {"xig_polaris_media": None}}}],
    [{"url": "x", "body": {"data": {"xig_polaris_media": {"if_not_gated_logged_out": {"like_count": None}}}}}],
    [{"url": "x", "body": {"data": {"xig_polaris_media": {"comments_connection": {"edges": [None, {"node": {}}]}}}}}],
])
def test_missing_or_malformed_data_gives_none(responses):
    post, comments = parse_instagram(responses, {})
    assert set(post.values()) == {None}
    assert comments == []


def test_helpers():
    assert dig({"a": [{"b": 1}]}, "a", 0, "b") == 1
    assert dig({"a": []}, "a", 0, "b") is None
    assert dig(None, "a") is None
    assert unix_to_iso(0) == "1970-01-01T00:00:00Z"
    assert unix_to_iso("123") is None
    assert parse_count("12.5K") == 12_500
    assert parse_count("1,234") == 1234
    assert parse_count(None) is None


# ---------------------------------------------------------------- logged in
#
# instagram_logged_in_responses.json: from a run with a spare-account session.
# [0] inline post blob (xdt_api__v1__media__shortcode__web_info), [1] inline comments
# page 1, [2] the author's profile timeline (decoy posts with their own counts),
# [3] /api/graphql comments page 2. Trimmed to 3 comments per page; no viewer data.

@pytest.fixture
def logged_in() -> list[dict]:
    return load("instagram_logged_in_responses.json")


def test_logged_in_post_fields(logged_in):
    post, _ = parse_instagram(logged_in, {"description": "685 likes, 687 comments - x on May 1, 2020: \"old\""})
    # Logged-in counts are live; the meta tag still shows the stale logged-out ones.
    assert (post["likes"], post["commentsCount"]) == (763, 753)
    assert post["author"] == "swissmilitaryindia"
    assert post["timestamp"] == "2026-09-28T06:54:59Z"
    assert post["text"].startswith("It’s time for a Giveaway Alert!")


def test_logged_in_ignores_profile_timeline(logged_in):
    post, _ = parse_instagram([logged_in[2]], {})
    assert set(post.values()) == {None}


def test_logged_in_comments_both_pages(logged_in):
    _, comments = parse_instagram(logged_in, {})
    assert [c["id"] for c in comments] == [
        "18117581158933642", "18197147914386037", "18631209157047551",  # inline page 1
        "18347519911254048", "18602020210064686", "17912140059283390",  # graphql page 2
    ]
    assert comments[3] == {
        "id": "18347519911254048",
        "author": "user_26",
        "text": comments[3]["text"],
        "likes": 3,
        "replies": 0,
        "parentId": None,
        "timestamp": "2026-09-29T05:29:59Z",
    }
    assert comments[3]["text"].startswith("38 wheels spotted")


# ---------------------------------------------------------------- replies
#
# instagram_replies_response.json: the PolarisPostChildCommentsQuery reply after
# clicking "View all 2 replies" under comment 18197147914386037 (in the logged-in
# fixture above).

def test_replies_parsed_with_parent(logged_in):
    _, comments = parse_instagram(logged_in + load("instagram_replies_response.json"), {})
    replies = [c for c in comments if c["parentId"]]
    assert len(replies) == 2
    assert {r["parentId"] for r in replies} == {"18197147914386037"}
    assert replies[0]["author"] and replies[0]["text"] and replies[0]["timestamp"].endswith("Z")


def test_nest_replies_puts_replies_under_parent_and_limits_top_level():
    comments = [
        {"id": "1", "parentId": None, "timestamp": "2026-01-01T00:00:00Z"},
        {"id": "2", "parentId": None, "timestamp": "2026-01-02T00:00:00Z"},
        {"id": "3", "parentId": None, "timestamp": "2026-01-03T00:00:00Z"},
        {"id": "r2b", "parentId": "2", "timestamp": "2026-01-05T00:00:00Z"},
        {"id": "r2a", "parentId": "2", "timestamp": "2026-01-04T00:00:00Z"},
        {"id": "r3", "parentId": "3", "timestamp": "2026-01-06T00:00:00Z"},
    ]
    assert [c["id"] for c in nest_replies(comments, 2)] == ["1", "2", "r2a", "r2b"]
    assert [c["id"] for c in nest_replies(comments, 10)] == ["1", "2", "r2a", "r2b", "3", "r3"]


def test_has_more_comments_uses_latest_page(logged_in):
    assert instagram_has_more_comments(logged_in) is True
    last_page = {"url": "x", "body": {"data": {"xdt_api__v1__media__media_id__comments__connection": {
        "edges": [], "page_info": {"has_next_page": False, "end_cursor": None}}}}}
    assert instagram_has_more_comments(logged_in + [last_page]) is False
    assert instagram_has_more_comments(load("instagram_noise_responses.json")) is None
    # Reply threads have their own page_info; they must not count as the comment list.
    assert instagram_has_more_comments(logged_in + load("instagram_replies_response.json")) is True
