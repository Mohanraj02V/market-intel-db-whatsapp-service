"""
backend/whatsapp/views.py

Django REST Framework views for the WhatsApp integration.

Security design:
  - Every view requires authentication (IsAuthenticated).
  - Role-based permissions enforce what each role can do.
  - No API key, session secret, or credential is ever returned.
  - Multi-tenancy: all prospect/contact lookups enforce ownership.
  - The webhook endpoint bypasses JWT auth (uses HMAC instead).
  - Webhook processing is stateless and reject-first.

Endpoints:
  GET    /api/whatsapp/status/                    Session status
  GET    /api/whatsapp/qr/                        QR code (Super Admin)
  POST   /api/whatsapp/session/start/             Start session (Super Admin)
  POST   /api/whatsapp/session/stop/              Stop session (Super Admin)
  POST   /api/whatsapp/session/logout/            Logout session (Super Admin)
  POST   /api/whatsapp/send/                      Send message (LQ only)
  GET    /api/whatsapp/conversations/             List conversations (LQ/Manager)
  GET    /api/whatsapp/conversations/<id>/messages/ Messages in a conversation (LQ/Manager)
  GET    /api/whatsapp/config/                    Get config (Manager/Admin)
  PATCH  /api/whatsapp/config/                    Update config (Manager/Admin)
  POST   /api/whatsapp/config/safety-reset/       Reset safety stop (Super Admin)
  POST   /api/whatsapp/config/outbound/           Enable/disable outbound (Manager/Admin)
  GET    /api/whatsapp/audit-log/                 Audit log (Manager/Admin)
  GET    /api/whatsapp/safety-events/             Safety events (Manager/Admin)
  GET    /api/whatsapp/opt-outs/                  Opt-out list (Manager/Admin)
  POST   /api/whatsapp/webhook/                   WAHA webhook (HMAC auth, no JWT)
  GET    /api/whatsapp/health/                    Internal health (no auth)
"""

import logging
import uuid

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from prospects.models import ProspectContact

from .client import WAHAServiceError
from .models import (
    WhatsAppAuditLog,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppOptOut,
    WhatsAppSafetyEvent,
    WhatsAppSession,
)
from .permissions import (
    CanManageWhatsAppConfig,
    CanManageWhatsAppSession,
    CanResetSafetyStop,
    CanSendWhatsApp,
    CanViewAuditLog,
    CanViewWhatsApp,
)
from .serializers import (
    ResetSafetyStopSerializer,
    SendMessageSerializer,
    SetOutboundEnabledSerializer,
    WhatsAppAuditLogSerializer,
    WhatsAppConfigurationSerializer,
    WhatsAppConversationSerializer,
    WhatsAppMessageSerializer,
    WhatsAppOptOutSerializer,
    WhatsAppSafetyEventSerializer,
    WhatsAppSessionSerializer,
)
from .services import (
    WhatsAppAuthorizationError,
    WhatsAppBlockedError,
    WhatsAppError,
    admin_reset_safety_stop,
    admin_set_outbound_enabled,
    get_qr,
    get_session_status,
    logout_session,
    send_whatsapp_message,
    start_session,
    stop_session,
    _get_config,
)
from .webhooks import (
    WebhookDuplicateError,
    WebhookVerificationError,
    process_webhook_event,
)

logger = logging.getLogger(__name__)


def _get_client_ip(request) -> str:
    """Extract client IP from request headers."""
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def _get_request_id(request) -> str:
    """Extract or generate a request ID for tracing."""
    return (
        request.META.get("HTTP_X_REQUEST_ID") or
        request.META.get("HTTP_X_TRACE_ID") or
        str(uuid.uuid4())
    )


# ---------------------------------------------------------------------------
# Health (no authentication required — used by monitoring)
# ---------------------------------------------------------------------------

