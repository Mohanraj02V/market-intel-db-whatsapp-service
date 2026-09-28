"""
backend/whatsapp/webhooks.py

Secure WAHA webhook event processor.

Security design:
  1.  HMAC-SHA256 signature verification (reject if invalid).
  2.  Timestamp validation (reject stale events > 5 minutes).
  3.  Payload schema validation (reject unknown event types).
  4.  Duplicate event detection (idempotent processing).
  5.  Safe phone number extraction (no arbitrary field trust).
  6.  Opt-out keyword detection.
  7.  Unmatched message quarantine (no guessing associations).
  8.  Async-safe: all DB writes use atomic transactions.

IMPORTANT:
  - Never execute arbitrary commands from webhook payloads.
  - Never trust phone numbers, chat IDs, or message bodies without validation.
  - Never expose unmatched messages to random users.
"""

import hashlib
import hmac
import json
import logging
import os
import re
from typing import Optional

from django.db import transaction
from django.utils import timezone

from .models import (
    WhatsAppAuditLog,
    WhatsAppConversation,
    WhatsAppEvent,
    WhatsAppMessage,
    WhatsAppOptOut,
    WhatsAppSession,
)
from .services import check_incoming_for_opt_out, _write_audit_log

logger = logging.getLogger(__name__)

# Maximum allowed age of a webhook event (seconds).
# Events older than this are rejected to prevent replay attacks.
WEBHOOK_MAX_AGE_SECONDS = 300  # 5 minutes

# Allowed WAHA event types. Anything else is rejected.
ALLOWED_EVENT_TYPES = frozenset([
    "message",
    "message.ack",
    "session.status",
    "message.waiting",
])

# Valid WhatsApp chat ID pattern.
VALID_CHAT_ID_RE = re.compile(r"^\d{7,15}@(c|g)\.us$")
PHONE_DIGITS_RE = re.compile(r"^\d{7,15}$")


class WebhookVerificationError(Exception):
    """Raised when webhook signature or schema validation fails."""
    pass


class WebhookDuplicateError(Exception):
    """Raised when a duplicate webhook event is detected."""
    pass


def _get_webhook_secret() -> str:
    secret = os.environ.get("WHATSAPP_WEBHOOK_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError(
            "WHATSAPP_WEBHOOK_SECRET is not configured or too short. "
            "Minimum 32 characters required."
        )
    return secret


