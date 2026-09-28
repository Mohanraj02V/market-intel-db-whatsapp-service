"""
backend/whatsapp/services.py

Business logic layer for WhatsApp integration.

This is where all safety checks, authorization, and business rules live.
The Django views call this layer. This layer calls the WAHA client.

Safety Guard order for outbound sends:
  1.  WHATSAPP_ENABLED check
  2.  outbound_enabled (global kill switch)
  3.  safety_stop_active (auto-triggered by consecutive failures)
  4.  User send permission
  5.  Company/prospect/key-person authorization
  6.  Opt-out check
  7.  Recipient cooldown check
  8.  Idempotency check (duplicate send protection)
  9.  Rate limit check (per-user + global)
 10.  Message content validation
 11.  WAHA send
 12.  Post-send: record result, update Safety Guard counters

On any WhatsApp-side error:
  - Log the error.
  - Increment consecutive_failed_sends.
  - If threshold reached: trigger safety_stop_active.
  - Alert (log at ERROR level + create WhatsAppSafetyEvent).
  - Do NOT retry automatically.
"""

import hashlib
import logging
import os
import re
from datetime import timedelta
from typing import Optional

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .client import WAHAServiceError, get_waha_client
from .models import (
    WhatsAppAuditLog,
    WhatsAppConfiguration,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppOptOut,
    WhatsAppSafetyEvent,
    WhatsAppSession,
)

logger = logging.getLogger(__name__)
User = get_user_model()

# Regex for valid WhatsApp chat IDs.
VALID_CHAT_ID_RE = re.compile(r"^\d{7,15}@c\.us$")

# Opt-out keywords (case-insensitive).
OPT_OUT_KEYWORDS = frozenset([
    "stop",
    "unsubscribe",
    "do not contact",
    "remove me",
    "dont contact",
    "optout",
    "opt out",
    "no more",
])


class WhatsAppError(Exception):
    """Base exception for WhatsApp service errors."""
    def __init__(self, message: str, code: str = "WA_ERROR"):
        super().__init__(message)
        self.code = code


class WhatsAppBlockedError(WhatsAppError):
    """Raised when a send is blocked by a safety control."""
    def __init__(self, message: str, code: str = "WA_BLOCKED"):
        super().__init__(message, code)


class WhatsAppAuthorizationError(WhatsAppError):
    """Raised when authorization fails."""
    def __init__(self, message: str):
        super().__init__(message, "WA_UNAUTHORIZED")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _phone_digits(value: str) -> str:
    """Strip all non-digit characters from a phone number."""
    return re.sub(r"\D+", "", value or "")


def _chat_id_from_phone(phone: str) -> str:
    """Convert a phone number to a WhatsApp chat ID."""
    digits = _phone_digits(phone)
    return f"{digits}@c.us"


def _get_config() -> WhatsAppConfiguration:
    """Return the WhatsApp configuration, creating defaults if needed."""
    config, _created = WhatsAppConfiguration.objects.get_or_create(pk=1)
    return config


def _get_or_create_session(session_name: str = "default") -> WhatsAppSession:
    """Return the WhatsApp session record, creating it if needed."""
    session, _created = WhatsAppSession.objects.get_or_create(
        session_name=session_name,
    )
    return session


def _write_audit_log(
    action: str,
    performed_by=None,
    prospect_contact=None,
    session_name: str = "",
    chat_id: str = "",
    message_id=None,
    success: Optional[bool] = None,
    result_code: str = "",
    result_detail: str = "",
    request_id: str = "",
    ip_address: str = "",
) -> WhatsAppAuditLog:
    """Write an immutable audit log entry."""
    profile = getattr(performed_by, "profile", None)

    entry = WhatsAppAuditLog(
        action=action,
        performed_by=performed_by,
        performed_by_role=profile.role if profile else None,
        session_name=session_name or "default",
        chat_id=chat_id,
        success=success,
        result_code=result_code,
        result_detail=result_detail[:500] if result_detail else None,
        request_id=request_id,
        ip_address=ip_address or None,
        message_id=message_id,
    )

    if prospect_contact is not None:
        entry.prospect_contact_id = prospect_contact.pk
        entry.prospect_contact_name = prospect_contact.contact_name
        try:
            entry.prospect_id = prospect_contact.prospect_id
            entry.prospect_name = prospect_contact.prospect.company_name
        except Exception:
            pass

    entry.save()
    return entry


