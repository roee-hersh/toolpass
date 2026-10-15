"""Slack Web API requests, mapped to the slack integration's actions.

Every method is one path (``/api/chat.postMessage``); the SDKs send POST
with a JSON or form body, so the channel comes from the body (or the
query). The engine checks channels by id (``channel:C0123456789``): a
request that names a channel another way (``#general``, a direct message
``D…``) is refused with a message asking for the id.
"""

from __future__ import annotations

from collections.abc import Mapping

from toolpass.core.routes import Extractor, Request, Route, RouteTable
from toolpass.integrations.slack.actions import CHANNEL_ID_RE

__all__ = ["ROUTES"]

_CHANNEL = "channel:{channel}"
_WORKSPACE = "workspace"


def _channel(*names: str) -> Extractor:
    """An extractor reading the channel id from one of the named fields."""

    def extract(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
        fields = r.fields()
        for name in names:
            raw = fields.get(name, "")
            if not raw:
                continue
            ids = [c.strip() for c in raw.split(",") if c.strip()]
            if len(ids) != 1:
                return {"_unresolved": "name one channel per request, by id"}
            if not CHANNEL_ID_RE.fullmatch(ids[0]):
                return {"_unresolved": f"channel {ids[0]!r} must be a channel id such as C0123456789, not a name or a direct message"}
            return {"channel": ids[0]}
        return {"_unresolved": "the request names no channel"}

    return extract


def _post(r: Request, vars_: Mapping[str, str]) -> dict[str, str]:
    out = dict(_channel("channel")(r, vars_) or {})
    if r.fields().get("thread_ts"):
        out["_action"] = "message.post_thread"
    return out


def _routes() -> list[Route]:
    ch = _channel("channel")
    out: list[Route] = [
        # Messages.
        Route("*", "/chat.postMessage", "message.post", _CHANNEL, effect="write", sends_out=True, body=_post),
        Route("*", "/chat.postEphemeral", "message.post", _CHANNEL, effect="write", sends_out=True, body=ch),
        Route("*", "/chat.scheduleMessage", "message.post", _CHANNEL, effect="write", sends_out=True, body=ch),
        Route("*", "/chat.update", "message.post", _CHANNEL, effect="write", sends_out=True, body=ch),
        Route("*", "/chat.meMessage", "message.post", _CHANNEL, effect="write", sends_out=True, body=ch),
        Route("*", "/chat.unfurl", "message.post", _CHANNEL, effect="write", sends_out=True, body=ch),
        Route("*", "/chat.delete", "message.post", _CHANNEL, effect="destructive", body=ch),
        Route("*", "/chat.deleteScheduledMessage", "message.post", _CHANNEL, effect="destructive", body=ch),
        Route("*", "/chat.getPermalink", "channel.read", _CHANNEL, effect="read", body=ch),
        Route("*", "/chat.scheduledMessages.list", "user.active", _WORKSPACE, effect="read"),
        # Conversations.
        Route("*", "/conversations.history", "channel.read", _CHANNEL, effect="read", body=ch),
        Route("*", "/conversations.replies", "channel.read", _CHANNEL, effect="read", body=ch),
        Route("*", "/conversations.info", "channel.read", _CHANNEL, effect="read", body=ch),
        Route("*", "/conversations.members", "channel.read", _CHANNEL, effect="read", body=ch),
        Route("*", "/conversations.mark", "channel.read", _CHANNEL, effect="write", body=ch),
        Route("*", "/conversations.leave", "channel.read", _CHANNEL, effect="write", body=ch),
        Route("*", "/conversations.close", "channel.read", _CHANNEL, effect="write", body=ch),
        Route("*", "/conversations.join", "channel.join", _CHANNEL, effect="write", body=ch),
        Route("*", "/conversations.invite", "channel.invite", _CHANNEL, effect="write", body=ch),
        Route("*", "/conversations.kick", "channel.invite", _CHANNEL, effect="destructive", body=ch),
        Route("*", "/conversations.archive", "channel.archive", _CHANNEL, effect="destructive", body=ch),
        Route("*", "/conversations.unarchive", "channel.archive", _CHANNEL, effect="write", body=ch),
        Route("*", "/conversations.rename", "channel.rename", _CHANNEL, effect="write", body=ch),
        Route("*", "/conversations.setTopic", "channel.rename", _CHANNEL, effect="write", body=ch),
        Route("*", "/conversations.setPurpose", "channel.rename", _CHANNEL, effect="write", body=ch),
        Route("*", "/conversations.create", "channel.create", _WORKSPACE, effect="write"),
        Route("*", "/conversations.open", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/conversations.list", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/users.conversations", "user.active", _WORKSPACE, effect="read"),
        # Files.
        Route("*", "/files.upload", "file.upload", _CHANNEL, effect="write", sends_out=True, body=_channel("channels", "channel")),
        Route(
            "*", "/files.completeUploadExternal", "file.upload", _CHANNEL, effect="write", sends_out=True, body=_channel("channel_id", "channel"), spec=False
        ),
        Route(
            "*",
            "/files.getUploadURLExternal",
            None,
            _WORKSPACE,
            effect="write",
            untrusted_output=False,
            description="reserve an upload slot; the upload is checked at files.completeUploadExternal",
            spec=False,
        ),
        Route("*", "/files.info", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/files.list", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/files.delete", "user.active", _WORKSPACE, effect="destructive"),
        Route("*", "/files.comments.delete", "user.active", _WORKSPACE, effect="destructive"),
        Route("*", "/files.revokePublicURL", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/files.sharedPublicURL", "user.active", _WORKSPACE, effect="write", sends_out=True),
        Route("*", "/files.remote.add", "user.active", _WORKSPACE, effect="write", sends_out=True),
        Route("*", "/files.remote.update", "user.active", _WORKSPACE, effect="write", sends_out=True),
        Route("*", "/files.remote.remove", "user.active", _WORKSPACE, effect="destructive"),
        Route("*", "/files.remote.share", "user.active", _WORKSPACE, effect="write", sends_out=True),
        Route("*", "/files.remote.info", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/files.remote.list", "user.active", _WORKSPACE, effect="read"),
        # Pins, reactions, stars, reminders: a member's own actions in a channel.
        Route("*", "/pins.add", "channel.read", _CHANNEL, effect="write", body=ch),
        Route("*", "/pins.remove", "channel.read", _CHANNEL, effect="write", body=ch),
        Route("*", "/pins.list", "channel.read", _CHANNEL, effect="read", body=ch),
        Route("*", "/reactions.add", "channel.read", _CHANNEL, effect="write", body=ch),
        Route("*", "/reactions.remove", "channel.read", _CHANNEL, effect="write", body=ch),
        Route("*", "/reactions.get", "channel.read", _CHANNEL, effect="read", body=ch),
        Route("*", "/reactions.list", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/stars.add", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/stars.remove", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/stars.list", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/reminders.add", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/reminders.complete", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/reminders.delete", "user.active", _WORKSPACE, effect="destructive"),
        Route("*", "/reminders.info", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/reminders.list", "user.active", _WORKSPACE, effect="read"),
        # Users, the workspace, search.
        Route("*", "/users.info", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/users.list", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/users.lookupByEmail", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/users.getPresence", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/users.identity", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/users.profile.get", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/users.profile.set", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/users.setPresence", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/users.setActive", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/users.setPhoto", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/users.deletePhoto", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/dnd.info", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/dnd.teamInfo", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/dnd.setSnooze", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/dnd.endSnooze", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/dnd.endDnd", "user.active", _WORKSPACE, effect="write"),
        Route("*", "/auth.test", None, _WORKSPACE, effect="read", untrusted_output=False, description="the bot's own identity"),
        Route("*", "/api.test", None, _WORKSPACE, effect="read", untrusted_output=False),
        Route("*", "/bots.info", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/emoji.list", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/team.info", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/team.profile.get", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/team.accessLogs", "workspace.admin", _WORKSPACE, effect="read"),
        Route("*", "/team.billableInfo", "workspace.admin", _WORKSPACE, effect="read"),
        Route("*", "/team.integrationLogs", "workspace.admin", _WORKSPACE, effect="read"),
        Route("*", "/search.messages", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/usergroups.list", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/usergroups.users.list", "user.active", _WORKSPACE, effect="read"),
        Route("*", "/usergroups.create", "workspace.admin", _WORKSPACE, effect="write"),
        Route("*", "/usergroups.update", "workspace.admin", _WORKSPACE, effect="write"),
        Route("*", "/usergroups.enable", "workspace.admin", _WORKSPACE, effect="write"),
        Route("*", "/usergroups.disable", "workspace.admin", _WORKSPACE, effect="write"),
        Route("*", "/usergroups.users.update", "workspace.admin", _WORKSPACE, effect="write"),
    ]
    # Enterprise Grid administration: every admin.* method needs an org admin.
    for method in _ADMIN_METHODS:
        effect = (
            "read"
            if method.endswith(
                (".list", ".info", ".search", ".getConversationPrefs", ".getTeams", ".listOriginalConnectedChannelInfo", ".listGroups", ".listChannels")
            )
            else "write"
        )
        if method.endswith((".delete", ".remove")):
            effect = "destructive"
        out.append(Route("*", "/" + method, "org.admin", _WORKSPACE, effect=effect))
    return out


_ADMIN_METHODS = (
    "admin.apps.approve",
    "admin.apps.approved.list",
    "admin.apps.requests.list",
    "admin.apps.restrict",
    "admin.apps.restricted.list",
    "admin.conversations.archive",
    "admin.conversations.convertToPrivate",
    "admin.conversations.create",
    "admin.conversations.delete",
    "admin.conversations.disconnectShared",
    "admin.conversations.ekm.listOriginalConnectedChannelInfo",
    "admin.conversations.getConversationPrefs",
    "admin.conversations.getTeams",
    "admin.conversations.invite",
    "admin.conversations.rename",
    "admin.conversations.restrictAccess.addGroup",
    "admin.conversations.restrictAccess.listGroups",
    "admin.conversations.restrictAccess.removeGroup",
    "admin.conversations.search",
    "admin.conversations.setConversationPrefs",
    "admin.conversations.setTeams",
    "admin.conversations.unarchive",
    "admin.emoji.add",
    "admin.emoji.addAlias",
    "admin.emoji.list",
    "admin.emoji.remove",
    "admin.emoji.rename",
    "admin.inviteRequests.approve",
    "admin.inviteRequests.approved.list",
    "admin.inviteRequests.denied.list",
    "admin.inviteRequests.deny",
    "admin.inviteRequests.list",
    "admin.teams.admins.list",
    "admin.teams.create",
    "admin.teams.list",
    "admin.teams.owners.list",
    "admin.teams.settings.info",
    "admin.teams.settings.setDefaultChannels",
    "admin.teams.settings.setDescription",
    "admin.teams.settings.setDiscoverability",
    "admin.teams.settings.setIcon",
    "admin.teams.settings.setName",
    "admin.usergroups.addChannels",
    "admin.usergroups.addTeams",
    "admin.usergroups.listChannels",
    "admin.usergroups.removeChannels",
    "admin.users.assign",
    "admin.users.invite",
    "admin.users.list",
    "admin.users.remove",
    "admin.users.session.invalidate",
    "admin.users.session.reset",
    "admin.users.setAdmin",
    "admin.users.setExpiration",
    "admin.users.setOwner",
    "admin.users.setRegular",
)

ROUTES = RouteTable(
    "slack",
    _routes(),
    claims=("/",),
    unrouted={
        **{
            ("*", "/" + m): "Calls API: app-level, no user action to check"
            for m in ("calls.add", "calls.end", "calls.info", "calls.participants.add", "calls.participants.remove", "calls.update")
        },
        **{("*", "/" + m): "interactive UI: app-level" for m in ("dialog.open", "views.open", "views.publish", "views.push", "views.update")},
        **{("*", "/" + m): "workflow steps: app-level" for m in ("workflows.stepCompleted", "workflows.stepFailed", "workflows.updateStep")},
        **{
            ("*", "/" + m): "app management: app-level"
            for m in (
                "apps.event.authorizations.list",
                "apps.permissions.info",
                "apps.permissions.request",
                "apps.permissions.resources.list",
                "apps.permissions.scopes.list",
                "apps.permissions.users.list",
                "apps.permissions.users.request",
                "apps.uninstall",
            )
        },
        **{("*", "/" + m): "OAuth: app-level" for m in ("oauth.access", "oauth.token", "oauth.v2.access")},
        ("*", "/auth.revoke"): "revokes the bot's own token",
        ("*", "/rtm.connect"): "the RTM socket: app-level",
        ("*", "/migration.exchange"): "Enterprise Grid migration: app-level",
    },
    prefixes=("/api",),
)
