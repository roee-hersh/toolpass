"""The azure integration: Azure role-based access control for one Entra tenant."""

from __future__ import annotations

from agent_authorizer.integrations.azure.azure import Azure, AzureConnection

__all__ = ["INTEGRATION", "Azure", "AzureConnection"]

INTEGRATION = Azure()
