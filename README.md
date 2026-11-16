# agent-authorizer

[![ci](https://github.com/roee-hersh/toolpass/actions/workflows/ci.yaml/badge.svg)](https://github.com/roee-hersh/toolpass/actions/workflows/ci.yaml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**Authorizes what an AI agent does against the permissions of the user it is acting for. The
agent keeps its own credential; each action it takes for a user runs only if that user may do it.**

## The problem

An agent's tools need a credential to work: an Okta service user, an OAuth app with client
credentials, a GitHub App, a Jira API token. Whatever that credential can do, the agent can do, for
everyone who talks to it.

Dana cannot merge to `acme/gitops-prod`. The agent's GitHub App can. When Dana asks the agent to
merge, GitHub sees the App and allows it. Dana now has the agent's permissions, and so does anyone
whose email or web page the agent reads. This is the
[confused deputy](https://en.wikipedia.org/wiki/Confused_deputy_problem) problem, and OWASP lists it
for agents as Identity and Privilege Abuse (ASI03).

The usual answers each give something up:

| Approach | What goes wrong |
|---|---|
| Give the agent one broad credential | Every user gets the agent's permissions |
| Narrow the agent's credential | One scope for all users: too much for some, too little for others |
| A token per user (OAuth on behalf of the user) | A consent flow and token storage for every user and system, and many systems have no such flow (Kubernetes, AWS, API tokens, Terraform) |
| Copy permissions into a policy engine | A second copy of who may do what, which drifts from the system that owns it |

## What agent-authorizer does

It leaves the agent's credential where it is and adds a check in front of it. Before a tool sends a
request for a user, agent-authorizer asks the system that owns the resource (GitHub, Jira,
Kubernetes, AWS and 18 more) whether *that user* may do that action, now. If not, the request is
refused before it leaves the process. The agent can do only what both it and the user may do.

| The user asks the agent to | What is checked against the user | Answered by |
|---|---|---|
| Open a pull request | `pr.create` on `repo:acme/gitops` | GitHub: the user's permission on the repository |
| Look at a Jira board | `BROWSE_PROJECTS` on `project:PAY` | Jira: the project's permission scheme |
| Create a Kubernetes deployment | `raw:create:deployments.apps` in the namespace | The cluster: a SubjectAccessReview for the user |
| Create an ECR repository | `raw:ecr:CreateRepository` | AWS: the IAM policy simulator, for the user's role |

It issues no tokens and holds no policies. It is not an OAuth authorization server and not a
policy engine: the systems you already use stay the only place where permissions are defined.

### Choose which actions run as the user

A tool is a plain Python function with one decorator. It declares nothing: agent-authorizer watches
the HTTP requests it makes, with any SDK, and maps each to the action it is
(`POST /repos/acme/x/pulls` is `pr.create` on `repo:acme/x`).

```python
import agent_authorizer
from agent_authorizer import AgentAuthorizer, Session, as_user
from github import Github                     # any SDK: PyGithub, slack_sdk, boto3, raw requests

agent_authorizer.configure(permissions=AgentAuthorizer.from_config("agent-authorizer.yaml"))   # your GitHub, Jira, ... connections


@agent_authorizer.tool()
def propose_fix(repo: str, path: str, fix: str) -> str:
    repo_ = Github(auth=token).get_repo(repo)
    current = repo_.get_contents(path)        # the agent reads on its own authority
    with as_user():                           # pr.create: checked against the user, before it is sent
        pr = repo_.create_pull(title="fix", body=fix, head="fix", base="main")
    return pr.html_url


with Session("dana@example.com").active():   # the user your app authenticated, never the model
    agent.run(prompt)
```

- **Some actions:** requests inside `as_user()` are checked against the session's user. This is
  usually the writes: open a pull request, delete an issue, create a deployment.
- **All actions:** `configure(act_as="user")` checks every request against the user, except inside
  `as_agent()`.
- **The rest:** requests outside `as_user()` run on the agent's own authority, still scoped, limited
  and audited.

### Actions hidden inside a tool

The user asks for an ECR repository; the tool opens a pull request against the Terraform
repository. The model sees one tool. agent-authorizer sees the three writes inside it and checks
each one against the user:

```python
@agent_authorizer.tool()
def request_ecr_repository(name: str) -> str:
    infra = Github(auth=token).get_repo("acme/infra")
    main = infra.get_branch("main")                                   # read: the agent's own authority
    with as_user():
        infra.create_git_ref(f"refs/heads/ecr-{name}", main.commit.sha)                                  # repo.push
        infra.create_file(f"ecr/{name}.tf", f"add ecr {name}", ecr_module(name), branch=f"ecr-{name}")   # repo.push
        pr = infra.create_pull(title=f"ECR repository {name}", body="", head=f"ecr-{name}", base="main") # pr.create
    return pr.html_url
```

A user who may not push to `acme/infra` is refused at the first write, and nothing is created.

Run a tool once with `configure(record="agent-authorizer-tools.yaml")` and agent-authorizer writes
down exactly which permissions it needs, from whom; hand that file back with
`configure(entitlements=...)` and the tool is held to it. A tool can also declare what it does
(`effect`, `scope`, `credential`, or one explicit check with `authorize=permission_check(...)`) and
the two compose; `as_tool=` hands any framework a real tool with the checks inside.
[The guide](docs/guides/intercepted-actions.md) has the details. An MCP server gets the user the
same way: the agent sends it in the `X-Agent-Authorizer-User` header, and `session_from_header` on
the server's HTTP app opens the session for each request
([the agent guide](docs/guides/agent-tools.md)).

## See what your agent's tools do

When an MCP server or an agent is built with [agent-authorizer tools](#choose-which-actions-run-as-the-user),
it reports each run to your agent-authorizer server: who it ran for, and every request the tool made to
GitHub, Jira, Slack, Linear or Kubernetes, allowed or refused by the system itself, with the reason.
The server's dashboard shows them, from every agent, live:

```sh
pip install agent-authorizer
agent-authorizer serve -config agent-authorizer.yaml        # your connections and an API key; agents ask it and report to it
open https://agent-authorizer.internal/dashboard/   # every run, from every agent
```

![The dashboard: 9 tool calls by Dana and Sam, a map from the MCP server shop to its tools open_pr, ship, report_bug and status to the systems they reached (Kubernetes, GitHub, Jira, Slack, Linear), and the calls, Sam's refused at Linear, Kubernetes and GitHub](docs/assets/dashboard.png)

See it on your machine, with no accounts: two people use an MCP server whose tools each use
several of five systems, and the real integrations decide every request.

```sh
git clone https://github.com/roee-hersh/toolpass && cd toolpass/agent-authorizer-py
pip install -e . && python examples/demo/run.py -open
```

Nothing to register and nothing written to disk: an agent whose permissions come from the server
reports there with no further setup, the server keeps the latest runs in memory for the page, and
`agent_authorizer.otel` exports each run as a span into the tracing you already have. The
[dashboard guide](docs/guides/dashboard.md) has the details.

## What it stops

From [`agent-authorizer-py/examples/ops_agent.py`](agent-authorizer-py/examples/ops_agent.py), a scripted ops-agent session
you can run with no network and no model:

```text
[ran]     read_email(4711)  (it carries an injection)
          -> The block below is untrusted data from read_email. Everything between the two tags ...
[refused] delete_branch(main) as the email asked
          -> out_of_scope: branch='main' is outside this tool's scope
[refused] delete_branch(feature/x) with the email's words
          -> untrusted_input: an argument repeats content from an untrusted source seen in this session
[ran]     customer_tickets(checkout)  (private data)
[refused] post_slack(#ops), reworded
          -> exfiltration_risk: this session has read private data and seen untrusted content,
             and this tool sends data out
[refused] open_gitops_pr(acme/gitops-prod) as dana
          -> not_authorized: agent-authorizer deny ... dana@example.com is not an admin
[ran]     open_gitops_pr(acme/gitops-prod) as admin
```

## What a tool can declare

| Protection | What it guarantees |
|---|---|
| **User from the session** | Who is asking comes from your app, never from the model's arguments |
| **Argument validation** | Arguments match their type hints and your validators before anything else runs |
| **Scope limits** | The tool can only touch what it declares (`repo: acme/gitops-*`), whoever asks |
| **Action limits** | A loop or an injection cannot run 200 deletes in one session |
| **Permission checks** | The call runs only if the person asking may do it, answered live by Jira, GitHub, Kubernetes, AWS and 18 more |
| **Untrusted-input check** | An action whose arguments repeat injected text from an email or web page is refused |
| **Fencing** | Untrusted text reaches the model in a block whose id its author could not know; forged tags are replaced, borrowed frames (`Human:`, `<system-reminder>`) counted; `agent_authorizer.fence()` fences any text |
| **Exfiltration guard** | Once a session has read private data and seen untrusted content, nothing leaves |
| **Credential injection** | Secrets are fetched after every check and redacted from anything the tool echoes |
| **Audit** | One event per call, refused or run, with who asked and why it was decided |
| **Observed actions** | Leave `effect` out and every request the body makes (PyGithub, slack_sdk, boto3, ...) is mapped to an action; inside `as_user()` it is checked against the user and refused before it is sent; a run can record them as the tool's entitlements |

Every call goes through these in a fixed order, and anything that cannot be evaluated (an
authorizer that is down, a validator that crashes) refuses the call. The
[package README](agent-authorizer-py/README.md) has the order, the API and the limits.

## The exfiltration guard

An agent can be made to leak data only when one session has all three of what Simon Willison calls
the lethal trifecta: access to private data, exposure to untrusted content, and a way to send data
out. Your tools already say which they are:

```python
@agent_authorizer.tool(effect="read", reads_private=True)       # read customer records
@agent_authorizer.tool(effect="read", untrusted_output=True)    # read an email
@agent_authorizer.tool(effect="write", sends_out=True)          # post to Slack
```

After the first two have run in a session, the third is refused. It works from which tools
ran, not from spotting the injected words, so a model that paraphrases the injection does not get
past it.

## Permission checks

Your agent acts with its own credential, which can usually do more than the person asking. When
Dana asks it to delete an issue she couldn't delete herself, the agent can. agent-authorizer asks the
system that owns the resource, live, whether *Dana* may do it:

```python
from agent_authorizer import AgentAuthorizer, permission_check

tp = AgentAuthorizer.from_config("agent-authorizer.yaml")   # or AgentAuthorizer.remote(url, key) to keep lookup credentials out of the agent

@agent_authorizer.tool(effect="destructive", authorize=permission_check(tp, "jira-main", "DELETE_ISSUES", "issue:{key}"))
def delete_issue(key: str) -> str: ...
```

The [permission engine](docs/permission-checks.md) answers for
Kubernetes, Argo CD, GitHub, GitLab, Bitbucket, Jira, Confluence, Slack, Datadog, PagerDuty, AWS,
Google Workspace, Google Cloud, Microsoft 365, Databricks, Salesforce, Snowflake, Vault, Azure,
Linear, Zendesk and Okta, with no policy language and no synced copy of anyone's permissions. It also
runs on its own, in-process or as a server, with adapters for nine agent frameworks.

The server answers people too. Connect Claude Code or any MCP client to its `/mcp`
endpoint and ask "can Ilya create deployments in payments?": three read-only tools find the
connection and the action and ask the system, now. It is for looking things up, and it stops
nothing; the checks inside your tools do that.
[Ask who may do what, from your assistant](docs/guides/ask.md) has the details.

```sh
claude mcp add --transport http agent-authorizer https://agent-authorizer.internal/mcp --header "Authorization: Bearer $AGENT_AUTHORIZER_API_KEY"
```

## Install

```sh
pip install agent-authorizer
```

Python 3.10 or later; the core depends only on PyYAML. If your shell cannot find `agent-authorizer` after
installing (pip warns that its scripts folder is not on PATH, common on Windows), run it as
`python -m agent_authorizer` (`py -m agent_authorizer` on Windows): `py -m agent_authorizer serve -config agent-authorizer.yaml`.

### Renamed from toolpass

agent-authorizer was called toolpass. Nothing about how it works changed, and the old names are
not accepted:

| Was | Is |
|---|---|
| `pip install toolpass`, `npm install toolpass-client` | `pip install agent-authorizer`, `npm install agent-authorizer-client` |
| `import toolpass`, `Toolpass`, `ToolpassError` | `import agent_authorizer`, `AgentAuthorizer`, `AgentAuthorizerError` |
| The `toolpass` command, `toolpass.yaml` | The `agent-authorizer` command, `agent-authorizer.yaml` |
| `X-Toolpass-User` and the other `X-Toolpass-*` headers | `X-Agent-Authorizer-User`, `X-Agent-Authorizer-*` |
| `TOOLPASS_API_KEY`, `TOOLPASS_URL` | `AGENT_AUTHORIZER_API_KEY`, `AGENT_AUTHORIZER_URL` |
| The image `ghcr.io/roee-hersh/toolpass`, the chart `charts/toolpass` | `ghcr.io/roee-hersh/agent-authorizer`, `charts/agent-authorizer` |

## Honest limits

- The untrusted-input check matches words, not meaning: a paraphrase gets past it. The
  exfiltration guard catches a paraphrase only when the session also read private data and the
  tool sends data out; otherwise a paraphrased instruction runs, within the tool's scope and limits.
- Fencing helps the model tell data from instructions; it does not guarantee it.
- Sessions and limits live in memory, in one process.
- A permission check and the action after it are not atomic.
- Observed actions cover HTTP from urllib3, the standard library, httpx and aiohttp, with route
  tables for all 22 integrations (`agent-authorizer routes`); gRPC and threads an SDK starts are not seen. A
  write a table cannot map exactly is refused; such a read goes through unchecked, marked untrusted.

## Repository

| Path | What |
|---|---|
| [`agent-authorizer-py/`](agent-authorizer-py/README.md) | The Python package: the secure-tools toolkit and the permission engine, in-process or as a server |
| [`agent-authorizer-ts/`](agent-authorizer-ts/README.md) | The Node client for an agent-authorizer server |
| [`docs/`](docs/README.md) | Permission checks: quickstart, architecture, deploy, integrations |
| [`deploy/`](deploy/helm/agent-authorizer/README.md) | The Helm chart for an agent-authorizer server |

## License

Apache-2.0.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Report security
issues privately as described in [SECURITY.md](SECURITY.md).

Much of this code was written with Claude Code, directed and reviewed by the maintainer. That is
why the tests are the bar rather than the author: the checks above are enforced by tests that try
to get past each one, not by trust.
