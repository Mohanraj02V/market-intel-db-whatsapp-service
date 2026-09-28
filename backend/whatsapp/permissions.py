"""
backend/whatsapp/permissions.py

Role-based permission classes for the WhatsApp module.

Mapped to existing Market Intel roles:
  SuperAdmin (is_superuser)  → full access
  MANAGER                    → read-only + monitoring + config (no sends)
  LQ                         → send + read (their authorized contacts only)
  PRE                        → no WhatsApp access

Do not create permissions casually.
Every permission class must have an explicit justification.
"""

from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsSuperAdmin(BasePermission):
    """Super Admin only (Django is_superuser)."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


class CanSendWhatsApp(BasePermission):
    """
    Only LQ users and Super Admin can initiate WhatsApp sends.

    MANAGER cannot send messages — they can only view/audit.
    PRE cannot send — they have no WhatsApp role.
    """

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_superuser:
            return True
        profile = getattr(request.user, "profile", None)
        return bool(profile and profile.role == "LQ")


class CanViewWhatsApp(BasePermission):
    """
    LQ, Manager, and Super Admin can view WhatsApp conversations and messages.

    PRE cannot view WhatsApp data.
    """

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_superuser:
            return True
        profile = getattr(request.user, "profile", None)
        return bool(profile and profile.role in ("LQ", "MANAGER"))


class CanManageWhatsAppSession(BasePermission):
    """
    Only Super Admin can start/stop/logout WhatsApp sessions.

    Sessions are sensitive — unauthorized session actions could log out
    the shared business WhatsApp account.
    """

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.method in SAFE_METHODS:
            # Viewing session status is allowed for MANAGER and LQ.
            profile = getattr(request.user, "profile", None)
            return bool(
                request.user.is_superuser or
                (profile and profile.role in ("LQ", "MANAGER"))
            )
        # Write operations (start/stop/logout) require Super Admin.
        return request.user.is_superuser


class CanManageWhatsAppConfig(BasePermission):
    """
    Manager and Super Admin can change WhatsApp configuration.
    (e.g., enable/disable outbound, change rate limits)
    """

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_superuser:
            return True
        profile = getattr(request.user, "profile", None)
        if not profile:
            return False
        if request.method in SAFE_METHODS:
            return profile.role in ("LQ", "MANAGER")
        return profile.role == "MANAGER"


class CanResetSafetyStop(BasePermission):
    """
    Only Super Admin can reset the Safety Guard stop.

    This is a deliberate barrier — resuming after a safety stop
    requires conscious Super Admin review, not just Manager action.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.is_superuser)


class CanViewAuditLog(BasePermission):
    """Manager and Super Admin can view the WhatsApp audit log."""

    def has_permission(self, request, view):
        if not (request.user and request.user.is_authenticated):
            return False
        if request.user.is_superuser:
            return True
        profile = getattr(request.user, "profile", None)
        return bool(profile and profile.role == "MANAGER")
