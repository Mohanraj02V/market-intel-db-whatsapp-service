"""
backend/whatsapp/__init__.py
Market Intel — WhatsApp Integration Module

This module provides a clean, secure integration between the Market Intel
Django backend and the WAHA WhatsApp service running on Oracle Cloud.

Architecture:
    Django (AWS EC2)
        └── whatsapp.client     -- HTTPS calls to WAHA
        └── whatsapp.services   -- Business logic + safety guards
        └── whatsapp.webhooks   -- Incoming WAHA event processing
        └── whatsapp.models     -- WhatsApp integration metadata
        └── whatsapp.views      -- DRF API endpoints
        └── whatsapp.serializers
        └── whatsapp.permissions
        └── whatsapp.urls

Security design:
    - All WAHA calls go through a single authenticated client.
    - API key is server-side only. Never exposed to React.
    - Webhooks are HMAC-verified before any processing.
    - Every outbound send passes through the Safety Guard.
    - Multi-tenancy: company isolation enforced on every query.
    - Role-based: only permitted roles can trigger sends.
"""
