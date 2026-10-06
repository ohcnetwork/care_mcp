"""Mounted by Care at /api/care_mcp/ (config/urls.py loops over PLUGIN_APPS)."""

from django.urls import path

from care_mcp.views import ConfigView, MCPView

urlpatterns = [
    path("mcp/", MCPView.as_view(), name="care-mcp-endpoint"),
    path("config/", ConfigView.as_view(), name="care-mcp-config"),
]
