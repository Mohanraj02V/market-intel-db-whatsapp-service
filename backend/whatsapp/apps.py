"""
backend/whatsapp/apps.py
Django app configuration for the WhatsApp integration module.
"""
from django.apps import AppConfig


class WhatsAppConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "whatsapp"
    verbose_name = "WhatsApp Integration"
