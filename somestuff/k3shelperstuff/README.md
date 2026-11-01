# k3shelperstuff

Helpers around a K3s/Kubernetes cluster: keep the local kubeconfig in sync with a
remote K3s server, hand out client certificates for new users through the CSR API,
and find workloads whose running image silently lags behind the tag or pin it was
deployed from.

## Modules

- **`update_local_k3s_keys.py`** — fetches the kubeconfig from a remote K3s server via
  SSH, compares user credentials and cluster CA data with the local kubeconfig and
  interactively updates the differences.
- **`k8s_user_cert.py`** — issues a client certificate for a new user through the
  `certificates.k8s.io` API, optionally creates the RBAC binding and writes the result
  into a kubeconfig.
- **`keel_drift.py`** — finds Keel-tracked workloads whose running image is out of date.
- **`pin_drift.py`** — finds version pins in a repository that lag behind their upstream
  release.
- **`drift.py`** — `pin_drift` and `keel_drift` as subcommands `pin`/`keel` of one CLI.

Logging (loguru, with a stdlib `logging` intercept) and the startup banner are shared
via `somestuff.k3shelperstuff`.

## Installation

```bash
pip install somestuff-k3shelperstuff
```

`somestuff` is a PEP 420 namespace; the package is imported as
`somestuff.k3shelperstuff` and shares the namespace with the other `somestuff-*`
distributions.

## Usage

Console scripts and module invocation are equivalent:

| Console script          | Module                                           |
|-------------------------|--------------------------------------------------|
| `update-local-k3s-keys` | `somestuff.k3shelperstuff.update_local_k3s_keys` |
| `k8s-user-cert`         | `somestuff.k3shelperstuff.k8s_user_cert`         |
| `keel-drift`            | `somestuff.k3shelperstuff.keel_drift`            |
| `pin-drift`             | `somestuff.k3shelperstuff.pin_drift`             |
| `k3s-drift`             | `somestuff.k3shelperstuff.drift`                 |

### update-local-k3s-keys

Fetches the kubeconfig from a remote K3s server via SSH, compares user
credentials and cluster CA data against the local kubeconfig, and interactively
updates any differences.

- Extracts client certificates, client keys, and cluster CA data from both remote and local kubeconfig
- Shows truncated diffs without exposing full secrets
- Prompts before writing any changes
- Auto-detects remote host and context from the current-context in `~/.kube/config`

```bash
update-local-k3s-keys [OPTIONS]
python3 -m somestuff.k3shelperstuff.update_local_k3s_keys [OPTIONS]
```

| Option                    | Description                                                       |
|---------------------------|-------------------------------------------------------------------|
| `-u`, `--user USER`       | SSH user (default: `root`)                                        |
| `-H`, `--host HOST`       | Remote host (auto-detected from kubeconfig server URL)            |
| `-c`, `--context CONTEXT` | Local kubeconfig context (auto-detected from current-context)     |
| `--create`                | Create context/cluster/user if the context does not exist locally |
| `-y`, `--yes`             | Non-interactive mode (skip confirmation prompts)                  |
| `--server URL`            | Override the K3s API server URL (default: `https://{host}:6443`)  |

The remote kubeconfig is read from `/etc/rancher/k3s/k3s.yaml` on the target host.

### k8s-user-cert

Generates an RSA key and a CSR, has the cluster CA sign it through the
`certificates.k8s.io` API (approving the request on the way), optionally creates
the matching RBAC binding, and merges cluster/user/context entries into a
kubeconfig.

```bash
k8s-user-cert extern-admin
k8s-user-cert extern-admin -o /tmp/extern-kubeconfig.yaml
k8s-user-cert extern-admin --cluster-nickname prod --external-url https://k8s.example.com:6443
k8s-user-cert reader --role view                  # ClusterRoleBinding
k8s-user-cert dev1 --role edit -n dev             # RoleBinding in 'dev'
k8s-user-cert someone --no-rbac --group ops       # certificate only
k8s-user-cert breakglass --group system:masters   # see below
python3 -m somestuff.k3shelperstuff.k8s_user_cert extern-admin
```

