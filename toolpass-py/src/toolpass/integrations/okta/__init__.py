"""The okta integration: a user's Okta groups, for group.member checks and
as a groups_connection of the integrations that grant by group."""

from __future__ import annotations

from toolpass.integrations.okta.okta import Okta, OktaConnection

__all__ = ["INTEGRATION", "Okta", "OktaConnection"]

INTEGRATION = Okta()
