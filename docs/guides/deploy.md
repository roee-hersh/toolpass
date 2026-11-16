# Deploy agent-authorizer

agent-authorizer is one Python package. The engine runs inside your agent's process, or as one stateless
server process (a container or a `pip install`) with one config file and the credentials it
references. This page covers where to run it, how to reach it safely, and each way to install it.
The [quickstart](../quickstart.md) runs it locally in five minutes. The
[configuration reference](../reference/configuration.md) documents the file.

## Pick a topology

| | In-process | Sidecar | Shared service |
|---|---|---|---|
| **Shape** | `pip install agent-authorizer` in the agent; `AgentAuthorizer.from_config(...)` | One agent-authorizer container next to each agent, in the same pod or on the same host | One agent-authorizer deployment that every agent calls |
| **Agent uses** | the engine directly | `AgentAuthorizer.remote("http://localhost:8080", key)` | `AgentAuthorizer.remote("https://agent-authorizer.example.internal", key)` |
| **TLS** | Not needed: no network hop | Not needed: traffic never leaves the pod | Needed in front of agent-authorizer (see below) |
| **Upstream credentials** | In the agent's process | In every agent's pod, mounted only into the agent-authorizer container | In one place |
| **Cache** | Per agent process | Per agent | Shared by every agent that hits the same replica |
| **Languages** | Python | any (Python, the Node client, HTTP) | any |
| **Choose it when** | Development, or an agent that may hold the lookup credentials | One or two agents, or you want the API key never to cross the network | Several agents or teams, or you want the upstream credentials held in one place |