def _trigger_safety_stop(reason: str, session: WhatsAppSession, user=None):
    """
    Trigger the Safety Guard: disable all outbound messaging and alert.

    This is a non-reversible automated action. Only a Super Admin can reset it.
    """
    config = _get_config()

    if config.safety_stop_active:
        # Already stopped — just log.
        logger.warning("Safety stop already active. Reason: %s", reason)
        return

    with transaction.atomic():
        config.safety_stop_active = True
        config.safety_stop_reason = reason
        config.safety_stop_at = timezone.now()
        config.save(update_fields=["safety_stop_active", "safety_stop_reason", "safety_stop_at"])

        WhatsAppSafetyEvent.objects.create(
            event_type=WhatsAppSafetyEvent.EventType.SAFETY_STOP_TRIGGERED,
            triggered_by=user,
            details=reason,
            consecutive_failures_at_event=session.consecutive_failed_sends,
        )

    # Log at ERROR level so monitoring systems can alert.
    logger.error(
        "WHATSAPP SAFETY STOP TRIGGERED: %s | consecutive_failures=%d | "
        "OUTBOUND MESSAGING IS NOW DISABLED. Manual Super Admin review required.",
        reason,
        session.consecutive_failed_sends,
    )


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def start_session(performed_by=None, session_name: str = "default") -> dict:
    """Start the WAHA WhatsApp session."""
    client = get_waha_client()

    _write_audit_log(
        action=WhatsAppAuditLog.Action.SESSION_STARTED,
        performed_by=performed_by,
        session_name=session_name,
    )

    try:
        result = client.start_session(session_name)
        session = _get_or_create_session(session_name)
        session.started_at = timezone.now()
        session.status = WhatsAppSession.Status.STARTING
        session.save(update_fields=["started_at", "status"])
        return result
    except WAHAServiceError as exc:
        logger.error("Failed to start WAHA session: %s", str(exc))
        raise WhatsAppError(str(exc), "WA_START_FAILED") from exc


def stop_session(performed_by=None, session_name: str = "default") -> dict:
    """Stop the WAHA session without logging out WhatsApp."""
    client = get_waha_client()

    _write_audit_log(
        action=WhatsAppAuditLog.Action.SESSION_STOPPED,
        performed_by=performed_by,
        session_name=session_name,
    )

    try:
        result = client.stop_session(session_name)
        session = _get_or_create_session(session_name)
        session.status = WhatsAppSession.Status.STOPPED
        session.stopped_at = timezone.now()
        session.save(update_fields=["status", "stopped_at"])
        return result
    except WAHAServiceError as exc:
        logger.error("Failed to stop WAHA session: %s", str(exc))
        raise WhatsAppError(str(exc), "WA_STOP_FAILED") from exc


def logout_session(performed_by=None, session_name: str = "default") -> dict:
    """Logout from WhatsApp (clears the session)."""
    client = get_waha_client()

    logger.info(
        "WhatsApp logout requested by %s for session %s",
        getattr(performed_by, "username", "system"),
        session_name,
    )

    _write_audit_log(
        action=WhatsAppAuditLog.Action.SESSION_STOPPED,
        performed_by=performed_by,
        session_name=session_name,
        result_detail="logout",
    )

    try:
        result = client.logout_session(session_name)
        session = _get_or_create_session(session_name)
        session.status = WhatsAppSession.Status.DISCONNECTED
        session.phone_number = None
        session.stopped_at = timezone.now()
        session.save(update_fields=["status", "phone_number", "stopped_at"])
        return result
    except WAHAServiceError as exc:
        raise WhatsAppError(str(exc), "WA_LOGOUT_FAILED") from exc


def get_session_status(session_name: str = "default") -> dict:
    """Get WAHA session status (from WAHA, plus local tracking data)."""
    client = get_waha_client()

    try:
        waha_status = client.get_session_status(session_name)
        local_session = _get_or_create_session(session_name)

        return {
            "session_name": session_name,
            "waha_status": waha_status,
            "local_status": local_session.status,
            "phone_number": local_session.phone_number,
            "send_count": local_session.send_count,
            "failed_send_count": local_session.failed_send_count,
            "consecutive_failed_sends": local_session.consecutive_failed_sends,
            "last_successful_message_at": local_session.last_successful_message_at,
        }
    except WAHAServiceError as exc:
        return {
            "session_name": session_name,
            "waha_status": {"error": str(exc)},
            "local_status": WhatsAppSession.Status.DISCONNECTED,
        }


