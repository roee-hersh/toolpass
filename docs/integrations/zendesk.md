# zendesk

One connection is one Zendesk Support account. toolpass authenticates as an administrator, finds
the team member or end user by email and reads what governs their access: the role (`admin`,
`agent`, `end-user`), the custom role's configuration on Enterprise plans, or the profile's ticket
restriction on other plans, plus the groups the agent belongs to. Ticket questions read the ticket
(group, assignee, requester, organization, status) and apply the agent's ticket access before the
action's own setting. Nothing is written.

## Credential

Either of:

1. An **API token** (`auth_mode: token`, the default) with `username` set to the email of the
   team member the token acts as. Zendesk sends it as HTTP Basic `email/token:<token>`. An API token
   acts with the full permissions of that user, so create a dedicated administrator for toolpass and
   keep the token tightly held.
2. An **OAuth access token** (`auth_mode: oauth`), sent as `Bearer`. The `read` scope covers every
   endpoint toolpass calls.

Use an administrator: an agent credential cannot read tickets outside its own ticket access
(Zendesk answers 403), and those tickets then answer `resource_not_visible`.

`toolpass probe` reads `GET /api/v2/users/me` and reports the credential's user and role.

## Connection

```yaml
  - id: zendesk-acme
    integration: zendesk
    url: https://acme.zendesk.com
    username: toolpass-bot@acme.com
    credential: env:ZENDESK_API_TOKEN
    # auth_mode: oauth                 # credential is then an OAuth access token; username unused
```

| Key | Meaning |
|---|---|
| `url` | the account URL |
| `auth_mode` | `token` (default) or `oauth` |
| `username` | `token` mode: the email of the user the API token acts as |
| `credential` | the API token or OAuth access token, `env:` or `file:` |

### Identity

`GET /api/v2/users/search?query=email:<email>`. The search matches loosely, so only a record whose
`email` equals the address (ignoring case) is the user; when no primary email matches, each result's
`GET /api/v2/users/{id}/identities` is read and a secondary `email` identity equal to the address
counts. None is `user_not_found`, two are `user_ambiguous`. A deleted user (`active: false`) or a suspended one is denied every action; a record that does not say whether the user is active and not suspended is unknown (`unsupported`). The
identity carries the role, `role_type`, `custom_role_id`, `ticket_restriction`,
`only_private_comments` and the default `organization_id`; for agents and administrators the
identity's groups are the ids from `GET /api/v2/users/{id}/group_memberships` (agents only:
administrators see every ticket). Groups sent by the caller are ignored.

### Grants

| User | Source of the grants |
|---|---|
| `role: admin` | may do everything; closed tickets still take no comment, merge or property change |
| `role: agent` with `custom_role_id` | the role's `configuration` from `GET /api/v2/custom_roles` (Enterprise; cached five minutes, refetched once when the id is unknown) |
| `role: agent`, `role_type: 1` (light agent), no custom role | sees tickets per `ticket_restriction`, comments privately, edits only tickets they requested |
| `role: agent`, no custom role | `ticket_restriction` on the profile (`null` all, `groups`, `organization`, `assigned`); `only_private_comments`; settings the plan does not expose answer `unsupported` |
| `role: agent`, `role_type` 2 or 3 (chat agent, contributor), no custom role | `unsupported` |
| `role: end-user` | sees and comments on tickets they requested or are CC'd on |

## Resources

| Resource | Meaning |
|---|---|
| `ticket:<id>` | a ticket, by numeric id (plain decimal, no leading zero) |
| `organization:<id>` | an organization |
| `user:<id>` | a user profile |
| `account` | the account, for settings that are not about one object |

## Actions

| Action | Resource | Decided by |
|---|---|---|
| `ticket.view` | `ticket:` | ticket access covers the ticket (below) |
| `ticket.edit` | `ticket:` | ticket access, `ticket_editing`; light agents only as requester; end users never |
| `ticket.comment_public` | `ticket:` | ticket access, `ticket_comment_access: public` / `only_private_comments: false`; light agents never; end users on their own tickets |
| `ticket.merge` | `ticket:` | ticket access and `ticket_merge` |
| `ticket.delete` | `ticket:` | ticket access and `ticket_deletion`; closed tickets included |

A **closed** ticket takes no comment, merge or property change from anyone, administrators
included; Zendesk creates a follow-up instead. The one exception is `ticket.edit` for a custom role
with `modify_closed_tickets`.
| `organization.edit` | `organization:` | the organization exists and `organization_editing` |
| `user.edit` | `user:` | administrators edit anyone; anyone edits their own profile; end-user profiles per `end_user_profile_access` (`full`/`edit`, `edit-within-org` compares default organizations, `readonly`); other team members only by administrators |
| `macro.manage` | `account` | `macro_access: full` (shared macros; `manage-group` and `manage-personal` are denied) |
| `view.manage` | `account` | `view_access: full` |
| `business_rules.manage` | `account` | `manage_business_rules` |
| `account.admin` | `account` | `role: admin` |

### Ticket access

| `ticket_access` / `ticket_restriction` | The ticket is visible when |
|---|---|
| `all` / `null` | always |
| `within-groups` / `groups` | its group is one of the agent's groups, or the agent is its assignee or requester |
| `within-groups-and-public-groups` | as above, or its group's `is_public` is true (`GET /api/v2/groups/{id}`) |
| `within-organization` / `organization` | its `organization_id` is the agent's default organization, or the agent is its assignee or requester |
| `assigned-only` / `assigned` | the agent is its assignee |
| `requested` (end users) | the user is its requester or in `collaborator_ids` |

A ticket in no group, asked about by an agent restricted to groups, answers `unsupported`: whether
unassigned tickets appear depends on views toolpass does not read.

