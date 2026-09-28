"""
backend/whatsapp/models.py

WhatsApp integration data models.

Design principles:
  - Do NOT duplicate Prospect, ProspectContact, Company, or User data.
  - Use ForeignKey references to existing models only.
  - Store only what is needed for WhatsApp integration metadata.
  - All sensitive data (API keys, session secrets) stay in environment
    variables, never in the database.

Models defined here:
  WhatsAppConfiguration   -- Per-company WAHA configuration flags
  WhatsAppSession         -- WhatsApp session state tracking
  WhatsAppConversation    -- Maps WhatsApp chat ID to a ProspectContact
  WhatsAppMessage         -- Sent/received message records
  WhatsAppOptOut          -- Contacts who requested no further messages
  WhatsAppSafetyEvent     -- Safety guard trigger records
  WhatsAppAuditLog        -- Immutable audit trail for all actions
"""

import hashlib
import uuid

from django.contrib.auth import get_user_model
from django.db import models
from django.utils import timezone

User = get_user_model()


# ---------------------------------------------------------------------------
# WhatsAppConfiguration
# ---------------------------------------------------------------------------

class WhatsAppConfiguration(models.Model):
    """
    Company-level WhatsApp configuration flags.

    One record per company (or global if no Company model is present).
    Controls whether WhatsApp is enabled and outbound messaging is allowed.
    """

    # Global outbound kill switch.
    # If False, ALL outbound messages are blocked regardless of other settings.
    outbound_enabled = models.BooleanField(
        default=False,
        help_text=(
            "Master kill switch for outbound WhatsApp messages. "
            "Set to False to immediately stop all outbound sends."
        ),
    )

    # Emergency stop flag (set by Safety Guard on abnormal behavior).
    # Must be manually cleared by Super Admin after investigation.
    safety_stop_active = models.BooleanField(
        default=False,
        help_text=(
            "Activated by the Safety Guard on abnormal send failures. "
            "Requires manual Super Admin review and reset to resume."
        ),
    )

    # Reason for current safety stop (for admin display).
    safety_stop_reason = models.TextField(blank=True, null=True)
    safety_stop_at = models.DateTimeField(null=True, blank=True)
    safety_stop_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_safety_stops",
    )

    # Per-recipient cooldown period in minutes.
    recipient_cooldown_minutes = models.IntegerField(
        default=120,
        help_text="Minimum minutes between messages to the same recipient.",
    )

    # Global rate limit: max messages per hour across all users/prospects.
    global_hourly_limit = models.IntegerField(
        default=50,
        help_text="Maximum total outbound messages allowed per hour.",
    )

    # Per-user rate limit: max messages per hour for a single user.
    per_user_hourly_limit = models.IntegerField(
        default=20,
        help_text="Maximum outbound messages per user per hour.",
    )

    # Safety Guard thresholds.
    # If consecutive send failures exceed this, trigger safety stop.
    consecutive_failure_threshold = models.IntegerField(
        default=5,
        help_text=(
            "Number of consecutive send failures before the Safety Guard "
            "triggers an automatic outbound stop."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "WhatsApp Configuration"

    def __str__(self):
        return f"WhatsApp Config (outbound={'ON' if self.outbound_enabled else 'OFF'})"


# ---------------------------------------------------------------------------
# WhatsAppSession
# ---------------------------------------------------------------------------

class WhatsAppSession(models.Model):
    """
    Tracks the current state of the WAHA WhatsApp session.

    There is typically one session per deployment.
    This model stores state received from WAHA webhook events and health polls.
    """

    class Status(models.TextChoices):
        STARTING = "STARTING", "Starting"
        QR_REQUIRED = "QR_REQUIRED", "QR Required"
        AUTHENTICATED = "AUTHENTICATED", "Authenticated"
        READY = "READY", "Ready"
        DISCONNECTED = "DISCONNECTED", "Disconnected"
        FAILED = "FAILED", "Failed"
        STOPPED = "STOPPED", "Stopped"

    session_name = models.CharField(
        max_length=100,
        default="default",
        unique=True,
        help_text="WAHA session name (matches WAHA session ID).",
    )

    status = models.CharField(
        max_length=30,
        choices=Status.choices,
        default=Status.DISCONNECTED,
    )

    # Phone number of the connected WhatsApp account.
    # Populated after successful authentication.
    phone_number = models.CharField(max_length=50, blank=True, null=True)

    # Last WAHA health check response.
    last_health_check_at = models.DateTimeField(null=True, blank=True)
    last_health_status = models.CharField(max_length=50, blank=True, null=True)

    # Counters for monitoring.
    send_count = models.BigIntegerField(default=0)
    receive_count = models.BigIntegerField(default=0)
    failed_send_count = models.BigIntegerField(default=0)
    consecutive_failed_sends = models.IntegerField(default=0)
    disconnect_count = models.IntegerField(default=0)
    reconnect_count = models.IntegerField(default=0)
    outbound_block_count = models.BigIntegerField(default=0)
    cooldown_block_count = models.BigIntegerField(default=0)
    opt_out_count = models.BigIntegerField(default=0)

    last_successful_message_at = models.DateTimeField(null=True, blank=True)
    last_failed_message_at = models.DateTimeField(null=True, blank=True)
    last_failed_message_error = models.TextField(blank=True, null=True)

    started_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "WhatsApp Session"

    def __str__(self):
        return f"Session '{self.session_name}' ({self.status})"


# ---------------------------------------------------------------------------
# WhatsAppConversation
# ---------------------------------------------------------------------------

class WhatsAppConversation(models.Model):
    """
    Maps a WhatsApp chat ID to a Market Intel ProspectContact (Key Person).

    This is the bridge between raw WhatsApp chats and Market Intel records.
    Every conversation must be associated with an authorized prospect contact.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # The raw WhatsApp chat ID (e.g., "919876543210@c.us").
    # This is the authoritative identifier used by WAHA.
    chat_id = models.CharField(
        max_length=100,
        unique=True,
        help_text="WhatsApp chat ID in format: <digits>@c.us",
    )

    # Reference to the ProspectContact (Key Person) this conversation belongs to.
    # This is the primary multi-tenancy anchor.
    prospect_contact = models.ForeignKey(
        "prospects.ProspectContact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_conversations",
        help_text="The Key Person associated with this WhatsApp contact.",
    )

    # The prospect this conversation is associated with (denormalized for query efficiency).
    prospect = models.ForeignKey(
        "prospects.Prospect",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_conversations",
    )

    # The WAHA session this conversation belongs to.
    session = models.ForeignKey(
        WhatsAppSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="conversations",
    )

    # Whether this conversation has been matched to a Market Intel record.
    # Unmatched conversations are quarantined and require admin review.
    is_matched = models.BooleanField(
        default=False,
        help_text="True only when linked to an authorized ProspectContact.",
    )

    # Unread message count (from WAHA).
    unread_count = models.IntegerField(default=0)

    # Last message timestamp (for display purposes).
    last_message_at = models.DateTimeField(null=True, blank=True)
    last_message_preview = models.CharField(max_length=200, blank=True, null=True)
    last_message_from_me = models.BooleanField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "WhatsApp Conversation"
        indexes = [
            models.Index(fields=["chat_id"]),
            models.Index(fields=["prospect_contact"]),
            models.Index(fields=["is_matched"]),
        ]

    def __str__(self):
        contact = self.prospect_contact.contact_name if self.prospect_contact else "Unmatched"
        return f"WhatsApp: {self.chat_id} → {contact}"


# ---------------------------------------------------------------------------
# WhatsAppMessage
# ---------------------------------------------------------------------------

class WhatsAppMessage(models.Model):
    """
    Record of a WhatsApp message (sent or received).

    Stores metadata only — full message body is truncated/omitted for privacy
    unless MESSAGE_LOGGING_ENABLED is explicitly set.
    """

    class Direction(models.TextChoices):
        OUTBOUND = "OUTBOUND", "Outbound (sent)"
        INBOUND = "INBOUND", "Inbound (received)"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        SENT = "SENT", "Sent"
        DELIVERED = "DELIVERED", "Delivered"
        READ = "READ", "Read"
        FAILED = "FAILED", "Failed"
        BLOCKED = "BLOCKED", "Blocked (Safety Guard)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Idempotency key — prevents double-sends.
    # Computed as SHA256 of (user_id + prospect_contact_id + message_hash + intent).
    idempotency_key = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="SHA256 idempotency key. Prevents duplicate sends.",
    )

    # Link to conversation.
    conversation = models.ForeignKey(
        WhatsAppConversation,
        on_delete=models.CASCADE,
        related_name="messages",
    )

    # Who sent/received this message.
    performed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_messages",
        help_text="The Market Intel user who initiated the send (outbound only).",
    )

    # WAHA message ID (from WAHA response or webhook event).
    waha_message_id = models.CharField(max_length=255, blank=True, null=True, db_index=True)

    direction = models.CharField(max_length=10, choices=Direction.choices)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    # Message content (truncated to 500 chars; no full body logging by default).
    # Set WHATSAPP_LOG_MESSAGE_BODY=true in Django .env to store full body.
    body_preview = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text="First 500 chars of message body (for admin reference only).",
    )

    # Content hash (SHA256 of full body) — used for idempotency/duplicate detection.
    body_hash = models.CharField(max_length=64, blank=True, null=True)

    # Message type (text, image, document, audio, etc.)
    message_type = models.CharField(max_length=50, default="chat")

    # Error details (for failed sends).
    error_message = models.TextField(blank=True, null=True)
    error_code = models.CharField(max_length=50, blank=True, null=True)

    # Timestamps.
    waha_timestamp = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Timestamp from the WAHA event/response.",
    )
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "WhatsApp Message"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["idempotency_key"]),
            models.Index(fields=["waha_message_id"]),
            models.Index(fields=["conversation", "direction", "created_at"]),
        ]

    def __str__(self):
        return f"{self.direction} [{self.status}] via {self.conversation.chat_id}"

    @staticmethod
    def compute_idempotency_key(
        user_id: int,
        prospect_contact_id: str,
        message_body: str,
        intent_tag: str = "send",
    ) -> str:
        """
        Compute a deterministic idempotency key.

        Given the same inputs, this always produces the same key.
        A duplicate send attempt will hit the unique constraint.
        """
        raw = f"{user_id}:{prospect_contact_id}:{intent_tag}:{message_body}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def compute_body_hash(body: str) -> str:
        """SHA256 of message body for content deduplication."""
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# WhatsAppOptOut
# ---------------------------------------------------------------------------

class WhatsAppOptOut(models.Model):
    """
    Records contacts who have explicitly requested no further WhatsApp messages.

    This is a blocklist. Any future outbound send to a contact in this table
    is automatically rejected by the Safety Guard.
    """

    # Phone number in E.164-like digits (no + sign, no special chars).
    phone_digits = models.CharField(
        max_length=20,
        unique=True,
        db_index=True,
        help_text="Digits-only phone number (e.g., 919876543210).",
    )

    # The ProspectContact that requested opt-out (if matched).
    prospect_contact = models.ForeignKey(
        "prospects.ProspectContact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_opt_outs",
    )

    # How the opt-out was registered.
    class Reason(models.TextChoices):
        RECIPIENT_REQUEST = "RECIPIENT_REQUEST", "Recipient requested"
        ADMIN_ACTION = "ADMIN_ACTION", "Admin action"
        INCOMING_KEYWORD = "INCOMING_KEYWORD", "Incoming STOP keyword"

    reason = models.CharField(max_length=30, choices=Reason.choices)

    # Raw incoming message that triggered the opt-out (keyword cases).
    trigger_message = models.TextField(blank=True, null=True)

    # Whether this opt-out is currently active.
    is_active = models.BooleanField(default=True)

    # Who registered/reversed the opt-out.
    registered_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_opt_outs_registered",
    )
    reversed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_opt_outs_reversed",
    )
    reversed_at = models.DateTimeField(null=True, blank=True)
    reversal_reason = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "WhatsApp Opt-Out"
        indexes = [models.Index(fields=["phone_digits", "is_active"])]

    def __str__(self):
        return f"OptOut: {self.phone_digits} ({'active' if self.is_active else 'reversed'})"


# ---------------------------------------------------------------------------
# WhatsAppSafetyEvent
# ---------------------------------------------------------------------------

class WhatsAppSafetyEvent(models.Model):
    """
    Records Safety Guard trigger events.

    Safety events are immutable audit records of when and why the Safety Guard
    blocked a send or triggered an outbound stop.
    """

    class EventType(models.TextChoices):
        SAFETY_STOP_TRIGGERED = "SAFETY_STOP_TRIGGERED", "Safety Stop Triggered"
        SAFETY_STOP_RESET = "SAFETY_STOP_RESET", "Safety Stop Reset"
        OUTBOUND_DISABLED = "OUTBOUND_DISABLED", "Outbound Disabled"
        OUTBOUND_ENABLED = "OUTBOUND_ENABLED", "Outbound Enabled"
        RATE_LIMIT_BLOCKED = "RATE_LIMIT_BLOCKED", "Rate Limit Block"
        COOLDOWN_BLOCKED = "COOLDOWN_BLOCKED", "Cooldown Block"
        OPT_OUT_BLOCKED = "OPT_OUT_BLOCKED", "Opt-Out Block"
        DUPLICATE_BLOCKED = "DUPLICATE_BLOCKED", "Duplicate Send Block"
        SESSION_FAILURE = "SESSION_FAILURE", "Session Failure"
        CONSECUTIVE_FAILURES = "CONSECUTIVE_FAILURES", "Consecutive Failures"
        ADMIN_EMERGENCY_STOP = "ADMIN_EMERGENCY_STOP", "Admin Emergency Stop"

    event_type = models.CharField(max_length=30, choices=EventType.choices)

    triggered_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_safety_events",
        help_text="The user whose action triggered this event (if applicable).",
    )

    prospect_contact = models.ForeignKey(
        "prospects.ProspectContact",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_safety_events",
    )

    details = models.TextField(
        blank=True,
        null=True,
        help_text="Human-readable description of what triggered this event.",
    )

    # Current consecutive failure count at time of event.
    consecutive_failures_at_event = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "WhatsApp Safety Event"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event_type} at {self.created_at}"


# ---------------------------------------------------------------------------
# WhatsAppAuditLog
# ---------------------------------------------------------------------------

class WhatsAppAuditLog(models.Model):
    """
    Immutable audit trail for all WhatsApp actions.

    Records are never updated or deleted.
    Used for compliance, incident investigation, and Manager reporting.

    IMPORTANT: Do NOT store API keys, tokens, session secrets, or full
    message bodies in this log. Only metadata.
    """

    class Action(models.TextChoices):
        SESSION_STARTED = "SESSION_STARTED", "Session Started"
        SESSION_STOPPED = "SESSION_STOPPED", "Session Stopped"
        QR_REQUESTED = "QR_REQUESTED", "QR Requested"
        QR_SCANNED = "QR_SCANNED", "QR Scanned"
        SESSION_CONNECTED = "SESSION_CONNECTED", "Session Connected"
        SESSION_DISCONNECTED = "SESSION_DISCONNECTED", "Session Disconnected"
        MESSAGE_SENT = "MESSAGE_SENT", "Message Sent"
        MESSAGE_RECEIVED = "MESSAGE_RECEIVED", "Message Received"
        MESSAGE_FAILED = "MESSAGE_FAILED", "Message Failed"
        SEND_BLOCKED = "SEND_BLOCKED", "Send Blocked"
        OPT_OUT_REGISTERED = "OPT_OUT_REGISTERED", "Opt-Out Registered"
        OPT_OUT_REVERSED = "OPT_OUT_REVERSED", "Opt-Out Reversed"
        SAFETY_STOP = "SAFETY_STOP", "Safety Stop"
        ADMIN_RESUMED = "ADMIN_RESUMED", "Admin Resumed"
        CONFIG_CHANGED = "CONFIG_CHANGED", "Config Changed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    action = models.CharField(max_length=30, choices=Action.choices, db_index=True)

    # Who performed the action (null for system-initiated events).
    performed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="whatsapp_audit_logs",
    )
    performed_by_role = models.CharField(max_length=20, blank=True, null=True)

    # What record was affected.
    prospect_id = models.UUIDField(null=True, blank=True)
    prospect_name = models.CharField(max_length=255, blank=True, null=True)
    prospect_contact_id = models.UUIDField(null=True, blank=True)
    prospect_contact_name = models.CharField(max_length=255, blank=True, null=True)

    # WhatsApp-specific context.
    session_name = models.CharField(max_length=100, blank=True, null=True)
    chat_id = models.CharField(max_length=100, blank=True, null=True)
    message_id = models.UUIDField(null=True, blank=True)

    # Result.
    success = models.BooleanField(null=True, blank=True)
    result_code = models.CharField(max_length=50, blank=True, null=True)
    result_detail = models.TextField(blank=True, null=True)

    # Metadata — for request tracing.
    request_id = models.CharField(max_length=100, blank=True, null=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "WhatsApp Audit Log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["action", "created_at"]),
            models.Index(fields=["performed_by", "created_at"]),
            models.Index(fields=["prospect_contact_id"]),
        ]

    def __str__(self):
        user = self.performed_by.username if self.performed_by else "system"
        return f"[{self.action}] by {user} at {self.created_at}"


# ---------------------------------------------------------------------------
# WhatsAppEvent
# ---------------------------------------------------------------------------

class WhatsAppEvent(models.Model):
    """
    Raw webhook event log from WAHA.

    Stores the event type, HMAC verification result, and processing status.
    The full payload is NOT stored by default to avoid inadvertent message
    body logging. Only the event type and key metadata are kept.
    """

    class ProcessingStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSED = "PROCESSED", "Processed"
        FAILED = "FAILED", "Failed"
        REJECTED = "REJECTED", "Rejected (invalid signature/schema)"
        DUPLICATE = "DUPLICATE", "Duplicate (already processed)"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # WAHA event type (e.g., "message", "message.ack", "session.status").
    event_type = models.CharField(max_length=100, db_index=True)

    # WAHA session name.
    session_name = models.CharField(max_length=100, blank=True, null=True)

    # HMAC verification result.
    hmac_valid = models.BooleanField(null=True, blank=True)

    # Processing outcome.
    processing_status = models.CharField(
        max_length=20,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.PENDING,
    )
    processing_error = models.TextField(blank=True, null=True)

    # Deduplication: hash of the event payload.
    payload_hash = models.CharField(max_length=64, blank=True, null=True, db_index=True)

    # Associated WhatsApp message (if this event relates to one).
    whatsapp_message = models.ForeignKey(
        WhatsAppMessage,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="events",
    )

    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "WhatsApp Event"
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["event_type", "received_at"]),
            models.Index(fields=["payload_hash"]),
            models.Index(fields=["processing_status"]),
        ]

    def __str__(self):
        return f"Event: {self.event_type} [{self.processing_status}] at {self.received_at}"