def get_qr(session_name: str = "default", performed_by=None) -> dict:
    """Get the QR code for WhatsApp authentication."""
    client = get_waha_client()

    _write_audit_log(
        action=WhatsAppAuditLog.Action.QR_REQUESTED,
        performed_by=performed_by,
        session_name=session_name,
    )

    try:
        return client.get_qr(session_name)
    except WAHAServiceError as exc:
        raise WhatsAppError(str(exc), "WA_QR_FAILED") from exc


# ---------------------------------------------------------------------------
# Outbound messaging — with full Safety Guard pipeline
# ---------------------------------------------------------------------------

def send_whatsapp_message(
    *,
    performed_by,
    prospect_contact,
    message_body: str,
    session_name: str = "default",
    intent_tag: str = "manual",
    request_id: str = "",
    ip_address: str = "",
) -> WhatsAppMessage:
    """
    Send a WhatsApp text message through the full Safety Guard pipeline.

    Args:
        performed_by:     The authenticated Django User initiating the send.
        prospect_contact: The ProspectContact (Key Person) to message.
        message_body:     The plaintext message.
        session_name:     WAHA session name (default: "default").
        intent_tag:       A label for the send intent (for idempotency key).
        request_id:       Optional request/trace ID for logging.
        ip_address:       Request IP for audit log.

    Returns:
        WhatsAppMessage record.

    Raises:
        WhatsAppBlockedError: If any safety control blocks the send.
        WhatsAppAuthorizationError: If authorization fails.
        WhatsAppError: On unexpected WAHA errors.
    """
    from prospects.models import ProspectContact

    # ------------------------------------------------------------------
    # GUARD 1: WHATSAPP_ENABLED
    # ------------------------------------------------------------------
    if os.environ.get("WHATSAPP_ENABLED", "false").lower() != "true":
        raise WhatsAppBlockedError(
            "WhatsApp integration is disabled.",
            code="WA_DISABLED",
        )

    # ------------------------------------------------------------------
    # GUARD 2: Global kill switch
    # ------------------------------------------------------------------
    config = _get_config()

    if not config.outbound_enabled:
        WhatsAppSafetyEvent.objects.create(
            event_type=WhatsAppSafetyEvent.EventType.OUTBOUND_DISABLED,
            triggered_by=performed_by,
            prospect_contact=prospect_contact,
        )
        raise WhatsAppBlockedError(
            "Outbound WhatsApp messaging is disabled. Contact your administrator.",
            code="WA_OUTBOUND_DISABLED",
        )

    # ------------------------------------------------------------------
    # GUARD 3: Safety stop
    # ------------------------------------------------------------------
    if config.safety_stop_active:
        WhatsAppSafetyEvent.objects.create(
            event_type=WhatsAppSafetyEvent.EventType.SAFETY_STOP_TRIGGERED,
            triggered_by=performed_by,
            prospect_contact=prospect_contact,
            details="Send blocked due to active safety stop.",
        )
        raise WhatsAppBlockedError(
            "WhatsApp sending has been temporarily stopped due to unusual activity. "
            "Contact your administrator. Reason: "
            + (config.safety_stop_reason or "Unknown."),
            code="WA_SAFETY_STOP",
        )

    # ------------------------------------------------------------------
    # GUARD 4: User send permission
    # ------------------------------------------------------------------
    profile = getattr(performed_by, "profile", None)
    user_role = profile.role if profile else None

    # Only LQ and SuperAdmin can send WhatsApp messages.
    allowed_sender_roles = {"LQ"}
    is_superuser = performed_by.is_superuser

    if not is_superuser and user_role not in allowed_sender_roles:
        raise WhatsAppAuthorizationError(
            f"Role '{user_role}' is not permitted to send WhatsApp messages. "
            "Only LQ users and administrators can send WhatsApp messages."
        )

    # ------------------------------------------------------------------
    # GUARD 5: Contact/prospect validation
    # ------------------------------------------------------------------
    if not isinstance(prospect_contact, ProspectContact):
        raise WhatsAppAuthorizationError("Invalid prospect contact.")

    if not prospect_contact.phone_number:
        raise WhatsAppBlockedError(
            "This key person does not have a phone number.",
            code="WA_NO_PHONE",
        )

    phone = _phone_digits(prospect_contact.phone_number)

    if len(phone) < 7 or len(phone) > 15:
        raise WhatsAppBlockedError(
            "Phone number must be in international format (7-15 digits, e.g., 919876543210).",
            code="WA_INVALID_PHONE",
        )

    chat_id = f"{phone}@c.us"

    # Validate chat ID format.
    if not VALID_CHAT_ID_RE.match(chat_id):
        raise WhatsAppBlockedError(
            f"Invalid WhatsApp chat ID format: {chat_id!r}",
            code="WA_INVALID_CHAT_ID",
        )

    # ------------------------------------------------------------------
    # GUARD 6: Opt-out check
    # ------------------------------------------------------------------
    opt_out = WhatsAppOptOut.objects.filter(phone_digits=phone, is_active=True).first()
    if opt_out:
        session = _get_or_create_session(session_name)
        session.opt_out_count = models_f("opt_out_count") + 1 if hasattr(session, "_counter") else session.opt_out_count + 1
        session.save(update_fields=["opt_out_count"])

        WhatsAppSafetyEvent.objects.create(
            event_type=WhatsAppSafetyEvent.EventType.OPT_OUT_BLOCKED,
            triggered_by=performed_by,
            prospect_contact=prospect_contact,
            details=f"Recipient {phone} has opted out.",
        )

        _write_audit_log(
            action=WhatsAppAuditLog.Action.SEND_BLOCKED,
            performed_by=performed_by,
            prospect_contact=prospect_contact,
            session_name=session_name,
            chat_id=chat_id,
            success=False,
            result_code="WA_OPT_OUT",
            result_detail="Recipient has opted out.",
            request_id=request_id,
            ip_address=ip_address,
        )

        raise WhatsAppBlockedError(
            "This recipient has opted out of WhatsApp messages.",
            code="WA_OPT_OUT",
        )

    # ------------------------------------------------------------------
    # GUARD 7: Recipient cooldown
    # ------------------------------------------------------------------
    cooldown_minutes = config.recipient_cooldown_minutes
    cooldown_cutoff = timezone.now() - timedelta(minutes=cooldown_minutes)

    recent_to_recipient = WhatsAppMessage.objects.filter(
        conversation__chat_id=chat_id,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        status__in=[WhatsAppMessage.Status.SENT, WhatsAppMessage.Status.DELIVERED, WhatsAppMessage.Status.READ],
        created_at__gte=cooldown_cutoff,
    ).exists()

    if recent_to_recipient:
        session = _get_or_create_session(session_name)
        session.cooldown_block_count += 1
        session.save(update_fields=["cooldown_block_count"])

        WhatsAppSafetyEvent.objects.create(
            event_type=WhatsAppSafetyEvent.EventType.COOLDOWN_BLOCKED,
            triggered_by=performed_by,
            prospect_contact=prospect_contact,
            details=f"Cooldown: {cooldown_minutes} minutes not elapsed since last message to {phone}.",
        )

        _write_audit_log(
            action=WhatsAppAuditLog.Action.SEND_BLOCKED,
            performed_by=performed_by,
            prospect_contact=prospect_contact,
            session_name=session_name,
            chat_id=chat_id,
            success=False,
            result_code="WA_COOLDOWN",
            result_detail=f"Cooldown: {cooldown_minutes} minutes not elapsed.",
            request_id=request_id,
            ip_address=ip_address,
        )

        raise WhatsAppBlockedError(
            f"A message was recently sent to this recipient. "
            f"Please wait {cooldown_minutes} minutes before sending again.",
            code="WA_COOLDOWN",
        )

    # ------------------------------------------------------------------
    # GUARD 8: Idempotency (duplicate send protection)
    # ------------------------------------------------------------------
    idempotency_key = WhatsAppMessage.compute_idempotency_key(
        user_id=performed_by.pk,
        prospect_contact_id=str(prospect_contact.pk),
        message_body=message_body,
        intent_tag=intent_tag,
    )

    existing = WhatsAppMessage.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        WhatsAppSafetyEvent.objects.create(
            event_type=WhatsAppSafetyEvent.EventType.DUPLICATE_BLOCKED,
            triggered_by=performed_by,
            prospect_contact=prospect_contact,
            details="Duplicate send attempt detected by idempotency key.",
        )
        logger.info(
            "Duplicate WhatsApp send blocked: idempotency_key=%s message_id=%s",
            idempotency_key,
            existing.pk,
        )
        # Return the existing message record instead of sending again.
        return existing

    # ------------------------------------------------------------------
    # GUARD 9: Rate limits
    # ------------------------------------------------------------------
    one_hour_ago = timezone.now() - timedelta(hours=1)

    # Per-user rate limit.
    user_sends_last_hour = WhatsAppMessage.objects.filter(
        performed_by=performed_by,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        created_at__gte=one_hour_ago,
    ).count()

    if user_sends_last_hour >= config.per_user_hourly_limit:
        WhatsAppSafetyEvent.objects.create(
            event_type=WhatsAppSafetyEvent.EventType.RATE_LIMIT_BLOCKED,
            triggered_by=performed_by,
            prospect_contact=prospect_contact,
            details=f"Per-user hourly limit reached: {user_sends_last_hour}/{config.per_user_hourly_limit}",
        )
        raise WhatsAppBlockedError(
            f"You have reached your hourly WhatsApp message limit ({config.per_user_hourly_limit}/hour). "
            "Please wait before sending more messages.",
            code="WA_RATE_LIMIT_USER",
        )

    # Global rate limit.
    global_sends_last_hour = WhatsAppMessage.objects.filter(
        direction=WhatsAppMessage.Direction.OUTBOUND,
        created_at__gte=one_hour_ago,
    ).count()

    if global_sends_last_hour >= config.global_hourly_limit:
        WhatsAppSafetyEvent.objects.create(
            event_type=WhatsAppSafetyEvent.EventType.RATE_LIMIT_BLOCKED,
            triggered_by=performed_by,
            prospect_contact=prospect_contact,
            details=f"Global hourly limit reached: {global_sends_last_hour}/{config.global_hourly_limit}",
        )
        raise WhatsAppBlockedError(
            "The global WhatsApp message limit for this hour has been reached. "
            "Please try again later.",
            code="WA_RATE_LIMIT_GLOBAL",
        )

    # ------------------------------------------------------------------
    # GUARD 10: Message content validation
    # ------------------------------------------------------------------
    body = message_body.strip() if isinstance(message_body, str) else ""

    if not body:
        raise WhatsAppBlockedError("Message body cannot be empty.", code="WA_EMPTY_BODY")

    if len(body) > 4096:
        raise WhatsAppBlockedError(
            "Message body exceeds 4096 characters.", code="WA_BODY_TOO_LONG"
        )

    # ------------------------------------------------------------------
    # STEP 11: Get/create conversation record
    # ------------------------------------------------------------------
    conversation, _ = WhatsAppConversation.objects.get_or_create(
        chat_id=chat_id,
        defaults={
            "prospect_contact": prospect_contact,
            "prospect": prospect_contact.prospect,
            "session": _get_or_create_session(session_name),
            "is_matched": True,
        },
    )

    # Ensure conversation is matched to this contact.
    if not conversation.is_matched:
        conversation.prospect_contact = prospect_contact
        conversation.prospect = prospect_contact.prospect
        conversation.is_matched = True
        conversation.save(update_fields=["prospect_contact", "prospect", "is_matched"])

    # ------------------------------------------------------------------
    # STEP 12: Create message record (PENDING) before sending.
    # This ensures we have a record even if WAHA times out.
    # ------------------------------------------------------------------
    body_hash = WhatsAppMessage.compute_body_hash(body)

    with transaction.atomic():
        wa_message = WhatsAppMessage.objects.create(
            idempotency_key=idempotency_key,
            conversation=conversation,
            performed_by=performed_by,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            status=WhatsAppMessage.Status.PENDING,
            body_preview=body[:500],
            body_hash=body_hash,
            message_type="chat",
        )

    # ------------------------------------------------------------------
    # STEP 13: Call WAHA
    # ------------------------------------------------------------------
    session = _get_or_create_session(session_name)
    client = get_waha_client()

    try:
        waha_response = client.send_text(
            session_name=session_name,
            chat_id=chat_id,
            text=body,
        )

        # Success path.
        with transaction.atomic():
            wa_message.status = WhatsAppMessage.Status.SENT
            wa_message.waha_message_id = (
                waha_response.get("id") or
                waha_response.get("key", {}).get("id")
            )
            wa_message.sent_at = timezone.now()
            wa_message.save(update_fields=["status", "waha_message_id", "sent_at"])

            session.send_count += 1
            session.consecutive_failed_sends = 0  # Reset on success.
            session.last_successful_message_at = timezone.now()
            session.save(update_fields=[
                "send_count", "consecutive_failed_sends", "last_successful_message_at"
            ])

            conversation.last_message_at = timezone.now()
            conversation.last_message_preview = body[:200]
            conversation.last_message_from_me = True
            conversation.save(update_fields=[
                "last_message_at", "last_message_preview", "last_message_from_me"
            ])

        _write_audit_log(
            action=WhatsAppAuditLog.Action.MESSAGE_SENT,
            performed_by=performed_by,
            prospect_contact=prospect_contact,
            session_name=session_name,
            chat_id=chat_id,
            message_id=wa_message.pk,
            success=True,
            result_code="SENT",
            request_id=request_id,
            ip_address=ip_address,
        )

        logger.info(
            "WhatsApp message sent: user=%s prospect_contact=%s chat_id=%s message_id=%s",
            performed_by.username,
            prospect_contact.pk,
            chat_id,
            wa_message.pk,
        )

        return wa_message

    except WAHAServiceError as exc:
        # Failure path — update message record and Safety Guard counters.
        error_msg = str(exc)
        error_code = getattr(exc, "status_code", 500)

        with transaction.atomic():
            wa_message.status = WhatsAppMessage.Status.FAILED
            wa_message.error_message = error_msg
            wa_message.error_code = str(error_code)
            wa_message.save(update_fields=["status", "error_message", "error_code", "updated_at"])

            session.failed_send_count += 1
            session.consecutive_failed_sends += 1
            session.last_failed_message_at = timezone.now()
            session.last_failed_message_error = error_msg[:500]
            session.save(update_fields=[
                "failed_send_count",
                "consecutive_failed_sends",
                "last_failed_message_at",
                "last_failed_message_error",
            ])

        _write_audit_log(
            action=WhatsAppAuditLog.Action.MESSAGE_FAILED,
            performed_by=performed_by,
            prospect_contact=prospect_contact,
            session_name=session_name,
            chat_id=chat_id,
            message_id=wa_message.pk,
            success=False,
            result_code=str(error_code),
            result_detail=error_msg,
            request_id=request_id,
            ip_address=ip_address,
        )

        logger.error(
            "WhatsApp send failed: user=%s chat_id=%s error=%s consecutive_failures=%d",
            performed_by.username,
            chat_id,
            error_msg,
            session.consecutive_failed_sends,
        )

        # Check if Safety Guard should trigger.
        if session.consecutive_failed_sends >= config.consecutive_failure_threshold:
            _trigger_safety_stop(
                reason=(
                    f"Automatic safety stop: {session.consecutive_failed_sends} consecutive "
                    f"send failures. Last error: {error_msg}"
                ),
                session=session,
                user=performed_by,
            )

        raise WhatsAppError(
            f"WhatsApp message delivery failed: {error_msg}",
            code="WA_SEND_FAILED",
        ) from exc


