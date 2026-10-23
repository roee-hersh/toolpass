# microsoft365

toolpass asks Microsoft Graph as an app registration with read-only application permissions. It
resolves the caller's email to an Entra user (by UPN, then `mail`, then proxy address), then reads
the facts the action needs: transitive group membership (`checkMemberGroups`), directory roles,
team and channel membership, and the effective permissions on a drive item. Exchange delegation
(Send As, Send on Behalf, Full Access, calendar sharing) has no Graph API and is always unknown.
toolpass never performs an action and nothing is persisted.

## Credential

An app registration with a client secret or a certificate, and **application** permissions with
admin consent. All of them are read-only:

| Permission | Needed for | Note |
|---|---|---|
| `User.Read.All` | identity lookup, `user.active`, `mail.send_as_self` | |
| `GroupMember.Read.All` | `group.member`, group grants on files, a kubernetes connection's groups (`getMemberGroups`) | |
| `Member.Read.Hidden` (optional) | hidden-membership groups | without it those groups are silently omitted from `checkMemberGroups`: `group.member` answers unknown for a hidden-membership group unless membership is proven, but group grants on files are still omitted, a **possible false deny** there |
| `TeamMember.Read.All` | `team.*`, standard channels | |
| `ChannelMember.Read.All` | private and shared channels | |
| `Files.Read.All` | `file.*` | **broad**: this credential can read every file in every OneDrive and SharePoint site of the tenant. There is no narrower application permission that returns item permissions. Keep the secret tightly held, or omit this permission and accept `file.*` answering `credential_rejected`. |
| `RoleManagement.Read.Directory` (optional) | `role.member` | without it `role.member` is unknown (`credential_rejected`) |

Prefer a certificate: put the public certificate PEM in `certificate_file` and the private key PEM
in `credential`. toolpass then signs a PS256 client assertion with the `x5t#S256` thumbprint. With a
client secret, `credential` is the secret itself.

A certificate credential signs with `cryptography`: install `toolpass[crypto]` (the Docker image has it).

## Connection

```yaml
  - id: m365-contoso
    integration: microsoft365
    tenant_id: contoso.onmicrosoft.com          # or the tenant GUID
    client_id: 22222222-2222-2222-2222-222222222222
    credential: file:/secrets/m365-key.pem      # private key PEM, or the client secret
    certificate_file: /etc/toolpass/m365-cert.pem   # optional; present = certificate auth
    authority_url: https://login.microsoftonline.com  # optional
    url: https://graph.microsoft.com                  # optional
```

| Key | Meaning |
|---|---|
| `tenant_id` | Entra tenant, GUID or verified domain |
| `client_id` | application (client) id |
| `credential` | client secret, or the PEM private key when `certificate_file` is set (`env:`/`file:`) |
| `certificate_file` | path to the public certificate PEM; when set toolpass authenticates with a certificate assertion |
| `authority_url` | token authority, default `https://login.microsoftonline.com` |
| `url` | Graph endpoint, default `https://graph.microsoft.com` |

The token is fetched from `{authority_url}/{tenant_id}/oauth2/v2.0/token` with `scope={url}/.default`
and cached until shortly before expiry. A 401 from Graph invalidates it and the call is retried once.

National clouds are the same two keys (unverified, see below): GCC High / DoD use
`authority_url: https://login.microsoftonline.us` with `url: https://graph.microsoft.us` or
`https://dod-graph.microsoft.us`; China uses `https://login.chinacloudapi.cn` and
`https://microsoftgraph.chinacloudapi.cn`.

## Resources

| Resource | Meaning |
|---|---|
| `user:<email or object id>` | the caller's own account |
| `group:<guid>` | an Entra group (security or Microsoft 365) |
| `role:<guid>` | a directory role **template** id, e.g. `62e90394-69f5-4237-9190-012177145e10` for Global Administrator |
| `team:<guid>` | a team (its group id) |
| `team:<guid>/channel/<channel id>` | a channel; ids look like `19:...@thread.tacv2` |
| `drive:<drive id>/item/<item id>` | a OneDrive or SharePoint item |
| `mailbox:<email>` | an Exchange mailbox |

The user is found by `GET /users/{email}` (guest UPNs with `#EXT#` are path-escaped), then
`$filter=mail eq '...'`, then `$filter=proxyAddresses/any(p:p eq 'smtp:...')`. Several matches are
`user_ambiguous`. A disabled account (`accountEnabled: false`) is a deny for every action; an account
whose `accountEnabled` Graph does not report at all (`account_enabled=unknown` in the identity) is
unknown for every action, never assumed enabled. Guests are allowed but flagged (`guest=true` in the
identity and "(guest account)" in reasons); they are not credited with organization-wide sharing
links, which guest accounts cannot redeem.

## Actions