In-process is the simplest: nothing to deploy and no API key. It also puts agent-authorizer's lookup
credentials in the agent's process, where agent code (or a model with a shell) could read them.
Those credentials answer for every user, and in Jira they need Administer Jira
([trust boundaries](../concepts/architecture.md#trust-boundaries)). When that matters, run a server
and change one line in the agent: `AgentAuthorizer.from_config(...)` becomes `AgentAuthorizer.remote(...)`.

## In-process

```sh
pip install "agent-authorizer[crypto]"   # crypto only for the integrations that sign with a private key
```

```python
from agent_authorizer import AgentAuthorizer

tp = AgentAuthorizer.from_config("/etc/agent-authorizer/agent-authorizer.yaml")
```

The file's caches and `decision_log` apply; `listen` and `api_key` are for the server and are not
needed. `AgentAuthorizer(connections=[...])` takes the same connections in code (see the
[configuration reference](../reference/configuration.md#connections-in-code)); there the decision log
is off unless you pass `decision_log=`. Build one `AgentAuthorizer` per process and share it: each one
keeps its own caches.

## TLS and reaching agent-authorizer

The agent-authorizer server serves plain HTTP. `AgentAuthorizer.remote` and the Node client accept `http://` only
for `localhost` or a loopback address, because the API key travels in a header. So:

- **Sidecar**: point the agent at `http://localhost:8080`. Nothing else to do.
- **Shared service**: put TLS in front and give agents an `https://` URL. Use an Ingress or Gateway
  with a certificate (cert-manager, your cloud load balancer), or a TLS-terminating proxy on the
  host. A private CA works: point Python at it with `SSL_CERT_FILE` and Node with
  `NODE_EXTRA_CA_CERTS`.

Keep agent-authorizer off the public internet either way. Only your agents' tool layers need to reach it.

## Docker

```sh
docker run -d --name agent-authorizer -p 127.0.0.1:8080:8080 \
  -e AGENT_AUTHORIZER_API_KEY="$(openssl rand -hex 32)" \
  -e JIRA_TOKEN \
  -v "$PWD/agent-authorizer.yaml:/etc/agent-authorizer/agent-authorizer.yaml:ro" \
  ghcr.io/roee-hersh/agent-authorizer:0.6.0
```

The image is `python:3.13-slim` with `agent-authorizer[crypto]` installed and compiled, runs as the
non-root user 65532 (it works with a read-only root filesystem), and starts `agent-authorizer serve -config
/etc/agent-authorizer/agent-authorizer.yaml`. It is built for `linux/amd64` and `linux/arm64`. Pass each `env:`
credential with `-e` and mount each `file:` credential read-only. Pin a version tag rather than
`latest`.

## pip

The server is the `agent-authorizer` command of the same package. Install it into its own virtual
environment and run it under your service manager:

```sh
python3 -m venv /opt/agent-authorizer
/opt/agent-authorizer/bin/pip install "agent-authorizer[crypto]"   # pin it: "agent-authorizer[crypto]==<version>"
```

A systemd unit:

```ini
[Unit]
Description=agent-authorizer
After=network-online.target

[Service]
ExecStart=/opt/agent-authorizer/bin/agent-authorizer serve -config /etc/agent-authorizer/agent-authorizer.yaml
# AGENT_AUTHORIZER_API_KEY=... and each env: credential, one per line
EnvironmentFile=/etc/agent-authorizer/env
DynamicUser=yes
NoNewPrivileges=yes
ProtectSystem=strict
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

## Kubernetes with Helm

The chart runs agent-authorizer as a non-root Deployment with a read-only root filesystem, probes on
`/healthz`, the config in a ConfigMap and the API key in a Secret. Every release from 0.4.1 on
publishes it to GitHub's container registry:

```sh
kubectl create namespace agent-authorizer
kubectl -n agent-authorizer create secret generic agent-authorizer-api-key \
  --from-literal=AGENT_AUTHORIZER_API_KEY="$(openssl rand -hex 32)"

helm install agent-authorizer oci://ghcr.io/roee-hersh/charts/agent-authorizer -n agent-authorizer \
  --set apiKey.existingSecret=agent-authorizer-api-key
helm test agent-authorizer -n agent-authorizer
```

Add `--version` to pin a chart version; each chart version deploys the agent-authorizer image of the same
version. From a clone of the repository, the chart is `deploy/helm/agent-authorizer`.

Your connections go in a values file under `config`, exactly as in `agent-authorizer.yaml`. Credentials stay
references: an `env:NAME` maps to a Secret through `extraEnv`, and a `file:/path` to a Secret
mounted with `extraVolumes` and `extraVolumeMounts`. To answer questions about the cluster it runs
in, set `rbac.subjectAccessReview.create=true`. The [chart README](../../deploy/helm/agent-authorizer/README.md)
has a full values file and every value.

The chart creates a ClusterIP Service only. For a shared service, add your own Ingress or Gateway
route with TLS, for example:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: agent-authorizer
  namespace: agent-authorizer
  annotations:
    cert-manager.io/cluster-issuer: internal-ca
spec:
  ingressClassName: internal
  tls:
    - hosts: [agent-authorizer.example.internal]
      secretName: agent-authorizer-tls
  rules:
    - host: agent-authorizer.example.internal
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: agent-authorizer
                port:
                  number: 8080
```

For a sidecar, add the agent-authorizer image as a second container in the agent's own pod, with the same
config ConfigMap and Secrets, and point the agent at `http://localhost:8080`
(`AgentAuthorizer.remote("http://localhost:8080", key)`). Mount the Secrets
only into the agent-authorizer container, never into the agent's: they answer for every user, and in Jira
they need Administer Jira, so the agent should not be able to read them
([trust boundaries](../concepts/architecture.md#trust-boundaries)).

## Scaling and availability

- The server keeps no state, so run as many replicas as you need behind the Service. Two is a good
  default for a shared service.
- Each replica keeps its own caches. More replicas mean more upstream calls after a restart, never
  a different answer.
- Upstream rate limits usually bind before agent-authorizer does. Keep `decision_cache_seconds` above zero,
  and use `"fresh": true` only for destructive actions.
- A connection that is down does not stop the others; its checks answer `unknown`.

## Upgrading

Releases follow semantic versioning. The `agent-authorizer` package, the Docker image, the Helm chart and
`agent-authorizer-client` on npm all carry the same version. With a server, upgrade it before
the agents when a release adds a request field (such as `fresh`): an older agent-authorizer rejects fields
it does not know, and the clients report that as `unknown`, so nothing fails open.

## Production checklist

- [ ] You chose in-process or a server knowing where the lookup credentials end up.
- [ ] With a server: the API key is random (`openssl rand -hex 32`), stored in a secret manager,
      and given only to the agents' tool layers, and agents reach agent-authorizer on `localhost` or over
      TLS.
- [ ] Every upstream credential is read-only and scoped as its [integration page](../integrations/README.md)
      describes.
- [ ] `agent-authorizer probe` passes for every connection.
- [ ] The decision log goes to your log pipeline (`decision_log: stdout` in containers).
- [ ] [The dashboard](dashboard.md) at `/dashboard/` is behind your ingress's authentication when
      more people than the server's operators should open it: its runs carry tool arguments and are
      served to holders of the API key.
- [ ] Whoever holds the API key can ask what anyone may do, from an assistant too
      ([`/mcp`](ask.md#who-can-ask)): an assistant given the key answers everyone who can talk to it.
- [ ] You alert on a rise in `unknown` decisions; [Operating](operating.md#when-the-answer-is-unknown)
      lists the codes.
- [ ] The image, chart or package is pinned to a version.
- [ ] You ran the [live test](../development/testing.md#against-your-own-systems), with cases like
      [`examples/live-cases.yaml`](../../examples/live-cases.yaml), against your own systems for
      each integration you rely on.