# ---------------------------------------------------------------------------
# Admin controls
# ---------------------------------------------------------------------------

def admin_reset_safety_stop(performed_by, reason: str = "") -> None:
    """
    Reset the Safety Guard stop. Only callable by Super Admin.

    This resumes outbound messaging. The admin must have reviewed the
    incident before calling this.
    """
    if not performed_by.is_superuser:
        raise WhatsAppAuthorizationError(
            "Only Super Admin can reset the WhatsApp safety stop."
        )

    config = _get_config()

    if not config.safety_stop_active:
        return  # Already not in safety stop.

    with transaction.atomic():
        config.safety_stop_active = False
        config.safety_stop_reason = None
        config.safety_stop_at = None
        config.save(update_fields=["safety_stop_active", "safety_stop_reason", "safety_stop_at"])

        session = _get_or_create_session()
        session.consecutive_failed_sends = 0
        session.save(update_fields=["consecutive_failed_sends"])

        WhatsAppSafetyEvent.objects.create(
            event_type=WhatsAppSafetyEvent.EventType.SAFETY_STOP_RESET,
            triggered_by=performed_by,
            details=reason or "Manual reset by Super Admin.",
        )

    _write_audit_log(
        action=WhatsAppAuditLog.Action.ADMIN_RESUMED,
        performed_by=performed_by,
        result_detail=reason or "Safety stop manually reset.",
        success=True,
    )

    logger.warning(
        "WhatsApp safety stop RESET by %s. Reason: %s. Outbound messaging resumed.",
        performed_by.username,
        reason or "No reason provided.",
    )