| Action | Resource | Graph call |
|---|---|---|
| `user.active` | `user:` or `mailbox:` | the resolved user's `accountEnabled`; unknown when Graph does not report it |
| `group.member` | `group:` | `GET /groups/{id}?$select=id,visibility` (404 = not visible, `HiddenMembership` = unknown unless membership is proven), then `POST /users/{id}/checkMemberGroups` (transitive) |
| `role.member` | `role:` | `GET /users/{id}/transitiveMemberOf/microsoft.graph.directoryRole` |
| `team.member` / `team.owner` | `team:` | `GET /teams/{id}/members?$filter=...userId eq '{id}'`, roles; only records whose `userId` is the caller's count, the filter result is never trusted on its own |
| `channel.read` | `team:.../channel/...` | channel `membershipType`; `standard` = team membership, `private` = `/channels/{c}/members`, `shared` = `/channels/{c}/allMembers`; a missing or other value is unknown |
| `channel.owner` | same | as above, roles contain `owner` |
| `channel.message.post` | same | membership; unknown when `moderationSettings.userNewMessageRestriction` is set to anything but `everyone` |
| `file.read` / `file.edit` / `file.delete` / `file.share` | `drive:.../item/...` | `GET /drives/{d}/items/{i}/permissions` plus `GET /drives/{d}?$select=owner` (a 404 there skips only the owner rule); group grants expanded with `checkMemberGroups` in batches of 20 |
| `mail.send_as_self` | `mailbox:` | allow only for the user's own mailbox with `mail` set; an empty `mail` is unknown, not deny |
| `mail.send_as`, `mail.send_on_behalf`, `mailbox.full_access`, `calendar.read`, `calendar.write` | `mailbox:` | **always unknown**: Exchange delegation has no Graph API |

File rules, in order:

1. a permission granted to the user's id with a sufficient role (read: `read`/`write`/`owner`;
   edit and delete: `write`/`owner`; share: `owner`) is an allow;
2. the drive owner (`owner.user.id`) is allowed everything; when the drive lookup answers 404 this
   rule is skipped and the rest still run;
3. permissions granted to Entra groups are expanded with `checkMemberGroups`;
4. a sharing link with `scope: organization` allows with the caveat "via an organization-wide
   sharing link", except for guest accounts, which cannot redeem such links;
5. a grant that reaches the caller (directly, through a group they are in, or through a usable
   organization link) with a role string other than `read`/`write`/`owner` (a custom SharePoint
   permission level such as `sp.full control`) is unknown when it did not already allow: the custom
   level may grant more than toolpass can see;
6. a sharing link whose `scope` is not `organization`, `anonymous` or `users` is unknown;
7. `file.share` with only read/write access is unknown ("sharing rights depend on site settings");
8. permissions granted to SharePoint `siteGroup` principals cannot be expanded through Graph: if
   nothing else allowed, unknown; likewise an anonymous link;
9. otherwise deny.

## Decisions

| Situation | toolpass answers |
|---|---|
| membership / role / permission found | allow |
| user is not a member, has no role, no grant matches and no siteGroup, anonymous link, custom role or unmodelled link scope exists | deny |
| a guest whose only route to a file is an organization-wide sharing link | deny |
| `accountEnabled: false` | deny ("account disabled") |
| `accountEnabled` not reported by Graph | unknown (`unsupported`), for every action |
| no user by UPN, mail or proxy address | deny (`user_not_found`) |
| several users match | unknown (`user_ambiguous`) |
| 404 on a group, team, channel, drive item or its permissions | unknown (`resource_not_visible`) |
| 404 on the drive-owner lookup only | ignored; the group and link rules decide |
| 403 (`Authorization_RequestDenied` or other) | unknown (`credential_rejected`): a permission is missing |
| token endpoint rejects the client | unknown (`credential_rejected`) |
| `group.member` on a `HiddenMembership` group the user is not seen in | unknown (`unsupported`) |
| channel with no `membershipType`, or one other than standard/private/shared | unknown (`unsupported`) |
| `mail.send_as_self` with an empty `mail` attribute | unknown (`unsupported`) |
| custom role strings on a grant reaching the caller, sharing links with an unmodelled scope | unknown (`unsupported`) |
| siteGroup-only grants, anonymous links, moderated channels, Exchange delegation, non-owner sharing | unknown (`unsupported`) |
| 429, 5xx, timeout | unknown (`upstream_rate_limited` / `upstream_error` / `upstream_timeout`) |

Graph error messages are never copied into a decision text; only the error code is used.

## Observed actions

A toolpass tool's Microsoft Graph requests ([observed actions](../guides/intercepted-actions.md))
are mapped by the route table in `toolpass/integrations/microsoft365/routes.py`; `toolpass routes
microsoft365` prints it. The connection's `url` is the host (a national cloud's Graph too) and
`/v1.0` is the API version the table reads.

