"""The okta integration: a user's Okta groups, for group.member checks and
as a kubernetes groups_connection."""

from __future__ import annotations

from toolpass.integrations.okta.okta import Okta, OktaConnection

__all__ = ["INTEGRATION", "Okta", "OktaConnection"]

INTEGRATION = Okta()