| Option                 | Env var                          | Description                                                                        |
|------------------------|----------------------------------|------------------------------------------------------------------------------------|
| `USERNAME` (argument)  | —                                | Name of the new user; becomes the certificate's `CN`                               |
| `-g`, `--group G`      | `K8S_USER_CERT_GROUPS`           | Group membership, repeatable, becomes an `O` (default: none — the binding decides) |
| `-i`, `--input PATH`   | `K8S_USER_CERT_INPUT`            | Admin kubeconfig used to talk to the cluster (default: `~/.kube/config`)           |
| `-o`, `--output PATH`  | `K8S_USER_CERT_OUTPUT`           | Target kubeconfig (default: same as `--input`)                                     |
| `--validity SECONDS`   | `K8S_USER_CERT_VALIDITY`         | Requested lifetime (default: one year)                                             |
| `--external-url URL`   | `K8S_USER_CERT_EXTERNAL_URL`     | Server URL for the generated kubeconfig                                            |
| `--cluster-nickname N` | `K8S_USER_CERT_CLUSTER_NICKNAME` | Cluster name in the generated kubeconfig                                           |
| `--role ROLE`          | `K8S_USER_CERT_ROLE`             | ClusterRole the binding refers to (default: `cluster-admin`)                       |
| `-n`, `--namespace NS` | `K8S_USER_CERT_NAMESPACE`        | Namespace for a RoleBinding (without: ClusterRoleBinding)                          |
| `--no-rbac`            | `K8S_USER_CERT_NO_RBAC`          | Do not create any RBAC binding                                                     |
| `-v`, `--verbose`      | `K8S_USER_CERT_VERBOSE`          | DEBUG logging                                                                      |


#### Groups, and why `system:masters` is not the default

A client certificate's identity is entirely its subject: `CN` is the user name,
every `O` is a group. **By default no group is set**, so what the user may do is
decided solely by the RBAC binding — `--role` and `--namespace` mean what they
say.

`--group system:masters` is the exception you have to ask for explicitly: the API
server hard-wires that group to full cluster-admin **before RBAC is consulted**.
Such a certificate cannot be restricted by `--role`, `-n` or `--no-rbac` (those
then only decide which redundant binding gets written), and it cannot be revoked
through RBAC either — a short `--validity` is the only lever left. The tool warns
when it is used, and louder when it contradicts a requested restriction.

It also warns about the opposite mistake: no group *and* `--no-rbac` yields a
certificate that authenticates but is allowed to do nothing.

#### Other things worth knowing

- The requested `--validity` is a **wish**: kube-controller-manager caps every
  signature at its `--cluster-signing-duration` (one hour in some setups). The
  tool therefore logs the `notAfter` it actually received.
- Without `-o`, the admin kubeconfig is **rewritten in place** — `yaml.dump`
  drops comments and ordering, and `current-context` is moved to the new user.
  That case is logged as a warning.
- The written kubeconfig contains the private key and is created with mode
  `0600`.
- Re-running for the same user replaces the cluster/user/context entries instead
  of duplicating them, and RBAC bindings are deleted before being recreated
  (`roleRef` is immutable).

### keel-drift

#### Why it exists

