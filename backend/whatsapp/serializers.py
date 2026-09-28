"""
backend/whatsapp/serializers.py

DRF serializers for the WhatsApp module.

Security principles:
  - Read serializers expose only safe metadata fields.
  - No API keys, session secrets, or credentials are ever serialized.
  - Write serializers validate all inputs strictly.
  - Company isolation is enforced at the view layer (not serializer).
"""

from rest_framework import serializers

from .models import (
    WhatsAppAuditLog,
    WhatsAppConfiguration,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppOptOut,
    WhatsAppSafetyEvent,
    WhatsAppSession,
)


class WhatsAppConfigurationSerializer(serializers.ModelSerializer):
    class Meta:
        model = WhatsAppConfiguration
        fields = [
            "outbound_enabled",
            "safety_stop_active",
            "safety_stop_reason",
            "safety_stop_at",
            "recipient_cooldown_minutes",
            "global_hourly_limit",
            "per_user_hourly_limit",
            "consecutive_failure_threshold",
            "updated_at",
        ]
        read_only_fields = [
            "safety_stop_active",
            "safety_stop_reason",
            "safety_stop_at",
            "updated_at",
        ]

    def validate_recipient_cooldown_minutes(self, value):
        if value < 1 or value > 10080:  # max 7 days
            raise serializers.ValidationError(
                "Cooldown must be between 1 and 10080 minutes (7 days)."
            )
        return value

    def validate_global_hourly_limit(self, value):
        if value < 1 or value > 500:
            raise serializers.ValidationError("Global hourly limit must be between 1 and 500.")
        return value

    def validate_per_user_hourly_limit(self, value):
        if value < 1 or value > 100:
            raise serializers.ValidationError("Per-user hourly limit must be between 1 and 100.")
        return value


class WhatsAppSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WhatsAppSession
        fields = [
            "session_name",
            "status",
            "phone_number",
            "last_health_check_at",
            "last_health_status",
            "send_count",
            "receive_count",
            "failed_send_count",
            "consecutive_failed_sends",
            "disconnect_count",
            "reconnect_count",
            "outbound_block_count",
            "cooldown_block_count",
            "opt_out_count",
            "last_successful_message_at",
            "last_failed_message_at",
            # Do NOT expose last_failed_message_error (may contain sensitive WAHA details).
            "started_at",
            "updated_at",
        ]
        read_only_fields = fields


class WhatsAppConversationSerializer(serializers.ModelSerializer):
    contact_name = serializers.SerializerMethodField()
    prospect_name = serializers.SerializerMethodField()

    class Meta:
        model = WhatsAppConversation
        fields = [
            "id",
            "chat_id",
            "is_matched",
            "contact_name",
            "prospect_name",
            "unread_count",
            "last_message_at",
            "last_message_preview",
            "last_message_from_me",
            "updated_at",
        ]
        read_only_fields = fields

    def get_contact_name(self, obj):
        if obj.prospect_contact:
            return obj.prospect_contact.contact_name
        return None

    def get_prospect_name(self, obj):
        if obj.prospect:
            return obj.prospect.company_name
        return None


class WhatsAppMessageSerializer(serializers.ModelSerializer):
    performed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = WhatsAppMessage
        fields = [
            "id",
            "direction",
            "status",
            "body_preview",
            "message_type",
            "performed_by_name",
            "waha_timestamp",
            "sent_at",
            "delivered_at",
            "read_at",
            "created_at",
        ]
        read_only_fields = fields

    def get_performed_by_name(self, obj):
        if obj.performed_by:
            return obj.performed_by.get_full_name() or obj.performed_by.username
        return None


class SendMessageSerializer(serializers.Serializer):
    """
    Input serializer for outbound WhatsApp message sends.

    Requires explicit prospect_contact_id — no generic "to" + "message" endpoint.
    The backend resolves and validates the recipient from the Market Intel record.
    """
    prospect_contact_id = serializers.UUIDField(
        help_text="UUID of the ProspectContact (Key Person) to message."
    )
    message_body = serializers.CharField(
        max_length=4096,
        min_length=1,
        trim_whitespace=True,
        help_text="Plain-text message body.",
    )
    session_name = serializers.CharField(
        default="default",
        max_length=100,
        required=False,
    )

    def validate_message_body(self, value):
        stripped = value.strip()
        if not stripped:
            raise serializers.ValidationError("Message body cannot be empty or whitespace only.")
        # Reject suspiciously long messages.
        if len(stripped) > 4096:
            raise serializers.ValidationError("Message exceeds 4096 character limit.")
        return stripped


class WhatsAppOptOutSerializer(serializers.ModelSerializer):
    contact_name = serializers.SerializerMethodField()

    class Meta:
        model = WhatsAppOptOut
        fields = [
            "id",
            "phone_digits",
            "reason",
            "is_active",
            "contact_name",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_contact_name(self, obj):
        if obj.prospect_contact:
            return obj.prospect_contact.contact_name
        return None


class WhatsAppSafetyEventSerializer(serializers.ModelSerializer):
    triggered_by_name = serializers.SerializerMethodField()
    contact_name = serializers.SerializerMethodField()

    class Meta:
        model = WhatsAppSafetyEvent
        fields = [
            "id",
            "event_type",
            "triggered_by_name",
            "contact_name",
            "details",
            "consecutive_failures_at_event",
            "created_at",
        ]
        read_only_fields = fields

    def get_triggered_by_name(self, obj):
        if obj.triggered_by:
            return obj.triggered_by.get_full_name() or obj.triggered_by.username
        return "System"

    def get_contact_name(self, obj):
        if obj.prospect_contact:
            return obj.prospect_contact.contact_name
        return None


class WhatsAppAuditLogSerializer(serializers.ModelSerializer):
    performed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = WhatsAppAuditLog
        fields = [
            "id",
            "action",
            "performed_by_name",
            "performed_by_role",
            "prospect_name",
            "prospect_contact_name",
            "session_name",
            "chat_id",
            "success",
            "result_code",
            "result_detail",
            "created_at",
        ]
        read_only_fields = fields

    def get_performed_by_name(self, obj):
        if obj.performed_by:
            return obj.performed_by.get_full_name() or obj.performed_by.username
        return "System"


class ResetSafetyStopSerializer(serializers.Serializer):
    """Input for Super Admin safety stop reset."""
    reason = serializers.CharField(
        max_length=1000,
        required=True,
        help_text="Required: explanation of why the safety stop is being reset.",
    )

    def validate_reason(self, value):
        stripped = value.strip()
        if len(stripped) < 10:
            raise serializers.ValidationError(
                "Please provide a meaningful reason (at least 10 characters)."
            )
        return stripped


class SetOutboundEnabledSerializer(serializers.Serializer):
    """Input for enabling/disabling the global outbound kill switch."""
    enabled = serializers.BooleanField()
    reason = serializers.CharField(max_length=500, required=False, default="")
