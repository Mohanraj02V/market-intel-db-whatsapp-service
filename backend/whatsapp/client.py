"""
backend/whatsapp/client.py

Low-level WAHA HTTP client.

Responsibilities:
  - Authenticates all requests to WAHA with X-Api-Key header.
  - Validates configuration at startup (requires WHATSAPP_SERVICE_URL
    and WHATSAPP_SERVICE_API_KEY to be set).
  - Raises WAHAServiceError on connection failures.
  - Never logs the API key.
  - Enforces a configurable request timeout.
  - Does NOT contain any business logic (that lives in services.py).

The WAHA API key is the PLAINTEXT key that corresponds to the sha512 hash
stored in the WAHA container .env. Django sends the plaintext key in
X-Api-Key; WAHA validates it against the stored hash.
"""

import json
import logging
import os
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class WAHAServiceError(Exception):
    """Raised when WAHA returns an error or is unreachable."""

    def __init__(self, message: str, status_code: int = 503, detail: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or {}


class WAHAAuthError(WAHAServiceError):
    """Raised when WAHA returns 401 (invalid API key)."""
    pass


class WAHAClient:
    """
    Authenticated HTTP client for the WAHA WhatsApp API.

    All methods return the parsed JSON response body.
    They raise WAHAServiceError on HTTP or network errors.
    """

    def __init__(self):
        self._base_url: str = self._get_base_url()
        self._api_key: str = self._get_api_key()
        self._timeout: int = self._get_timeout()

    @staticmethod
    def _get_base_url() -> str:
        url = os.environ.get("WHATSAPP_SERVICE_URL", "").rstrip("/")
        if not url:
            raise RuntimeError(
                "WHATSAPP_SERVICE_URL is not configured. "
                "Set it to the HTTPS URL of the Oracle WAHA service."
            )
        if not url.startswith(("https://", "http://")):
            raise RuntimeError(
                f"WHATSAPP_SERVICE_URL must start with https:// (got: {url!r})"
            )
        return url

    @staticmethod
    def _get_api_key() -> str:
        key = os.environ.get("WHATSAPP_SERVICE_API_KEY", "")
        if len(key) < 32:
            raise RuntimeError(
                "WHATSAPP_SERVICE_API_KEY is not configured or is too short. "
                "Minimum 32 characters required."
            )
        return key

    @staticmethod
    def _get_timeout() -> int:
        try:
            return max(5, int(os.environ.get("WHATSAPP_SERVICE_TIMEOUT", "30")))
        except ValueError:
            return 30

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        query: Optional[dict] = None,
    ) -> tuple[int, Any]:
        """
        Execute an authenticated HTTP request to WAHA.

        Returns (status_code, response_body).
        Raises WAHAServiceError on network or HTTP errors.
        """
        url = urljoin(self._base_url + "/", path.lstrip("/"))

        if query:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(query)}"

        headers = {
            # WAHA expects X-Api-Key with the plaintext key.
            "X-Api-Key": self._api_key,
            "Accept": "application/json",
        }

        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url=url, method=method, data=body, headers=headers)

        try:
            with urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
                parsed = json.loads(raw) if raw.strip() else {}
                return response.status, parsed

        except HTTPError as exc:
            raw = exc.read().decode("utf-8") if exc.fp else ""
            try:
                detail = json.loads(raw)
            except json.JSONDecodeError:
                detail = {"detail": raw or "WAHA returned an error."}

            # Log without exposing the API key.
            logger.warning(
                "WAHA HTTP error: method=%s path=%s status=%d",
                method,
                path,
                exc.code,
                extra={"status_code": exc.code},
            )

            if exc.code == 401:
                raise WAHAAuthError(
                    "WAHA authentication failed. Check WHATSAPP_SERVICE_API_KEY.",
                    status_code=401,
                    detail=detail,
                )

            raise WAHAServiceError(
                f"WAHA returned HTTP {exc.code} for {method} {path}",
                status_code=exc.code,
                detail=detail,
            )

        except URLError as exc:
            logger.error(
                "WAHA unreachable: method=%s path=%s reason=%s",
                method,
                path,
                str(exc.reason),
            )
            raise WAHAServiceError(
                f"WAHA service is unreachable: {exc.reason}",
                status_code=503,
            ) from exc

        except TimeoutError:
            logger.error(
                "WAHA request timeout: method=%s path=%s timeout=%ds",
                method,
                path,
                self._timeout,
            )
            raise WAHAServiceError(
                f"WAHA request timed out after {self._timeout}s.",
                status_code=504,
            )

    # -----------------------------------------------------------------------
    # Public API methods — clean abstraction over WAHA endpoints
    # -----------------------------------------------------------------------

    def health(self) -> dict:
        """Check WAHA health (unauthenticated endpoint)."""
        _status, body = self._request("GET", "/health")
        return body

    # -- Session management --------------------------------------------------

    def start_session(self, session_name: str = "default") -> dict:
        """Start a WAHA session."""
        _status, body = self._request(
            "POST",
            f"/api/sessions/{session_name}/start",
        )
        return body

    def stop_session(self, session_name: str = "default") -> dict:
        """Stop a WAHA session (does not log out WhatsApp)."""
        _status, body = self._request(
            "POST",
            f"/api/sessions/{session_name}/stop",
        )
        return body

    def logout_session(self, session_name: str = "default") -> dict:
        """Logout from WhatsApp and clear the session."""
        _status, body = self._request(
            "POST",
            f"/api/sessions/{session_name}/logout",
        )
        return body

    def get_session_status(self, session_name: str = "default") -> dict:
        """Get the current state of a WAHA session."""
        _status, body = self._request(
            "GET",
            f"/api/sessions/{session_name}",
        )
        return body

    def get_all_sessions(self) -> list:
        """List all WAHA sessions."""
        _status, body = self._request("GET", "/api/sessions")
        return body if isinstance(body, list) else []

    def get_qr(self, session_name: str = "default") -> dict:
        """Get the QR code for a session (as image bytes or data URL)."""
        _status, body = self._request(
            "GET",
            f"/api/sessions/{session_name}/auth/qr",
        )
        return body

    # -- Messaging -----------------------------------------------------------

    def send_text(
        self,
        session_name: str,
        chat_id: str,
        text: str,
    ) -> dict:
        """
        Send a plain-text WhatsApp message.

        Args:
            session_name: WAHA session name.
            chat_id:      WhatsApp chat ID (e.g., "919876543210@c.us").
            text:         Message body (max 4096 chars).

        Returns:
            WAHA response with message ID.
        """
        _status, body = self._request(
            "POST",
            f"/api/sendText",
            payload={
                "session": session_name,
                "chatId": chat_id,
                "text": text,
            },
        )
        return body

    def send_image(
        self,
        session_name: str,
        chat_id: str,
        url: str,
        caption: str = "",
        filename: str = "",
    ) -> dict:
        """Send an image message."""
        _status, body = self._request(
            "POST",
            "/api/sendImage",
            payload={
                "session": session_name,
                "chatId": chat_id,
                "file": {"url": url},
                "caption": caption,
                "filename": filename,
            },
        )
        return body

    def send_document(
        self,
        session_name: str,
        chat_id: str,
        url: str,
        filename: str,
        caption: str = "",
    ) -> dict:
        """Send a document/file message."""
        _status, body = self._request(
            "POST",
            "/api/sendFile",
            payload={
                "session": session_name,
                "chatId": chat_id,
                "file": {"url": url},
                "caption": caption,
                "filename": filename,
            },
        )
        return body

    # -- Chat / messages -----------------------------------------------------

    def get_chats(self, session_name: str = "default", limit: int = 50) -> list:
        """Get list of chats."""
        _status, body = self._request(
            "GET",
            "/api/chats",
            query={"session": session_name, "limit": limit},
        )
        return body if isinstance(body, list) else []

    def get_messages(
        self,
        session_name: str,
        chat_id: str,
        limit: int = 50,
    ) -> list:
        """Get messages for a specific chat."""
        _status, body = self._request(
            "GET",
            f"/api/chats/{chat_id}/messages",
            query={"session": session_name, "limit": min(limit, 100)},
        )
        return body if isinstance(body, list) else []

    def mark_as_read(self, session_name: str, chat_id: str) -> dict:
        """Mark all messages in a chat as read."""
        _status, body = self._request(
            "POST",
            f"/api/chats/{chat_id}/messages/read",
            payload={"session": session_name},
        )
        return body


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------
_client_instance: Optional[WAHAClient] = None


def get_waha_client() -> WAHAClient:
    """
    Return a shared WAHAClient instance.

    Raises RuntimeError if WAHA is not configured.
    Raises a specific error if WHATSAPP_ENABLED=false.
    """
    if os.environ.get("WHATSAPP_ENABLED", "false").lower() != "true":
        raise WAHAServiceError(
            "WhatsApp integration is disabled (WHATSAPP_ENABLED=false).",
            status_code=503,
        )

    global _client_instance
    if _client_instance is None:
        _client_instance = WAHAClient()
    return _client_instance