def admin_set_outbound_enabled(performed_by, enabled: bool, reason: str = "") -> None:
    """
    Enable or disable the global outbound kill switch.
    Only callable by Super Admin or Manager.
    """
    profile = getattr(performed_by, "profile", None)
    role = profile.role if profile else None

    if not performed_by.is_superuser and role not in ("MANAGER",):
        raise WhatsAppAuthorizationError(
            "Only Super Admin or Manager can change the outbound enabled setting."
        )

    config = _get_config()
    config.outbound_enabled = enabled
    config.save(update_fields=["outbound_enabled"])

    event_type = (
        WhatsAppSafetyEvent.EventType.OUTBOUND_ENABLED
        if enabled
        else WhatsAppSafetyEvent.EventType.OUTBOUND_DISABLED
    )

    WhatsAppSafetyEvent.objects.create(
        event_type=event_type,
        triggered_by=performed_by,
        details=reason or f"Outbound messaging {'enabled' if enabled else 'disabled'}.",
    )

    _write_audit_log(
        action=WhatsAppAuditLog.Action.CONFIG_CHANGED,
        performed_by=performed_by,
        result_detail=f"outbound_enabled set to {enabled}. Reason: {reason}",
        success=True,
    )

    logger.warning(
        "WhatsApp outbound_enabled set to %s by %s. Reason: %s",
        enabled,
        performed_by.username,
        reason or "No reason provided.",
    )


