# Releasing the packages

One release version covers everything the repository ships:

| Package | Source | Where users get it |
|---|---|---|
| `agent-authorizer` (Python: the engine, the client, the `agent-authorizer` command) | [`agent-authorizer-py`](.) | [PyPI](https://pypi.org/project/agent-authorizer/) |
| `agent-authorizer-client` (Node and TypeScript client) | [`agent-authorizer-ts`](../agent-authorizer-ts) | [npm](https://www.npmjs.com/package/agent-authorizer-client) |
| The server image | [`Dockerfile`](../Dockerfile), which installs `agent-authorizer[crypto]` | `ghcr.io/roee-hersh/agent-authorizer` |
| The Helm chart | [`deploy/helm/agent-authorizer`](../deploy/helm/agent-authorizer) | `oci://ghcr.io/roee-hersh/charts/agent-authorizer` |

Each version in the repository is `0.0.0` (the chart's appVersion is `latest`, so a chart
installed from a clone runs the latest published image). The `release` workflow (`.github/workflows/release.yaml`,
started by hand or by `daily-release`) tags `main` and sets the version from the tag everywhere:
`.github/scripts/build-python.sh` writes it into `src/agent_authorizer/_version.py`; the image, the chart
(version and appVersion) and the npm package get it too. The release skill
(`.claude/skills/release/SKILL.md`) lists what to check afterwards.

## Release assets

The `assets` job attaches a registry-free copy of the packages to the GitHub release, under fixed
names so `releases/latest/download/...` works:

| Asset | What it is |
|---|---|
| `agent-authorizer-python.tar.gz` | The `agent-authorizer` source distribution |
| `agent_authorizer-<version>-py3-none-any.whl` | The `agent-authorizer` wheel |
| `agent-authorizer-client-node.tgz` | The npm package, as `npm pack` makes it |
| `checksums.txt` | SHA-256 of each of the above |

## Publishing to PyPI and npm

The `pypi` job publishes `agent-authorizer`, and the `npm` job publishes
`agent-authorizer-ts` as `agent-authorizer-client`, with trusted publishing (OpenID Connect), so no long-lived
token is stored. The jobs run only while the Actions variables `PUBLISH_PYPI` and `PUBLISH_NPM` are
`true`; unset, they are skipped and the release still succeeds, so the release skill checks the
registries after every release.

**PyPI.** `agent-authorizer` needs a trusted publisher. On pypi.org, under Your account → Publishing, add a
pending publisher (the project does not exist yet): project `agent-authorizer`, owner `roee-hersh`,
repository `toolpass`, workflow `release.yaml`, environment `pypi`. The first release that runs
the `pypi` job creates the project. `PUBLISH_PYPI` must be `true`.

**npm.** npm can only configure trusted publishing for a package that exists, so:

1. Publish the first version of `agent-authorizer-client` by hand from a release's
   `agent-authorizer-client-node.tgz`: `npm publish agent-authorizer-client-node.tgz --access public --otp=<code>`
   (the account needs two-factor authentication). Until then, the release's `npm` job finds no
   `agent-authorizer-client` on npm and skips, with a note in the run's summary.
2. On npmjs.com, `agent-authorizer-client` → Settings → Trusted publishing: GitHub Actions, owner
   `roee-hersh`, repository `toolpass`, workflow `release.yaml`, environment `npm`.
3. Set the Actions variable `PUBLISH_NPM` to `true`. The workflow authenticates with its OpenID
   Connect token.
