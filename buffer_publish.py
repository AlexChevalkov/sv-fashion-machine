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
- Every media URL is fetched once before the mutation, and the mutation is
  retried, because Buffer pulls the media itself and a cold R2 object loses
  that race. See warm_media_url().

Required environment variables (GitHub Actions secrets / env):
    BUFFER_ACCESS_TOKEN   token from https://publish.buffer.com/settings/api
    BUFFER_CHANNEL_ID     target channel id (defaults to the IG channel)
"""

import os
import time


BUFFER_ACCESS_TOKEN = os.environ.get("BUFFER_ACCESS_TOKEN")
# Instagram channel "sv_fashionacademy" in org "SV Fashion Media".
BUFFER_CHANNEL_ID = os.environ.get("BUFFER_CHANNEL_ID", "6a2481eac687a22dd46ad06c")
BUFFER_ENDPOINT = "https://api.buffer.com/graphql"

# How many times the createPost mutation is retried, and how many times each
# media URL is fetched before we give up on it. See warm_media_url() for why
# the warm-up exists at all.
BUFFER_MAX_ATTEMPTS = int(os.environ.get("BUFFER_MAX_ATTEMPTS", "3"))
MEDIA_WARM_ATTEMPTS = int(os.environ.get("BUFFER_MEDIA_WARM_ATTEMPTS", "4"))


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


def warm_media_url(url: str, attempts: int = 0):
    """
    Fetch a media URL ourselves until it answers 200 with a body.

    Buffer does not accept uploaded bytes — it fetches every asset URL itself,
    and for a carousel it fetches all of them at once. Our R2 public host
    (pub-*.r2.dev) serves a freshly written object from cold, and under that
    burst some of Buffer's fetches come back empty. Buffer then either rejects
    the whole post with "Image could not be read from its URL" or silently
    drops the asset it could not read — both happened live on 2026-08-08
    (the mfpen carousel was rejected outright; the Korea carousel lost
    slide_02 and was published with 6 slides instead of 7).

    Fetching each URL first puts the object in Cloudflare's edge cache, so
    Buffer's own fetch is a cache hit. Verified live 2026-08-09: the exact
    payload Buffer had rejected went through unchanged once every slide had
    been fetched once.

    Returns (ok: bool, info: str). Never raises.
    """
    import requests

    attempts = attempts or MEDIA_WARM_ATTEMPTS
    detail = "not attempted"

    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, timeout=120)
            size = len(response.content or b"")
            if response.status_code == 200 and size > 0:
                return True, f"{size} bytes"
            detail = f"HTTP {response.status_code}, {size} bytes"
        except Exception as exc:
            detail = repr(exc)

        print(f"Warm-up {attempt}/{attempts} failed for {url}: {detail}")
        if attempt < attempts:
            time.sleep(2 ** attempt)

    return False, detail


def warm_media_urls(urls) -> tuple:
    """
    Warm every media URL. Returns (ok: bool, info: str).

    A URL that stays unreadable is a hard stop: sending it to Buffer would
    either fail the post or, worse, produce a carousel quietly missing a
    slide. Better to keep the job at "Ready for Buffer" with a precise reason.
    """
    failed = []

    for url in urls:
        ok, detail = warm_media_url(url)
        if ok:
            print(f"Media ready for Buffer ({detail}): {url}")
        else:
            failed.append(f"{url} -> {detail}")

    if failed:
        return False, "Media not readable after warm-up: " + "; ".join(failed)
    return True, f"{len(urls)} media URL(s) warmed."


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

    Media URLs must be publicly reachable (Buffer fetches them itself) — R2
    public URLs work; redirect-based hosts (e.g. picsum.photos) do not. Each
    URL is warmed first; if one stays unreadable, nothing is sent, so a
    carousel can never reach Buffer with a slide missing.

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

    media_urls = [video_url] if video_url else list(image_urls)
    warm_ok, warm_info = warm_media_urls(media_urls)
    if not warm_ok:
        return False, warm_info
    print(warm_info)

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

    # Retry the mutation itself: a media fetch on Buffer's side can still time
    # out even on a warm object, and that surfaces as a plain InvalidInputError
    # rather than anything retry-worthy-looking. Re-warming between attempts
    # costs one cheap cached GET per asset.
    last_error = "not attempted"

    for attempt in range(1, BUFFER_MAX_ATTEMPTS + 1):
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
            last_error = f"Buffer request failed: {exc!r}"
        else:
            result = (data.get("data") or {}).get("createPost") or {}
            typename = result.get("__typename")

            if typename == "PostActionSuccess":
                post = result.get("post") or {}
                return True, (
                    f"Buffer draft created (id={post.get('id')}, "
                    f"status={post.get('status')}, assets={len(assets)}, "
                    f"attempt={attempt}/{BUFFER_MAX_ATTEMPTS})."
                )

            last_error = (
                f"Buffer error ({typename}): {result.get('message')} "
                f"| raw={str(data)[:500]}"
            )

        print(f"Buffer createPost attempt {attempt}/{BUFFER_MAX_ATTEMPTS}: {last_error}")

        if attempt < BUFFER_MAX_ATTEMPTS:
            time.sleep(2 ** attempt)
            warm_media_urls(media_urls)

    return False, f"{last_error} | after {BUFFER_MAX_ATTEMPTS} attempt(s)"