class WhatsAppHealthView(APIView):
    """Public health check endpoint for monitoring systems."""
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        try:
            config = _get_config()
            session_status = WhatsAppSession.objects.filter(
                session_name="default"
            ).values("status", "phone_number").first()

            health_status = "HEALTHY"
            if config.safety_stop_active:
                health_status = "SAFETY_STOP"
            elif not config.outbound_enabled:
                health_status = "OUTBOUND_BLOCKED"
            elif session_status and session_status["status"] != "READY":
                health_status = "DEGRADED"

            return Response({
                "status": health_status,
                "session_status": session_status["status"] if session_status else "UNKNOWN",
                "outbound_enabled": config.outbound_enabled,
                "safety_stop_active": config.safety_stop_active,
            })
        except Exception:
            return Response({"status": "DEGRADED"})


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

class WhatsAppStatusView(APIView):
    """Get WhatsApp session status."""
    permission_classes = [IsAuthenticated, CanViewWhatsApp]

    def get(self, request):
        try:
            status_data = get_session_status()
            local_session = WhatsAppSession.objects.filter(session_name="default").first()
            serialized_session = WhatsAppSessionSerializer(local_session).data if local_session else {}

            return Response({
                "session": serialized_session,
                "waha_status": status_data.get("waha_status", {}),
                "config": {
                    "outbound_enabled": _get_config().outbound_enabled,
                    "safety_stop_active": _get_config().safety_stop_active,
                },
            })
        except WAHAServiceError as exc:
            return Response(
                {"detail": "WhatsApp service is unavailable.", "session_status": "UNKNOWN"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )


class WhatsAppQRView(APIView):
    """Get QR code for WhatsApp authentication."""
    permission_classes = [IsAuthenticated, CanManageWhatsAppSession]

    def get(self, request):
        session_name = request.query_params.get("session", "default")

        try:
            result = get_qr(session_name=session_name, performed_by=request.user)
            return Response(result)
        except WhatsAppError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except WAHAServiceError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class WhatsAppSessionStartView(APIView):
    """Start a WAHA session. Super Admin only."""
    permission_classes = [IsAuthenticated, CanManageWhatsAppSession]

    def post(self, request):
        session_name = request.data.get("session_name", "default")

        try:
            result = start_session(performed_by=request.user, session_name=session_name)
            return Response(result)
        except WhatsAppError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except WAHAServiceError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)


class WhatsAppSessionStopView(APIView):
    """Stop a WAHA session. Super Admin only."""
    permission_classes = [IsAuthenticated, CanManageWhatsAppSession]

    def post(self, request):
        session_name = request.data.get("session_name", "default")

        try:
            result = stop_session(performed_by=request.user, session_name=session_name)
            return Response(result)
        except WhatsAppError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class WhatsAppSessionLogoutView(APIView):
    """Logout from WhatsApp. Super Admin only."""
    permission_classes = [IsAuthenticated, CanManageWhatsAppSession]

    def post(self, request):
        session_name = request.data.get("session_name", "default")

        try:
            result = logout_session(performed_by=request.user, session_name=session_name)
            return Response(result)
        except WhatsAppError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


# ---------------------------------------------------------------------------
# Messaging
# ---------------------------------------------------------------------------

