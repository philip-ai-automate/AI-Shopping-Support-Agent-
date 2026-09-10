"""
buffer_client.py — thin wrapper around Buffer's public GraphQL API
(https://developers.buffer.com/) for the Social Media Posts admin module.

Buffer's current API (2026) is GraphQL-only, single endpoint, authenticated
with a personal API key from the PhiXtra Buffer account (generated at
publish.buffer.com/settings/api, no OAuth needed for this server-to-server
use case). All Buffer HTTP calls live in this one file — if Buffer's schema
changes, only this module needs updating.

Important: Buffer does NOT accept direct file uploads for post media — it
fetches the image from a public URL you provide when the post publishes.
Callers must pass a URL that is reachable without authentication (see
`social_media_post_public_image` in portal_admin_routes.py).
"""
import os
import requests

BUFFER_API_URL = "https://api.buffer.com"


class BufferAPIError(Exception):
    """Raised for any GraphQL error or malformed response from Buffer."""
    pass


def _buffer_graphql(query: str, variables: dict = None) -> dict:
    api_key = os.environ.get("BUFFER_API_KEY", "")
    if not api_key:
        raise BufferAPIError("BUFFER_API_KEY is not configured.")

    resp = requests.post(
        BUFFER_API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=20,
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("errors"):
        raise BufferAPIError(payload["errors"][0].get("message", "Unknown Buffer API error"))
    return payload.get("data") or {}


def buffer_list_channels() -> list:
    """Returns every channel connected to this Buffer account, e.g.
    [{"id": "...", "service": "facebook", "name": "PhiXtra"}, ...] — used to
    populate the Buffer Channels mapping settings page so the owner picks
    from real channels instead of typing raw IDs."""
    query = "query { channels { id service name } }"
    data = _buffer_graphql(query)
    return data.get("channels") or []


def buffer_create_post(channel_id: str, caption: str, image_url: str, scheduled_for_iso: str = None):
    """Creates one post on one Buffer channel.

    scheduled_for_iso=None -> publishes immediately (Buffer's "Share Now").
    scheduled_for_iso="2026-09-10T15:00:00.000Z" -> scheduled for that exact
    UTC time.

    Returns (buffer_post_id, status).
    """
    mode = "customScheduled" if scheduled_for_iso else "shareNow"
    post_input = {
        "channelId": channel_id,
        "text": caption,
        "mode": mode,
        "schedulingType": "automatic",  # Buffer auto-publishes; never a manual reminder
        "assets": [{"image": {"url": image_url}}],
    }
    if scheduled_for_iso:
        post_input["dueAt"] = scheduled_for_iso

    query = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id status dueAt } }
        ... on MutationError { message }
      }
    }
    """
    data = _buffer_graphql(query, {"input": post_input})
    result = data.get("createPost") or {}
    if result.get("message"):
        raise BufferAPIError(result["message"])
    post = result.get("post") or {}
    if not post.get("id"):
        raise BufferAPIError("Buffer did not return a post id.")
    return post["id"], post.get("status")


def buffer_get_post_status(buffer_post_id: str) -> str:
    """Returns Buffer's status for one post: 'buffer' | 'sent' | 'failed' |
    'draft' | 'approval'."""
    query = "query GetPost($id: PostId!) { post(id: $id) { id status } }"
    data = _buffer_graphql(query, {"id": buffer_post_id})
    post = data.get("post") or {}
    return post.get("status")
