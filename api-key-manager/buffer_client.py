"""
buffer_client.py — thin wrapper around Buffer's public GraphQL API
(https://developers.buffer.com/). Pure HTTP, no database: every call takes
the API key it should use, because each business connects its OWN Buffer
account (entered on Integration › Buffer) and PhiXtra admin has a separate
one of its own (see buffer_accounts.py for where keys are stored).

Buffer's API (2026) is GraphQL-only, one endpoint, Bearer auth with a
personal API key (generated at publish.buffer.com/settings/api). It always
answers HTTP 200 — success or failure is read from the body: `errors[]`
for bad keys / rate limits, a typed error inside `data` for a rejected post.

Rewritten 2026-09-25 against Buffer's current docs: channels are listed per
organization (the 2026-09-05 version asked for them without one), a single
post is fetched with post(input: {id}), and Instagram/Facebook posts carry
the per-network `metadata` block Buffer requires for them.

Important: Buffer does NOT accept file uploads. It fetches the image from a
public https URL when the post goes out, so callers pass a URL that works
without a login and stays up until the post publishes.
"""
import requests

BUFFER_API_URL = "https://api.buffer.com"

# Buffer error codes meaning "this key no longer works" — the caller marks
# the connection as needing attention instead of treating it as a one-off.
AUTH_ERROR_CODES = {"UNAUTHORIZED", "UNAUTHENTICATED", "FORBIDDEN"}


class BufferAPIError(Exception):
    """Any error from Buffer. `code` is Buffer's error code when it sent
    one (e.g. UNAUTHORIZED, RATE_LIMIT_EXCEEDED), else None."""
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code

    @property
    def is_auth_error(self) -> bool:
        return self.code in AUTH_ERROR_CODES


def _buffer_graphql(api_key: str, query: str, variables: dict = None) -> dict:
    if not api_key:
        raise BufferAPIError("No Buffer key saved.", code="UNAUTHORIZED")
    try:
        resp = requests.post(
            BUFFER_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"query": query, "variables": variables or {}},
            timeout=20,
        )
    except requests.RequestException as e:
        raise BufferAPIError(f"Could not reach Buffer ({e.__class__.__name__}). Try again in a minute.")
    try:
        payload = resp.json()
    except ValueError:
        raise BufferAPIError(f"Buffer sent an unexpected reply (HTTP {resp.status_code}). Try again in a minute.")
    if payload.get("errors"):
        err = payload["errors"][0]
        code = (err.get("extensions") or {}).get("code")
        raise BufferAPIError(err.get("message", "Unknown Buffer error"), code=code)
    if resp.status_code != 200:
        raise BufferAPIError(f"Buffer sent an unexpected reply (HTTP {resp.status_code}).")
    return payload.get("data") or {}


def buffer_get_organizations(api_key: str) -> list:
    """[{"id", "name"}, ...] for the account this key belongs to. Also the
    cheapest way to prove a key works."""
    query = "query GetOrganizations { account { organizations { id name } } }"
    data = _buffer_graphql(api_key, query)
    return ((data.get("account") or {}).get("organizations")) or []


def buffer_list_channels(api_key: str, organization_id: str) -> list:
    """Every social account connected inside one Buffer organization, e.g.
    [{"id", "name", "displayName", "service": "instagram", "avatar",
      "isDisconnected", "isLocked"}, ...]."""
    query = """
    query GetChannels($organizationId: OrganizationId!) {
      channels(input: { organizationId: $organizationId }) {
        id name displayName service avatar isDisconnected isLocked
      }
    }
    """
    data = _buffer_graphql(api_key, query, {"organizationId": organization_id})
    return data.get("channels") or []


def _metadata_for(service: str, has_video: bool = False):
    """Buffer requires a post type for Instagram and Facebook. Images go out
    as ordinary feed posts; an Instagram video goes out as a Reel (also
    shared to the feed), a Facebook video as a normal video post."""
    if service == "instagram":
        return {"instagram": {"type": "reel" if has_video else "post", "shouldShareToFeed": True}}
    if service == "facebook":
        return {"facebook": {"type": "post"}}
    return None


def buffer_create_post(api_key: str, channel_id: str, caption: str, image_url: str = None,
                       scheduled_for_iso: str = None, service: str = None, assets: list = None):
    """Creates one post on one Buffer channel.

    scheduled_for_iso=None  -> published now (Buffer "shareNow").
    scheduled_for_iso="2026-09-26T09:00:00.000Z" -> scheduled for that UTC time.

    assets: a full Buffer assets list (several images, or one video
    {"video": {"url": …}}) — used instead of image_url when given.

    Returns (buffer_post_id, status).
    """
    post_input = {
        "channelId": channel_id,
        "text": caption,
        "mode": "customScheduled" if scheduled_for_iso else "shareNow",
        "schedulingType": "automatic",  # Buffer publishes it; never a phone reminder
    }
    if assets:
        post_input["assets"] = assets
    elif image_url:
        post_input["assets"] = [{"image": {"url": image_url}}]
    if scheduled_for_iso:
        post_input["dueAt"] = scheduled_for_iso
    meta = _metadata_for(service or "", has_video=any("video" in a for a in (assets or [])))
    if meta:
        post_input["metadata"] = meta

    query = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        ... on PostActionSuccess { post { id status dueAt } }
        ... on MutationError { message }
      }
    }
    """
    data = _buffer_graphql(api_key, query, {"input": post_input})
    result = data.get("createPost") or {}
    if result.get("message"):
        raise BufferAPIError(result["message"])
    post = result.get("post") or {}
    if not post.get("id"):
        raise BufferAPIError("Buffer did not return a post id.")
    return post["id"], post.get("status")


def buffer_get_post(api_key: str, buffer_post_id: str) -> dict:
    """{"id", "status", "dueAt", "sentAt", "externalLink", "error": {"message"}}.
    status is one of draft | buffer | scheduled | publishing | sent | failed | error."""
    query = """
    query GetPost($input: PostInput!) {
      post(input: $input) { id status dueAt sentAt externalLink error { message } }
    }
    """
    data = _buffer_graphql(api_key, query, {"input": {"id": buffer_post_id}})
    return data.get("post") or {}


def buffer_get_post_status(api_key: str, buffer_post_id: str) -> str:
    return buffer_get_post(api_key, buffer_post_id).get("status")


def buffer_delete_post(api_key: str, buffer_post_id: str) -> None:
    """Removes a post from Buffer (used to cancel or replace a scheduled one)."""
    query = """
    mutation DeletePost($input: DeletePostInput!) {
      deletePost(input: $input) { __typename }
    }
    """
    _buffer_graphql(api_key, query, {"input": {"id": buffer_post_id}})
