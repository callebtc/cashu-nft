"""Cloudflare Turnstile for image uploads.

The browser runs an invisible widget and sends its token with each upload in
the ``X-Turnstile-Token`` header. Tokens are single-use and expire after five
minutes; Cloudflare's siteverify API checks them.
"""

from typing import List, Optional

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TOKEN_HEADER = "X-Turnstile-Token"
MAX_TOKEN_LENGTH = 2048

FAILED_MESSAGE = "We couldn't confirm this upload came from a browser. Reload the page and try again."
UNAVAILABLE_MESSAGE = "We couldn't check this upload right now. Try again in a moment."


class SiteverifyResult(BaseModel):
    success: bool
    error_codes: List[str] = Field(default_factory=list, alias="error-codes")


class Turnstile:
    def __init__(self, sitekey: str, secret: str):
        self.sitekey = sitekey
        self.secret = secret
        self.client = httpx.AsyncClient(timeout=10)

    async def verify(self, request: Request) -> None:
        token = request.headers.get(TOKEN_HEADER, "")
        if not token or len(token) > MAX_TOKEN_LENGTH:
            raise HTTPException(403, FAILED_MESSAGE)
        form = {"secret": self.secret, "response": token}
        if request.client:
            form["remoteip"] = request.client.host
        try:
            response = await self.client.post(SITEVERIFY_URL, data=form)
            result = SiteverifyResult.model_validate_json(response.content)
        except (httpx.HTTPError, ValidationError):
            raise HTTPException(503, UNAVAILABLE_MESSAGE)
        if not result.success:
            raise HTTPException(403, FAILED_MESSAGE)

    async def aclose(self) -> None:
        await self.client.aclose()


def configure_turnstile(
    sitekey: Optional[str], secret: Optional[str]
) -> Optional[Turnstile]:
    """A Turnstile check when both keys are set; neither disables it."""
    if bool(sitekey) != bool(secret):
        raise ValueError("Set both the Turnstile sitekey and secret, or neither.")
    return Turnstile(sitekey, secret) if sitekey and secret else None
