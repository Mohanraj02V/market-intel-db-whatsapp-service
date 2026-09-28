"""
backend/whatsapp/urls.py

URL patterns for the WhatsApp integration module.

Register in config/urls.py with:
    path('api/whatsapp/', include('whatsapp.urls')),

All endpoints require authentication EXCEPT:
  - /webhook/  (HMAC-authenticated)
  - /health/   (monitoring, no auth)
"""

from django.urls import path

from .views import (
    WhatsAppAuditLogView,
    WhatsAppConfigView,
    WhatsAppConversationMessagesView,
    WhatsAppConversationsView,
    WhatsAppHealthView,
    WhatsAppOptOutsView,
    WhatsAppQRView,
    WhatsAppResetSafetyStopView,
    WhatsAppSafetyEventsView,
    WhatsAppSendMessageView,
    WhatsAppSessionLogoutView,
    WhatsAppSessionStartView,
    WhatsAppSessionStopView,
    WhatsAppSetOutboundView,
    WhatsAppStatusView,
    WhatsAppWebhookView,
)

urlpatterns = [
    # -- Health (no auth) --------------------------------------------------
    path("health/", WhatsAppHealthView.as_view(), name="whatsapp-health"),

    # -- Webhook (HMAC auth, no JWT) ----------------------------------------
    path("webhook/", WhatsAppWebhookView.as_view(), name="whatsapp-webhook"),

    # -- Session management (Super Admin) ------------------------------------
    path("status/", WhatsAppStatusView.as_view(), name="whatsapp-status"),
    path("qr/", WhatsAppQRView.as_view(), name="whatsapp-qr"),
    path("session/start/", WhatsAppSessionStartView.as_view(), name="whatsapp-session-start"),
    path("session/stop/", WhatsAppSessionStopView.as_view(), name="whatsapp-session-stop"),
    path("session/logout/", WhatsAppSessionLogoutView.as_view(), name="whatsapp-session-logout"),

    # -- Messaging (LQ / Super Admin) ----------------------------------------
    path("send/", WhatsAppSendMessageView.as_view(), name="whatsapp-send"),

    # -- Conversations (LQ / Manager) ----------------------------------------
    path("conversations/", WhatsAppConversationsView.as_view(), name="whatsapp-conversations"),
    path(
        "conversations/<uuid:conversation_id>/messages/",
        WhatsAppConversationMessagesView.as_view(),
        name="whatsapp-conversation-messages",
    ),

    # -- Configuration (Manager / Super Admin) --------------------------------
    path("config/", WhatsAppConfigView.as_view(), name="whatsapp-config"),
    path("config/outbound/", WhatsAppSetOutboundView.as_view(), name="whatsapp-outbound"),
    path(
        "config/safety-reset/",
        WhatsAppResetSafetyStopView.as_view(),
        name="whatsapp-safety-reset",
    ),

    # -- Monitoring / Audit (Manager / Super Admin) ---------------------------
    path("safety-events/", WhatsAppSafetyEventsView.as_view(), name="whatsapp-safety-events"),
    path("audit-log/", WhatsAppAuditLogView.as_view(), name="whatsapp-audit-log"),
    path("opt-outs/", WhatsAppOptOutsView.as_view(), name="whatsapp-opt-outs"),
]
