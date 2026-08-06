"""Delegated Microsoft Graph mail sender for the ADO task-monitor MCP.

Sends as the signed-in user (you) via Graph ``/me/sendMail`` using a
*delegated* token from the Windows WAM broker -- the same pattern the ADF MCP
uses (azure-identity-broker, console-window handle, default broker account).
No PAT, secret, or hand-pasted token.

Env:
  TENANT_ID          your Entra tenant id (defaults to "organizations",
                     which works for any work/school account)
  GRAPH_CLIENT_ID    public-client app id for the broker sign-in. Defaults to
                     the Azure CLI first-party client. If Mail.Send consent is
                     blocked for that client, register a public client with
                     delegated Mail.Send + User.Read and set this to its id.
"""
from __future__ import annotations

import os
from typing import Optional

import requests

GRAPH = "https://graph.microsoft.com"
DEFAULT_TENANT = "organizations"  # override with TENANT_ID for a specific tenant
AZURE_CLI_CLIENT = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
SCOPES = [f"{GRAPH}/Mail.Send", f"{GRAPH}/User.Read"]

_token: Optional[str] = None


def _acquire_token() -> str:
    """Delegated Graph token via WAM broker (cached in-process)."""
    global _token
    if _token:
        return _token
    import ctypes
    from azure.identity.broker import InteractiveBrowserBrokerCredential

    try:
        handle = ctypes.windll.kernel32.GetConsoleWindow()
    except Exception:
        handle = 0
    cred = InteractiveBrowserBrokerCredential(
        tenant_id=os.environ.get("TENANT_ID", DEFAULT_TENANT),
        client_id=os.environ.get("GRAPH_CLIENT_ID", AZURE_CLI_CLIENT),
        parent_window_handle=handle or 0,
        use_default_broker_account=True,
    )
    _token = cred.get_token(*SCOPES).token
    return _token


def whoami() -> dict:
    """Return the signed-in user's profile (verifies delegated auth works)."""
    token = _acquire_token()
    r = requests.get(f"{GRAPH}/v1.0/me", timeout=30,
                     headers={"Authorization": f"Bearer {token}"})
    r.raise_for_status()
    return r.json()


def send_mail(to: list[str], subject: str, html: str,
              cc: Optional[list[str]] = None,
              save_to_sent: bool = True) -> None:
    """Send an HTML mail as the signed-in user. Raises on non-202."""
    token = _acquire_token()
    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html},
            "toRecipients": [{"emailAddress": {"address": a}} for a in to],
            "ccRecipients": [{"emailAddress": {"address": a}} for a in (cc or [])],
        },
        "saveToSentItems": save_to_sent,
    }
    r = requests.post(f"{GRAPH}/v1.0/me/sendMail", timeout=60,
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"},
                      json=message)
    if r.status_code != 202:
        raise RuntimeError(f"sendMail failed http={r.status_code} body={r.text[:500]}")
