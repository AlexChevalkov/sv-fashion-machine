"""
Helper to push a DRAFT post to Buffer via the GraphQL API.

Validated live 2026-07-01:
    endpoint = https://api.buffer.com/graphql   (NOT graphql.buffer.com,
               NOT the classic REST api.bufferapp.com)
    auth     = Authorization: Bearer <BUFFER_ACCESS_TOKEN>
    mutation = createPost(input: CreatePostInput!)  with saveToDraft: true

Additive and safe:
- Never fails to import (requests imported lazily inside the call).
- Guard with buffer_is_configured(); callers must treat a Buffer failure as
  non-fatal so the rest of the pipeline keeps working.

Required environment variables (GitHub Actions secrets / env):
    BUFFER_ACCESS_TOKEN   token from https://publish.buffer.com/settings/api
    BUFFER_CHANNEL_ID     target channel id (defaults to the IG channel)
"""

import os


BUFFER_ACCESS_TOKEN = os.environ.get("BUFFER_ACCESS_TOKEN")
# Instagram channel "sv_fashionacademy" in org "SV Fashion Media".
BUFFER_CHANNEL_ID = os.environ.get("BUFFER_CHANNEL_ID", "6a2481eac687a22dd46ad06c")
BUFFER_ENDPOINT = "https://api.buffer.com/graphql"


_CREATE_DRAFT_MUTATION = """
mutation CreateDraft($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status } }
    ... on InvalidInputError { message }
    ... on UnauthorizedError { message }
    ... on NotFoundError { message }
    ... on UnexpectedError { message }
    ... on RestProxyError { message code }
    ... on LimitReachedError { message }
  }
}
"""


def buffer_is_configured() -> bool:
    """True only when a Buffer token and target channel are present."""
    return bool(BUFFER_ACCESS_TOKEN and BUFFER_CHANNEL_ID)


def create_instagram_draft(
    caption: str,
    image_urls=None,
    video_url: str = "",
    video_thumbnail_url: str = "",
    alt_text: str = "SV Fashion Media",
):
    """
    Create an Instagram DRAFT in Buffer.

    - Reel:               pass video_url (a PUBLIC direct URL, e.g. an R2 link).
    - Carousel / single:  pass image_urls (list of PUBLIC direct URLs).

    Media URLs must be publicly reachable (Buffer does a HEAD check) — R2
    public URLs work; redirect-based hosts (e.g. picsum.photos) do not.

    Returns a tuple (ok: bool, info: str). Never raises.
    """
    import requests

    if not buffer_is_configured():
        return False, "Buffer not configured (missing BUFFER_ACCESS_TOKEN)."

    image_urls = [u for u in (image_urls or []) if u]

    if video_url:
        video_asset = {"url": video_url}
        if video_thumbnail_url:
            video_asset["thumbnailUrl"] = video_thumbnail_url
        assets = [{"video": video_asset}]
        instagram_type = "reel"
    else:
        assets = [
            {"image": {"url": url, "metadata": {"altText": alt_text}}}
            for url in image_urls
        ]
        instagram_type = "post"

    if not assets:
        return False, "No media to publish (no video_url and no image_urls)."

    variables = {
        "input": {
            "channelId": BUFFER_CHANNEL_ID,
            "schedulingType": "notification",
            "mode": "addToQueue",
            "saveToDraft": True,
            "text": caption or "",
            "assets": assets,
            "metadata": {"instagram": {"type": instagram_type, "shouldShareToFeed": True}},
        }
    }

    try:
        resp = requests.post(
            BUFFER_ENDPOINT,
            json={"query": _CREATE_DRAFT_MUTATION, "variables": variables},
            headers={
                "Authorization": "Bearer " + BUFFER_ACCESS_TOKEN,
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        data = resp.json()
    except Exception as exc:
        return False, f"Buffer request failed: {exc!r}"

    result = (data.get("data") or {}).get("createPost") or {}
    typename = result.get("__typename")

    if typename == "PostActionSuccess":
        post = result.get("post") or {}
        return True, f"Buffer draft created (id={post.get('id')}, status={post.get('status')})."

    return False, f"Buffer error ({typename}): {result.get('message')} | raw={str(data)[:500]}"