def register_opt_out(
    phone_digits: str,
    reason: str,
    prospect_contact=None,
    registered_by=None,
    trigger_message: str = "",
) -> WhatsAppOptOut:
    """
    Register a recipient opt-out.

    Blocks all future outbound messages to this phone number.
    """
    opt_out, created = WhatsAppOptOut.objects.get_or_create(
        phone_digits=phone_digits,
        defaults={
            "reason": reason,
            "prospect_contact": prospect_contact,
            "registered_by": registered_by,
            "trigger_message": trigger_message,
            "is_active": True,
        },
    )

    if not created and not opt_out.is_active:
        # Reactivate an existing opt-out.
        opt_out.is_active = True
        opt_out.reason = reason
        opt_out.registered_by = registered_by
        opt_out.trigger_message = trigger_message
        opt_out.reversed_by = None
        opt_out.reversed_at = None
        opt_out.reversal_reason = None
        opt_out.save()

    if created or not opt_out.is_active:
        _write_audit_log(
            action=WhatsAppAuditLog.Action.OPT_OUT_REGISTERED,
            performed_by=registered_by,
            prospect_contact=prospect_contact,
            result_detail=f"Opt-out registered for {phone_digits}. Reason: {reason}",
            success=True,
        )

        session = _get_or_create_session()
        session.opt_out_count += 1
        session.save(update_fields=["opt_out_count"])

    return opt_out


def check_incoming_for_opt_out(message_body: str, phone_digits: str, prospect_contact=None) -> bool:
    """
    Check if an incoming message contains an opt-out keyword.

    Returns True if opt-out was registered, False otherwise.
    """
    body_lower = message_body.strip().lower()

    for keyword in OPT_OUT_KEYWORDS:
        if keyword in body_lower or body_lower == keyword:
            register_opt_out(
                phone_digits=phone_digits,
                reason=WhatsAppOptOut.Reason.INCOMING_KEYWORD,
                prospect_contact=prospect_contact,
                trigger_message=message_body[:500],
            )
            logger.info(
                "Opt-out keyword detected from %s. Keyword match: %r",
                phone_digits,
                keyword,
            )
            return True

    return False