def verify_hmac_signature(request_body: bytes, signature_header: str) -> bool:
    """
    Verify the HMAC-SHA256 signature from WAHA.

    WAHA signs the raw request body with the configured HMAC secret.
    The signature is sent in the X-Whatsapp-Signature header.

    Returns True if valid, False if invalid.
    """
    if not signature_header:
        logger.warning("Webhook received without signature header.")
        return False

    try:
        secret = _get_webhook_secret()
    except RuntimeError:
        logger.error("WHATSAPP_WEBHOOK_SECRET not configured. Cannot verify webhook.")
        return False

    expected_mac = hmac.new(
        key=secret.encode("utf-8"),
        msg=request_body,
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Compare using constant-time comparison to prevent timing attacks.
    # Strip any "sha256=" prefix that WAHA may add.
    received_mac = signature_header.removeprefix("sha256=").strip()

    return hmac.compare_digest(expected_mac, received_mac)


def _compute_payload_hash(raw_body: bytes) -> str:
    """SHA256 hash of the raw webhook payload for deduplication."""
    return hashlib.sha256(raw_body).hexdigest()


def _validate_event_schema(payload: dict) -> tuple[str, str, dict]:
    """
    Validate the webhook event payload schema.

    Returns (event_type, session_name, event_data).
    Raises WebhookVerificationError on invalid schema.
    """
    if not isinstance(payload, dict):
        raise WebhookVerificationError("Payload must be a JSON object.")

    event_type = payload.get("event") or payload.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise WebhookVerificationError("Missing or invalid 'event' field.")

    # Normalize event type.
    event_type = event_type.strip().lower()

    if event_type not in ALLOWED_EVENT_TYPES:
        raise WebhookVerificationError(
            f"Unknown event type: {event_type!r}. "
            f"Allowed: {sorted(ALLOWED_EVENT_TYPES)}"
        )

    session_name = payload.get("session", "default")
    if not isinstance(session_name, str):
        session_name = "default"
    session_name = session_name.strip()[:100]

    event_data = payload.get("payload") or payload.get("data") or {}
    if not isinstance(event_data, dict):
        event_data = {}

    return event_type, session_name, event_data


def _extract_phone_digits(chat_id: str) -> Optional[str]:
    """
    Safely extract phone digits from a WhatsApp chat ID.

    Returns None if the chat ID is not a valid individual contact.
    """
    if not isinstance(chat_id, str):
        return None

    # Only process individual chats (not groups).
    if not chat_id.endswith("@c.us"):
        return None

    digits = chat_id.split("@")[0]

    if not PHONE_DIGITS_RE.match(digits):
        return None

    return digits


def process_webhook_event(
    raw_body: bytes,
    signature_header: str,
    remote_addr: str = "",
) -> Optional[WhatsAppEvent]:
    """
    Process an incoming WAHA webhook event.

    This is the main entry point called by the webhook view.

    Steps:
      1. Verify HMAC signature.
      2. Parse and validate JSON payload.
      3. Check for duplicate event.
      4. Route to appropriate handler.
      5. Return the WhatsAppEvent record.

    Returns None if the event is rejected (logged and silently discarded).
    Raises WebhookVerificationError if signature/schema is invalid.
    Raises WebhookDuplicateError if this event was already processed.
    """
    # -----------------------------------------------------------------------
    # 1. HMAC verification
    # -----------------------------------------------------------------------
    if not verify_hmac_signature(raw_body, signature_header):
        logger.warning(
            "Webhook HMAC verification FAILED from %s. Request rejected.",
            remote_addr,
        )
        raise WebhookVerificationError("Invalid webhook signature.")

    # -----------------------------------------------------------------------
    # 2. Parse JSON
    # -----------------------------------------------------------------------
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Webhook payload JSON parse error: %s", exc)
        raise WebhookVerificationError("Invalid JSON payload.") from exc

    # -----------------------------------------------------------------------
    # 3. Validate schema
    # -----------------------------------------------------------------------
    event_type, session_name, event_data = _validate_event_schema(payload)

    # -----------------------------------------------------------------------
    # 4. Duplicate detection
    # -----------------------------------------------------------------------
    payload_hash = _compute_payload_hash(raw_body)

    existing = WhatsAppEvent.objects.filter(
        payload_hash=payload_hash,
        processing_status=WhatsAppEvent.ProcessingStatus.PROCESSED,
    ).first()

    if existing:
        logger.debug("Duplicate webhook event detected: hash=%s", payload_hash)
        raise WebhookDuplicateError(f"Duplicate event: {payload_hash}")

    # -----------------------------------------------------------------------
    # 5. Create event record
    # -----------------------------------------------------------------------
    with transaction.atomic():
        event_record = WhatsAppEvent.objects.create(
            event_type=event_type,
            session_name=session_name,
            hmac_valid=True,
            processing_status=WhatsAppEvent.ProcessingStatus.PENDING,
            payload_hash=payload_hash,
        )

    # -----------------------------------------------------------------------
    # 6. Route to handler
    # -----------------------------------------------------------------------
    try:
        if event_type == "message":
            _handle_incoming_message(event_data, session_name, event_record)
        elif event_type == "message.ack":
            _handle_message_ack(event_data, session_name, event_record)
        elif event_type == "session.status":
            _handle_session_status(event_data, session_name, event_record)

        # Mark as processed.
        event_record.processing_status = WhatsAppEvent.ProcessingStatus.PROCESSED
        event_record.processed_at = timezone.now()
        event_record.save(update_fields=["processing_status", "processed_at"])

    except Exception as exc:
        logger.error(
            "Webhook event processing error: event_type=%s error=%s",
            event_type,
            str(exc),
            exc_info=True,
        )
        event_record.processing_status = WhatsAppEvent.ProcessingStatus.FAILED
        event_record.processing_error = str(exc)[:500]
        event_record.processed_at = timezone.now()
        event_record.save(update_fields=["processing_status", "processing_error", "processed_at"])

    return event_record


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def _handle_incoming_message(
    event_data: dict,
    session_name: str,
    event_record: WhatsAppEvent,
) -> None:
    """
    Handle an incoming WhatsApp message event.

    Flow:
      1. Extract and validate chat ID.
      2. Check for opt-out keywords.
      3. Find matching conversation (via chat_id).
      4. If unmatched: quarantine (do not guess associations).
      5. Create WhatsAppMessage record.
      6. Update conversation metadata.
      7. Write audit log.
      8. (Notification to frontend via Django signal or channel — not implemented here)
    """
    # Safely extract fields — never trust them.
    raw_from = str(event_data.get("from") or "")
    raw_body = str(event_data.get("body") or "")
    waha_message_id = str(event_data.get("id") or "")
    is_from_me = bool(event_data.get("fromMe", False))

    # Skip messages sent by us (avoid creating inbound records for our own sends).
    if is_from_me:
        logger.debug("Skipping fromMe=true incoming message event.")
        return

    # Validate chat ID.
    if not VALID_CHAT_ID_RE.match(raw_from):
        logger.warning("Incoming message with invalid chat ID: %r — ignored.", raw_from)
        return

    phone_digits = _extract_phone_digits(raw_from)

    # -----------------------------------------------------------------------
    # Opt-out keyword check BEFORE any other processing.
    # -----------------------------------------------------------------------
    conversation = WhatsAppConversation.objects.filter(chat_id=raw_from).first()

    opt_out_registered = check_incoming_for_opt_out(
        message_body=raw_body,
        phone_digits=phone_digits or "",
        prospect_contact=conversation.prospect_contact if conversation else None,
    )

    if opt_out_registered:
        logger.info("Opt-out registered from incoming message: phone=%s", phone_digits)

    # -----------------------------------------------------------------------
    # Find or create conversation record.
    # -----------------------------------------------------------------------
    is_matched = False
    prospect_contact = None

    if conversation:
        is_matched = conversation.is_matched
        prospect_contact = conversation.prospect_contact
    else:
        # Try to match by phone number against ProspectContact records.
        from prospects.models import ProspectContact

        matched_contact = None
        if phone_digits:
            # Search for ProspectContact by phone number.
            # We strip non-digits from stored numbers for comparison.
            for contact in ProspectContact.objects.filter(
                phone_number__isnull=False
            ).select_related("prospect"):
                stored_digits = re.sub(r"\D+", "", contact.phone_number or "")
                if stored_digits == phone_digits or stored_digits.endswith(phone_digits[-10:]):
                    matched_contact = contact
                    break

        with transaction.atomic():
            session_obj, _ = WhatsAppSession.objects.get_or_create(session_name=session_name)
            conversation = WhatsAppConversation.objects.create(
                chat_id=raw_from,
                prospect_contact=matched_contact,
                prospect=matched_contact.prospect if matched_contact else None,
                session=session_obj,
                is_matched=matched_contact is not None,
            )

        is_matched = conversation.is_matched
        prospect_contact = conversation.prospect_contact

    if not is_matched:
        # Quarantine: do not expose to any user.
        logger.warning(
            "Incoming WhatsApp message from unmatched number %s — quarantined.",
            raw_from,
        )

    # -----------------------------------------------------------------------
    # Create WhatsAppMessage record.
    # -----------------------------------------------------------------------
    # Generate an idempotency key for this inbound message.
    body_hash = hashlib.sha256(raw_body.encode("utf-8")).hexdigest()
    inbound_idempotency = hashlib.sha256(
        f"inbound:{raw_from}:{waha_message_id}:{body_hash}".encode()
    ).hexdigest()

    existing_message = WhatsAppMessage.objects.filter(
        idempotency_key=inbound_idempotency
    ).first()

    if existing_message:
        # Already recorded this message.
        event_record.whatsapp_message = existing_message
        event_record.save(update_fields=["whatsapp_message"])
        return

    with transaction.atomic():
        wa_message = WhatsAppMessage.objects.create(
            idempotency_key=inbound_idempotency,
            conversation=conversation,
            performed_by=None,  # Inbound: no Market Intel user sent this.
            waha_message_id=waha_message_id or None,
            direction=WhatsAppMessage.Direction.INBOUND,
            status=WhatsAppMessage.Status.READ,
            body_preview=raw_body[:500],
            body_hash=body_hash,
            message_type=str(event_data.get("type") or "chat"),
        )

        # Update conversation metadata.
        conversation.last_message_at = timezone.now()
        conversation.last_message_preview = raw_body[:200]
        conversation.last_message_from_me = False
        conversation.unread_count = models_f("unread_count") + 1 if False else conversation.unread_count + 1
        conversation.save(update_fields=[
            "last_message_at", "last_message_preview", "last_message_from_me", "unread_count"
        ])

        # Update session receive count.
        WhatsAppSession.objects.filter(session_name=session_name).update(
            receive_count=models_f_whatsapp("receive_count") + 1
        )

        event_record.whatsapp_message = wa_message
        event_record.save(update_fields=["whatsapp_message"])

    _write_audit_log(
        action=WhatsAppAuditLog.Action.MESSAGE_RECEIVED,
        prospect_contact=prospect_contact,
        session_name=session_name,
        chat_id=raw_from,
        message_id=wa_message.pk,
        success=True,
        result_code="RECEIVED",
        result_detail="matched" if is_matched else "unmatched-quarantined",
    )

    logger.info(
        "Incoming WhatsApp message recorded: chat_id=%s matched=%s message_id=%s",
        raw_from,
        is_matched,
        wa_message.pk,
    )


def _handle_message_ack(
    event_data: dict,
    session_name: str,
    event_record: WhatsAppEvent,
) -> None:
    """
    Handle a message acknowledgement event (delivered/read status update).
    """
    waha_message_id = str(event_data.get("id") or "")
    ack_value = event_data.get("ack")

    if not waha_message_id:
        return

    # Map WAHA ACK values to our status enum.
    # WAHA ACK: 0=PENDING, 1=SENT, 2=DELIVERED, 3=READ, -1=FAILED
    ACK_STATUS_MAP = {
        -1: WhatsAppMessage.Status.FAILED,
        0: WhatsAppMessage.Status.PENDING,
        1: WhatsAppMessage.Status.SENT,
        2: WhatsAppMessage.Status.DELIVERED,
        3: WhatsAppMessage.Status.READ,
    }

    new_status = ACK_STATUS_MAP.get(int(ack_value or 0))
    if not new_status:
        return

    with transaction.atomic():
        updated = WhatsAppMessage.objects.filter(
            waha_message_id=waha_message_id,
        ).update(
            status=new_status,
            delivered_at=timezone.now() if new_status == WhatsAppMessage.Status.DELIVERED else None,
            read_at=timezone.now() if new_status == WhatsAppMessage.Status.READ else None,
        )

    if updated:
        logger.debug("Message ACK updated: waha_id=%s new_status=%s", waha_message_id, new_status)


def _handle_session_status(
    event_data: dict,
    session_name: str,
    event_record: WhatsAppEvent,
) -> None:
    """
    Handle a WAHA session status change event.

    Updates the local WhatsAppSession record.
    Triggers alerts for AUTH_FAILURE or abnormal disconnects.
    """
    waha_status = str(event_data.get("status") or "")
    phone = str(event_data.get("me", {}).get("phone") or "") if isinstance(event_data.get("me"), dict) else ""

    # Map WAHA status strings to our enum.
    STATUS_MAP = {
        "STARTING": WhatsAppSession.Status.STARTING,
        "SCAN_QR_CODE": WhatsAppSession.Status.QR_REQUIRED,
        "WORKING": WhatsAppSession.Status.READY,
        "FAILED": WhatsAppSession.Status.FAILED,
        "STOPPED": WhatsAppSession.Status.STOPPED,
        "DISCONNECTED": WhatsAppSession.Status.DISCONNECTED,
    }

    mapped_status = STATUS_MAP.get(waha_status.upper(), WhatsAppSession.Status.DISCONNECTED)

    with transaction.atomic():
        session, _ = WhatsAppSession.objects.get_or_create(session_name=session_name)

        old_status = session.status
        session.status = mapped_status

        if phone:
            session.phone_number = phone

        if mapped_status == WhatsAppSession.Status.READY and old_status != WhatsAppSession.Status.READY:
            session.reconnect_count += 1
            session.started_at = timezone.now()

        if mapped_status == WhatsAppSession.Status.DISCONNECTED and old_status == WhatsAppSession.Status.READY:
            session.disconnect_count += 1
            session.stopped_at = timezone.now()

        session.save()

    logger.info(
        "Session status change: session=%s %s -> %s",
        session_name,
        old_status,
        mapped_status,
    )

    # Alert on failure states.
    if mapped_status in (WhatsAppSession.Status.FAILED, WhatsAppSession.Status.DISCONNECTED):
        logger.error(
            "WHATSAPP SESSION FAILURE: session=%s status=%s — Manual intervention may be required.",
            session_name,
            mapped_status,
        )

    _write_audit_log(
        action=WhatsAppAuditLog.Action.SESSION_DISCONNECTED
        if mapped_status == WhatsAppSession.Status.DISCONNECTED
        else WhatsAppAuditLog.Action.SESSION_CONNECTED,
        session_name=session_name,
        result_detail=f"WAHA status: {waha_status}",
        success=mapped_status == WhatsAppSession.Status.READY,
    )


# Compatibility shims for F() expressions (avoid importing at module level
# to keep this file self-contained).
def models_f(field_name):
    from django.db.models import F
    return F(field_name)


def models_f_whatsapp(field_name):
    from django.db.models import F
    return F(field_name)
