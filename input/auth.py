"""
Token-based authentication for Cloudflare Tunnel access.
Localhost requests bypass auth automatically.
"""

import os
import secrets

from fastapi import HTTPException, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.requests import Request

# auto_error=False so we can handle missing credentials ourselves
# (localhost requests won't have an Authorization header)
security = HTTPBearer(auto_error=False)

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "")


def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Security(security),
):
    """
    FastAPI dependency that enforces bearer-token auth.

    - Localhost / direct access  → auto-bypass (no token needed)
    - Cloudflare Tunnel traffic  → token required

    Cloudflare's `cloudflared` proxy sets the `CF-Connecting-IP` header
    on every tunnelled request.  If that header is absent AND the TCP
    peer is loopback, the request is coming from the local machine.
    """
    # Detect whether the request arrived via Cloudflare Tunnel
    is_tunnel = request.headers.get("cf-connecting-ip") is not None

    if not is_tunnel:
        client_host = request.client.host if request.client else ""
        if client_host in ("127.0.0.1", "::1"):
            return True  # localhost bypass

    # --- Remote / tunnel traffic: require a valid bearer token ---
    if not credentials:
        raise HTTPException(status_code=403, detail="Not authorized")

    if not ACCESS_TOKEN:
        raise HTTPException(
            status_code=500, detail="ACCESS_TOKEN not configured on server"
        )

    if not secrets.compare_digest(credentials.credentials, ACCESS_TOKEN):
        raise HTTPException(status_code=403, detail="Not authorized")

    return True