On every poll [Keel](https://keel.sh) compares **only** the registry digest of
right now against the digest it memorised during its previous poll
(`trigger/poll/single_tag_watcher.go`). That memo lives in memory alone and is
seeded from the registry at startup (`trigger/poll/watcher.go`, `addJob`).

Which means: **what actually runs in the cluster never enters Keel's decision.**
If a tag is moved while Keel restarts, Keel sets its baseline to the new digest
without ever touching the corresponding deployment — the change stays invisible
until the next push.

`keel_drift.py` makes exactly the comparison Keel does not: **running pod digest
against the digest the tag currently points at.**

#### What it checks

- All `Deployment`/`StatefulSet`/`DaemonSet` objects carrying a `keel.sh/policy`
  annotation or label (`never`/empty is treated as untracked, exactly as Keel does)
- Regular containers **and** initContainers — the latter are flagged when they are
  stale while `keel.sh/initContainers: "true"` is missing, i.e. where Keel would
  never act
- `imagePullPolicy != Always` on a stale container — there a restart (and Keel's
  force policy) cannot re-pull the unchanged tag at all
- Multi-arch tags: both the index digest and its platform manifests count as a
  match, and an older index pointing at the same platform manifests is reported
  as current rather than as a false positive

Credentials come from the workload's `imagePullSecrets`; the local
`~/.docker/config.json` (or `$DOCKER_CONFIG`) is used as a fallback, because
anonymous Docker Hub access is capped at 100 manifest requests per hour and IP.

```bash
keel-drift                          # all tracked workloads
keel-drift --namespace somestuff    # a single namespace
keel-drift --drift-only --quiet     # only deviations, terse
keel-drift --fix-command            # emit rollout-restart commands
python3 -m somestuff.k3shelperstuff.keel_drift --drift-only
```

| Option                   | Env var                        | Description                                                          |
|--------------------------|--------------------------------|----------------------------------------------------------------------|
| `-n`, `--namespace NS`   | `KEEL_NAMESPACE`               | Only check this namespace                                            |
| `-c`, `--context CTX`    | `KEEL_CONTEXT`, `KUBE_CONTEXT` | kubeconfig context instead of the active one                         |
| `--drift-only`           | `KEEL_DRIFT_ONLY`              | Only show stale and unclear workloads                                |
| `--fix-command`          | `KEEL_FIX_COMMAND`             | Emit `kubectl rollout restart` commands (with `--context`, if given) |
| `-q`, `--quiet`          | `KEEL_QUIET`                   | Suppress the table, print the summary only                           |
| `-v`, `--verbose`        | `KEEL_VERBOSE`                 | Log every workload and registry access (DEBUG)                       |
| `--no-local-credentials` | `KEEL_NO_LOCAL_CREDENTIALS`    | Ignore the local Docker login, query anonymously                     |

The table and the summary go to **stdout**, logging and the progress display to
**stderr** — so the result can be piped while the diagnostics stay visible.

Without a kubeconfig the in-cluster service account is used, so the tool also
runs as a `Job`/`CronJob` inside the cluster.

#### Exit codes

| Code | Meaning                                                                                          |
|------|--------------------------------------------------------------------------------------------------|
| `0`  | Nothing stale (or no tracked workloads at all)                                                   |
| `1`  | At least one container is stale — usable as a pipeline gate                                      |
| `2`  | Unusable `--context` (the available ones are listed), or no kubeconfig and no in-cluster context |

### pin-drift

Counterpart to `keel-drift`: that one checks a rolling tag by digest, this one
checks a **fixed pin** (`v1.2.3`) against the release list of the project it
comes from. Read-only — it reads the repo files, the release APIs and, if a
kubeconfig is usable, the pods and nodes of the cluster, nothing else.

The pins are declared in `pin_drift.yml`, searched upwards from the current
directory (or given via `--config`). Paths in it are relative to the file
itself.

```yaml
pins:
  - name: mosquitto
    file: mosquitto-2.1/Dockerfile
    pattern: 'mosquitto/archive/refs/tags/v([\d.]+)\.tar\.gz'
    github: eclipse-mosquitto/mosquitto
    source: tags
  - name: some-chart
    files: [values.yaml, values-prod.yaml]
    var: app_version                 # matches `app_version: 1.4.2`
    forgejo: git.example.com/owner/repo
  - name: grafana
    file: deploy/grafana.yaml
    image: grafana/grafana           # matches `grafana/grafana:11.2.0`
    github: grafana/grafana
    tag_pattern: '^v\d+\.\d+\.\d+$'
```

| Key                  | Meaning                                                                     |
|----------------------|-----------------------------------------------------------------------------|
| `name`               | Unique name, used by `--only`                                               |
| `file` / `files`     | File(s) carrying the pin; several files must agree on the same value        |
| `var`                | Extract from a `key: value` line                                            |
| `image`              | Extract the tag after `image:`                                              |
| `pattern`            | Raw regex with exactly one capture group                                    |
| `github` / `forgejo` | `owner/repo` or `host/owner/repo` the releases come from                    |
| `source`             | `releases` (default, drafts/prereleases skipped) or `tags`                  |
| `tag_pattern`        | Filter for upstream tags (default: `^v?\d+(?:\.\d+)+$`)                     |
| `running_image`      | Image(s) whose tags in live pods are the running version (default: `image`) |
| `running_kubelet`    | `true`: the running version is the nodes' kubelet version (k3s, k8s)        |

Exactly one of `var`/`image`/`pattern` and one of `github`/`forgejo` is required.
A pin shorter than the upstream version (`5.1` against `5.1.3`) is a floating
tag and counts as current until a `5.2` appears. Files encrypted with git-crypt
are reported as `unclear` instead of failing.

#### Running column

With a usable kubeconfig (or in-cluster context) the table gets a `running`
column: the distinct tags of the matching images across all live pods
(containers and initContainers, finished pods ignored), or the kubelet versions
for `running_kubelet` pins. Image names are compared normalised (`redis` ==
`docker.io/library/redis`), tags ignoring a leading `v`. A pin whose running
version differs is noted as `not rolled out`, or `partially rolled out` if the
pinned version runs alongside older ones; this does not affect the exit code.
`-` means no matching pod — expected for pins outside the cluster.

GitHub allows 60 anonymous API requests per hour; a token is taken from
`GITHUB_TOKEN` / `GH_TOKEN` or `gh auth token`.

```bash
pin-drift                        # every declared pin
pin-drift --updates-only         # hide the pins that are current
pin-drift --only mosquitto       # a single pin (repeatable)
pin-drift --config ../pin_drift.yml
pin-drift --context ht@heidk8    # running versions from this context
pin-drift --no-running           # repo against upstream only
python3 -m somestuff.k3shelperstuff.pin_drift --updates-only
```

| Option              | Env var                             | Description                                   |
|---------------------|-------------------------------------|-----------------------------------------------|
| `-c`, `--config P`  | `PIN_DRIFT_CONFIG`                  | Pin declarations (default: search upwards)    |
| `-o`, `--only NAME` | `PIN_DRIFT_ONLY`                    | Check only this pin (repeatable)              |
| `--updates-only`    | `PIN_DRIFT_UPDATES_ONLY`            | Hide the pins that are current and rolled out |
| `--context CTX`     | `PIN_DRIFT_CONTEXT`, `KUBE_CONTEXT` | kubeconfig context (default: active one)      |
| `--no-running`      | `PIN_DRIFT_NO_RUNNING`              | Do not query the cluster                      |
| `-q`, `--quiet`     | `PIN_DRIFT_QUIET`                   | Suppress the table, print the summary only    |
| `-v`, `--verbose`   | `PIN_DRIFT_VERBOSE`                 | DEBUG logging, third-party libraries included |

#### Exit codes

| Code | Meaning                                                                                  |
|------|------------------------------------------------------------------------------------------|
| `0`  | No update available (pins current or unclear)                                            |
| `1`  | At least one pin has a major/minor/patch update — pipeline gate                          |
| `2`  | No usable `pin_drift.yml`, an unknown `--only` name, or an unusable explicit `--context` |

### k3s-drift

Both checks behind one CLI, so `--help` lists them side by side instead of only
showing the options of whichever tool happens to be the default.

```bash
k3s-drift --help                # lists pin and keel
k3s-drift pin [pin options]     # same options as pin-drift
k3s-drift keel [keel options]   # same options as keel-drift
python3 -m somestuff.k3shelperstuff.drift pin --updates-only
```

## Environment variables

- `K8S_USER_CERT_*` — one per `k8s-user-cert` option, see the table above.
- `KEEL_*` — one per `keel-drift` option, see the table above.
- `PIN_DRIFT_*` — one per `pin-drift` option, see the table above.
- `KUBE_CONTEXT` — fallback kubeconfig context for `keel-drift` and `pin-drift`
  when `KEEL_CONTEXT` / `PIN_DRIFT_CONTEXT` is not set.
- `DRIFT_PROG_NAME` — overrides the program name shown in `--help` of
  `python3 -m somestuff.k3shelperstuff.drift`, for wrapper scripts that call the
  module.
- `GITHUB_TOKEN` / `GH_TOKEN` — GitHub token for `pin-drift` (see above).
- `DOCKER_CONFIG` — directory of the Docker `config.json` used by `keel-drift`.

## Requirements

- `update-local-k3s-keys` runs `ssh <host> cat /etc/rancher/k3s/k3s.yaml`, so it
  needs non-interactive SSH access to the K3s server and a user allowed to read
  that file (default `root`).
- `k8s-user-cert` needs an admin kubeconfig that may create
  `CertificateSigningRequest` objects, approve them and, unless `--no-rbac` is
  given, create the bindings.
- `keel-drift` and `pin-drift` only read from the cluster; without a kubeconfig the
  in-cluster service account is used. `pin-drift --no-running` does not touch the
  cluster at all.

## Notes

- Requires: `loguru`, `typer`, `kubernetes`, `cryptography`, `pydantic`, `requests`,
  `urllib3`, `rich`, `tabulate`, `PyYAML`.
- Licensed under the LGPL, see `LICENSE.md` in the repository root.