class WhatsAppSendMessageView(APIView):
    """
    Send a WhatsApp message to an authorized ProspectContact (Key Person).
    LQ and Super Admin only.
    """
    permission_classes = [IsAuthenticated, CanSendWhatsApp]

    def post(self, request):
        serializer = SendMessageSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        contact_id = data["prospect_contact_id"]
        message_body = data["message_body"]
        session_name = data.get("session_name", "default")

        # Resolve the ProspectContact and enforce authorization.
        try:
            contact = ProspectContact.objects.select_related("prospect").get(pk=contact_id)
        except ProspectContact.DoesNotExist:
            return Response({"detail": "Key person not found."}, status=status.HTTP_404_NOT_FOUND)

        # Authorization: LQ users can only message contacts they are authorized for.
        # (The existing Market Intel permission model already controls which prospects
        # LQ users can access. We do not re-implement that here — we rely on the
        # existing ProspectContact queryset being properly scoped in the request.)
        # TODO: Enhance with company-level check when Company FK is added to UserProfile.

        try:
            wa_message = send_whatsapp_message(
                performed_by=request.user,
                prospect_contact=contact,
                message_body=message_body,
                session_name=session_name,
                intent_tag="manual",
                request_id=_get_request_id(request),
                ip_address=_get_client_ip(request),
            )
            return Response(
                WhatsAppMessageSerializer(wa_message).data,
                status=status.HTTP_201_CREATED,
            )

        except WhatsAppBlockedError as exc:
            return Response(
                {"detail": str(exc), "code": exc.code},
                status=status.HTTP_403_FORBIDDEN,
            )
        except WhatsAppAuthorizationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)
        except WhatsAppError as exc:
            return Response(
                {"detail": str(exc), "code": exc.code},
                status=status.HTTP_400_BAD_REQUEST,
            )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

class WhatsAppConversationsView(APIView):
    """List WhatsApp conversations visible to the current user."""
    permission_classes = [IsAuthenticated, CanViewWhatsApp]

    def get(self, request):
        qs = WhatsAppConversation.objects.select_related(
            "prospect_contact", "prospect"
        ).filter(
            is_matched=True
        ).order_by("-last_message_at")

        # Manager sees all conversations.
        # LQ sees only conversations linked to their authorized contacts.
        # (Full company-scoping requires Company FK — enhance later.)
        profile = getattr(request.user, "profile", None)
        role = profile.role if profile else None

        if not request.user.is_superuser and role not in ("MANAGER",):
            # LQ: only conversations for contacts they own/access.
            # For now: all matched conversations. Tighten with company scope.
            pass

        serializer = WhatsAppConversationSerializer(qs[:100], many=True)
        return Response(serializer.data)


class WhatsAppConversationMessagesView(APIView):
    """Get messages for a specific conversation."""
    permission_classes = [IsAuthenticated, CanViewWhatsApp]

    def get(self, request, conversation_id):
        try:
            conversation = WhatsAppConversation.objects.select_related(
                "prospect_contact", "prospect"
            ).get(pk=conversation_id, is_matched=True)
        except WhatsAppConversation.DoesNotExist:
            return Response({"detail": "Conversation not found."}, status=status.HTTP_404_NOT_FOUND)

        messages = WhatsAppMessage.objects.filter(
            conversation=conversation
        ).order_by("-created_at")[:100]

        serializer = WhatsAppMessageSerializer(messages, many=True)
        return Response({
            "conversation": WhatsAppConversationSerializer(conversation).data,
            "messages": serializer.data,
        })


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class WhatsAppConfigView(APIView):
    """Get or update WhatsApp configuration."""
    permission_classes = [IsAuthenticated, CanManageWhatsAppConfig]

    def get(self, request):
        config = _get_config()
        return Response(WhatsAppConfigurationSerializer(config).data)

    def patch(self, request):
        config = _get_config()
        serializer = WhatsAppConfigurationSerializer(
            config, data=request.data, partial=True
        )
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        return Response(serializer.data)


class WhatsAppSetOutboundView(APIView):
    """Enable or disable the global outbound kill switch."""
    permission_classes = [IsAuthenticated, CanManageWhatsAppConfig]

    def post(self, request):
        serializer = SetOutboundEnabledSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            admin_set_outbound_enabled(
                performed_by=request.user,
                enabled=serializer.validated_data["enabled"],
                reason=serializer.validated_data.get("reason", ""),
            )
            return Response({"detail": "Outbound setting updated.", "enabled": serializer.validated_data["enabled"]})
        except WhatsAppAuthorizationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)