| Request | Action and resource |
|---|---|
| `GET /drives/{drive-id}/items/{item-id}` (or `/root`), its content, versions, workbook, its list item's fields and versions | `file.read` on `drive:<drive id>/item/<item id>` |
| change, upload, check in or out, restore, label, `POST .../children` | `file.edit` on the item (a new child: on its folder) |
| `PUT .../items/{parent-id}:/{name}:/content` with `@microsoft.graph.conflictBehavior=fail` or `rename` | `file.edit` on the folder |
| `createLink`, `invite`, adding or changing the item's permissions | `file.share` on the item, sends out |
| removing a permission, or people from a sharing link (`revokeGrants`) | `file.share` on the item |
| reading the item's permissions (they carry its sharing links) | `file.share` on the item |
| `DELETE` the item, `permanentDelete` | `file.delete` on the item |
| `GET /teams/{guid}`, its members, channel list, apps, tags; the General channel | `team.member` on `team:<guid>` |
| change, archive or delete a team; its members, channels, apps, tags | `team.owner` on `team:<guid>` |
| `GET /teams/{guid}/channels/{channel-id}` and its messages, members, tabs | `channel.read` on `team:<guid>/channel/<channel id>` |
| post a message or a reply, react | `channel.message.post`, sends out |
| change or delete a channel or someone's message; the channel's members, tabs, sharing | `channel.owner` |
| `GET /groups/{guid}` and its members, owners, conversations, calendar, planner | `group.member` on `group:<guid>` (`/team`: `team.member`) |
| `POST /users/{email or id}/sendMail`, a draft's `send`, `reply`, `replyAll`, `forward` | `mail.send_as_self` on `mailbox:<email or id>`, sends out |
| anything else in that user's messages, folders, mailbox settings, calendar, events, contacts; `GET /users/{email or id}?$select=mailboxSettings` (or `$select=*`) | `user.active` on `mailbox:<email or id>` (allowed for the user's own mailbox only); calendar changes, inbox rules and automatic replies send out |
| `GET /users/{id}/drive`, `/drives`, `/joinedTeams` | `user.active` on `user:<id>` |
| `GET /me`, `/users`, `/users/{id}` (without `mailboxSettings` in `$select`), `/groups`, `/teams`, `/drives`, a drive's metadata | no check: the agent's own account, or directory data every member may read |
| reads outside the claimed areas listed below (`/organization`, `/reports`, ...) | no check |

A message whose `from` or `sender` names another mailbox is checked as `mail.send_as` on that
mailbox, which is always unknown, so it is refused. The table claims `/drives`, `/teams`,
`/groups`, `/users`, `/me`, `/sites`, `/shares` and `/chats`, and refuses there what it cannot tie
to one checked resource:

- `/me/...`: the token's own account, which need not be the session's user; name the user in
  `/users/{email}/...` instead.
- `$batch`: each request is sent on its own so each is checked.
- An item addressed by path (`/root:/a/b.txt`), through a site, group, user or sharing link, or by
  navigating to another item; address it as `/drives/{drive-id}/items/{item-id}`.
- An item's or list item's `createdByUser` and `lastModifiedByUser` (another user's profile and
  mailbox settings), and anything below the list item but its `fields` and `versions`.
- `$select=mailboxSettings` (or `*`) on the user list, a manager or direct reports, which would read
  other mailboxes' settings.
- Deleted teams (`/teamwork/deletedTeams` and below, in any spelling): their channels, messages
  and files belong to no team whose membership could be checked.
- Folder listings (`children`, `delta`, `search`): a listing carries every child's metadata and
  download link, and a child may be narrower than its folder. A child is read by id
  (`/items/{folder}/children/{child-id}`).
- Moves and copies (a destination folder is a second item), a new item that would replace an
  existing one (`@microsoft.graph.conflictBehavior=replace`, or a path upload without `fail` or
  `rename`).
- MIME message content, which can name any sender; a body that is not JSON, names a member twice,
  or that the hook could not read (a stream).
- `$expand` (except `replies` on channel messages), method override headers, encoded slashes, dot
  segments, and other spellings of the claimed paths: another case (`/Drives`), OData key syntax
  (`/users('x')`), `/beta`.