## Decisions

| Code | When |
|---|---|
| `allowed` / `denied` | the grants above decide |
| `unsupported` | a setting the plan does not expose (non-Enterprise agent asked about deletion, macros, views, business rules, organizations); an agent role type or ticket access value toolpass does not know; a ticket in no group for a group-restricted agent |
| `resource_not_visible` | the ticket, organization, user or the ticket's group answers 404, or 403 because toolpass's own user cannot see it; the user's `custom_role_id` is not among the account's custom roles |
| `user_not_found` / `user_ambiguous` | the email search |
| `credential_rejected` | 401, or 403 on a listing toolpass's user may not read |
| `invalid_request` | a non-numeric id, a query on the resource, a resource type the action does not take |
| `upstream_*` | 5xx, 429, timeouts, a `next_page` link pointing off the API |

## Route table

What a secured tool's request to the account means (`toolpass routes zendesk` prints the table).
Ids are plain decimal; a `.json` suffix on the path is read as Zendesk reads it.

| Request | Action, resource |
|---|---|
| `GET /api/v2/tickets/{id}` and below, `GET /api/v2/requests/{id}` | `ticket.view` on `ticket:{id}` |
| `PUT /api/v2/tickets/{id}` (or `/requests/{id}`) with only a public comment | `ticket.comment_public` on `ticket:{id}` |
| the same with property changes, or an internal note (`"public": false`) | `ticket.edit` on `ticket:{id}` |
| `DELETE /api/v2/tickets/{id}`, comment and chat redaction | `ticket.delete` on `ticket:{id}` |
| ticket tags, making a comment private | `ticket.edit` on `ticket:{id}` |
| `PUT`, `DELETE /api/v2/organizations/{id}`, its tags | `organization.edit` on `organization:{id}` |
| `PUT /api/v2/users/{id}` changing profile fields only (name, email, phone, notes, tags, user fields, ...), user tags and identities | `user.edit` on `user:{id}`; the user update, adding or changing an identity and requesting verification send out |
| a user update touching anything else (role, organization, suspension, ...); deleting, merging, suspending users, their group and organization memberships; creating users and organizations | `account.admin` on `account`; creating users and `update_many` send out |
| macros, views, triggers and automations | `macro.manage`, `view.manage`, `business_rules.manage` on `account` |
| account configuration (fields, forms, groups, brands, roles, SLAs, targets, ...) | `account.admin` on `account` |
| search and lists, `GET /api/v2/users/me`, `GET /api/v2/tickets/messaging/conversations/{id}/ticket` | no check (the agent's own access) |

A request that sends out may make Zendesk email someone, so it counts as sending data out of the
session: a ticket update notifies its requester and CCs, a new or changed email address (a user's
`email`, an email identity) is sent a verification unless the body says `"skip_verify_email": true`,
and a new user may be sent a welcome email. The route cannot read `skip_verify_email`, so these
writes send out either way.

An internal note is checked as `ticket.edit`: toolpass has no action for internal notes, so a light
agent is refused one. Refused, so the tool stops there:

- a public comment beside property changes: it needs two checks; send them as two requests;
- a comment that names its author (`author_id`) or says `is_public`, and a `voice_comment` or
  `tpe_voice_comment` in the ticket or request: the comment rules cannot read them;
- `PUT /api/v2/requests/{id}` with `solved`: a requester marking their request solved is not
  supported. Zendesk accepts that endpoint from end users only, `ticket.edit` is never allowed to
  end users, and no action covers solving as the requester; an agent solves the ticket with
  `PUT /api/v2/tickets/{id}` instead;
- a body that is not one JSON object under its record's key (`{"ticket": {...}}`) sent as
  `application/json`, that names a member twice, whose `id` is another record's, or that comes
  with a query string (Zendesk would merge a query's `ticket[...]` over the body);
- creating tickets, bulk ticket changes, merges and spam marking, satisfaction ratings, and the
  rest the table lists as left out: the integration has no action that covers them, or they change
  more than the one object checked.

## What it cannot see

- **Views and brands.** Ticket visibility through views, `groups_ticket_access: selected-groups`
  and `brands_ticket_access` are not read; only the role's `ticket_access` is applied.
- **Several organizations.** Only the user's default `organization_id` is compared for
  `within-organization` access and `edit-within-org` profile access.
- **Ticket sharing agreements, side conversations, apps** and the account owner's special standing
  are not modelled. Suspended tickets and deleted tickets are read like any other.
- **Custom role types.** A custom role's `role_type` (custom admin, billing admin) is not
  interpreted beyond its configuration.

## Unverified

Written from the OpenAPI description and Zendesk's help-center articles; not run against a live
account. Marked `UNVERIFIED` in the code:

- Whether `role_type` is `null` or `0` for a plain agent without a custom role.
- Whether an agent with several organization memberships sees tickets of all of them (toolpass
  compares the default organization only, so such an agent may be denied wrongly).
- Whether a ticket outside toolpass's own user's ticket access answers 403 or 404 (both answer
  `resource_not_visible`).
- Whether a light agent's `ticket_restriction` is `null` when the profile shows all tickets.
- Whether administrators may change a closed ticket's properties without `modify_closed_tickets`
  (denied here).

## Test

`python -m pytest tests/integrations/zendesk` (in `toolpass-py`) runs a fake Zendesk validated against the Support API
OpenAPI description when `TOOLPASS_SPECS_DIR` holds `zendesk.spec` (`test/specs/fetch.sh`). The
fake has an administrator, three custom-role agents (within-groups, assigned-only, within-organization),
a light agent, a plain agent restricted to groups and two end users. A property test
(`test_fuzz_parse_target`) checks that
only plain decimal ids reach the API.