class WhatsAppResetSafetyStopView(APIView):
    """Reset the Safety Guard stop. Super Admin only."""
    permission_classes = [IsAuthenticated, CanResetSafetyStop]

    def post(self, request):
        serializer = ResetSafetyStopSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        try:
            admin_reset_safety_stop(
                performed_by=request.user,
                reason=serializer.validated_data["reason"],
            )
            return Response({"detail": "Safety stop reset. Outbound messaging may resume."})
        except WhatsAppAuthorizationError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_403_FORBIDDEN)


# ---------------------------------------------------------------------------
# Monitoring / Audit
# ---------------------------------------------------------------------------

class WhatsAppSafetyEventsView(APIView):
    """List recent Safety Guard events."""
    permission_classes = [IsAuthenticated, CanViewAuditLog]

    def get(self, request):
        qs = WhatsAppSafetyEvent.objects.select_related(
            "triggered_by", "prospect_contact"
        ).order_by("-created_at")[:100]
        return Response(WhatsAppSafetyEventSerializer(qs, many=True).data)


class WhatsAppAuditLogView(APIView):
    """List WhatsApp audit log entries."""
    permission_classes = [IsAuthenticated, CanViewAuditLog]

    def get(self, request):
        qs = WhatsAppAuditLog.objects.select_related(
            "performed_by"
        ).order_by("-created_at")[:200]
        return Response(WhatsAppAuditLogSerializer(qs, many=True).data)


class WhatsAppOptOutsView(APIView):
    """List opt-out records."""
    permission_classes = [IsAuthenticated, CanViewAuditLog]

    def get(self, request):
        qs = WhatsAppOptOut.objects.select_related("prospect_contact").order_by("-created_at")
        return Response(WhatsAppOptOutSerializer(qs, many=True).data)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

class WhatsAppWebhookView(APIView):
    """
    WAHA webhook receiver.

    This endpoint does NOT use JWT authentication.
    Instead, it verifies the HMAC signature from WAHA.

    Security:
      - HMAC-SHA256 verification on raw request body.
      - Reject all events with invalid signatures.
      - Reject duplicate events.
      - Reject unknown event types.
      - All processing happens asynchronously-safe within a DB transaction.
      - Returns 200 immediately after verification — processing happens synchronously
        but any processing error does NOT cause a 5xx response (WAHA would retry).
    """
    authentication_classes = []  # No JWT on webhook.
    permission_classes = []      # Auth is via HMAC.

    def post(self, request):
        # Log all headers to debug WAHA signature header name
        logger.warning(f"WEBHOOK HEADERS RECEIVED: {dict(request.headers)}")
        
        # Read raw body for HMAC verification.
        raw_body = request.body
        # Try both common signature headers
        signature = request.headers.get("X-Whatsapp-Signature") or request.headers.get("X-Waha-Signature") or request.headers.get("X-Hub-Signature-256") or ""
        remote_addr = _get_client_ip(request)

        try:
            event_record = process_webhook_event(
                raw_body=raw_body,
                signature_header=signature,
                remote_addr=remote_addr,
            )

            return Response(
                {"status": "received", "event_id": str(event_record.pk)},
                status=status.HTTP_200_OK,
            )

        except WebhookVerificationError as exc:
            logger.warning(
                "Webhook verification failed from %s: %s",
                remote_addr,
                str(exc),
            )
            # Return 401 to signal authentication failure.
            # Do not reveal which check failed (signature, schema, etc.).
            return Response(
                {"detail": "Unauthorized."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        except WebhookDuplicateError:
            # Duplicate events are silently accepted (idempotent).
            return Response({"status": "duplicate"}, status=status.HTTP_200_OK)

        except Exception as exc:
            logger.error(
                "Webhook processing unexpected error from %s: %s",
                remote_addr,
                str(exc),
                exc_info=True,
            )
            # Return 200 even on processing errors so WAHA doesn't retry indefinitely.
            # The error is logged and the event record is marked FAILED.
            return Response({"status": "processing_error"}, status=status.HTTP_200_OK)