- Writes to groups (there is no owner action, and membership does not show the right to post),
  SharePoint sites, chats, Shifts, calendars by id (they may be another mailbox's, shared), and
  directory administration: listed as left out on purpose.

`GET /teams/{guid}/channels` lists every channel to a team member, private ones included.

## Probe

`toolpass probe` fetches a token, reads `GET /organization` and reports the tenant, then reads the
app's own service principal and `appRoleAssignments` to list granted application permissions. It
warns for any permission that allows writes (`.ReadWrite.`, `Mail.Send`, ...), for required
permissions that are missing, for the breadth of `Files.Read.All`, and when `Member.Read.Hidden` is
absent. If it cannot read its own assignments it warns "could not verify permissions".

## Kubernetes groups

As a [kubernetes](kubernetes.md#groups-from-the-email) connection's `groups_connection`, the
connection resolves the user as for a check (a disabled account is `user_not_found`), then calls
`POST /v1.0/users/{id}/getMemberGroups` with `{"securityEnabledOnly": false}` and returns the object
ids of every group the user belongs to, directly or transitively, lower-cased: the group names AKS's
Entra ID integration binds to. `GroupMember.Read.All` covers it. The Kubernetes username stays the
kubernetes connection's `username_template` (the UPN on AKS).

## What it cannot see

- **Site collection administrators** and SharePoint site-level roles are not in item permissions.
- **Custom SharePoint permission levels** collapse to read/write/owner; an unrecognised role string
  on a grant that reaches the caller makes the answer unknown rather than deny, because the level
  may include the right in question.
- **Sensitivity labels**, **Conditional Access**, **information barriers** and **Data Loss
  Prevention** can block an action toolpass allowed.
- **Team and channel moderation settings** are only detected, not evaluated; a member of a
  moderated channel is unknown for `channel.message.post`.
- **Exchange delegation** (Send As, Send on Behalf, Full Access, calendar folder permissions) has
  no application-permission Graph API: those actions are always unknown.
- **Hidden-membership groups** are omitted from `checkMemberGroups` without `Member.Read.Hidden`.
  `group.member` reads the group's `visibility` first and answers unknown for such a group unless
  membership is proven; group grants on files do not get that lookup (possible false deny there).
- **Organization-wide sharing links** are not credited to guest accounts; a guest with no other
  grant is denied.
- **PIM eligible** role assignments that are not activated do not appear in `transitiveMemberOf`.

## Unverified

Each item is marked `# UNVERIFIED:` in the code.

- Whether Entra still accepts RS256 with the legacy `x5t` header for certificate assertions; only
  PS256 with `x5t#S256` is implemented.
- The `proxyAddresses/any(p:p eq 'smtp:...')` advanced query with `ConsistencyLevel: eventual` and
  `$count=true`, and whether the `smtp:` prefix match is case-sensitive.
- The OData cast path `transitiveMemberOf/microsoft.graph.directoryRole?$select=roleTemplateId`.
- `/teams/{t}/channels/{c}/allMembers` for shared channels, and whether the `userId` filter applies
  there.
- `GET /drives/{id}?$select=owner` returning `owner.user.id`; SharePoint document libraries are
  expected to report the site's group instead, so the drive-owner rule mostly helps OneDrive.
- `file.delete` requiring only the `write` role; SharePoint "contribute without delete" levels are
  not distinguishable.
- `GET /groups/{id}?$select=id,visibility` being readable with `GroupMember.Read.All` alone, and
  `visibility` being null for security groups and `HiddenMembership` only for Microsoft 365 groups
  created that way.
- That `checkMemberGroups` omits hidden-membership groups (rather than failing) when
  `Member.Read.Hidden` is not granted; `group.member` treats a miss on such a group as unknown.
- That "people in your organization" sharing links cannot be redeemed by guest (B2B) accounts, as
  Microsoft's sharing documentation states; such links are not credited to guests.
- Whether an app can read its own service principal and `appRoleAssignments` with the permissions
  above; the probe warns instead of failing when it cannot.
- That AKS names Entra groups by the same object ids `getMemberGroups` returns, overage claims
  included (a user in more than 200 groups gets a token without them, which AKS resolves itself).
- National cloud endpoints (`login.microsoftonline.us`, `graph.microsoft.us`,
  `dod-graph.microsoft.us`, `login.chinacloudapi.cn`, `microsoftgraph.chinacloudapi.cn`) are
  configured through `authority_url` and `url` but were not exercised.

## Test

Unit tests run against a fake token endpoint and a fake Graph (`tests/integrations/microsoft365/test_microsoft365.py`). They cover
both credential types (the certificate path decodes the assertion, checks PS256 and the `x5t#S256`
thumbprint and verifies the signature), token caching and the 401 retry, every identity fallback
(including `#`, `%` and `/` in the address being path-escaped), an unreported `accountEnabled`,
every action allow and deny, `checkMemberGroups` batching, missing and hidden-membership groups, a
members collection that ignores the `userId` filter, a channel without a `membershipType`, the
file permission rules (guest and organization links, custom role strings, unmodelled link scopes,
a 404 on the drive-owner lookup), private and shared channels, the probe warnings and the injected
failure modes. `tests/integrations/microsoft365/test_routes.py` holds the route table to the Graph
description (`TOOLPASS_SPECS_DIR`), routes the main reads and writes, checks every refusal above,
parses each resource with the integration's own parser and runs routed requests through the check
against the fake Graph.
