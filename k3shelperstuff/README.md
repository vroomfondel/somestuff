# k3shelperstuff

Helpers around a K3s/Kubernetes cluster: keep the local kubeconfig in sync with a
remote K3s server, hand out client certificates for new users, and find workloads
whose running image silently lags behind the tag it was deployed from.

| Module                     | Purpose                                                                  |
|----------------------------|--------------------------------------------------------------------------|
| `update_local_k3s_keys.py` | Sync local `~/.kube/config` credentials with a remote K3s server via SSH |
| `k8s_user_cert.py`         | Issue a client certificate for a new user and write it into a kubeconfig |
| `keel_drift.py`            | Find Keel-tracked workloads whose running image is out of date           |

Logging setup is shared via `k3shelperstuff.configure_logging` / `print_banner`
(loguru, with a stdlib-`logging` intercept), mirroring the other packages in this
repo.

## update_local_k3s_keys

Fetches the kubeconfig from a remote K3s server via SSH, compares user
credentials and cluster CA data against the local kubeconfig, and interactively
updates any differences.

- Extracts client certificates, client keys, and cluster CA data from both remote and local kubeconfig
- Shows truncated diffs without exposing full secrets
- Prompts before writing any changes
- Auto-detects remote host and context from the current-context in `~/.kube/config`

```bash
python3 -m k3shelperstuff.update_local_k3s_keys [OPTIONS]
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

## k8s_user_cert

Generates an RSA key and a CSR, has the cluster CA sign it through the
`certificates.k8s.io` API (approving the request on the way), optionally creates
the matching RBAC binding, and merges cluster/user/context entries into a
kubeconfig.

```bash
python3 -m k3shelperstuff.k8s_user_cert extern-admin
python3 -m k3shelperstuff.k8s_user_cert extern-admin -o /tmp/extern-kubeconfig.yaml
python3 -m k3shelperstuff.k8s_user_cert extern-admin --cluster-nickname prod \
    --external-url https://k8s.example.com:6443
python3 -m k3shelperstuff.k8s_user_cert reader --role view             # ClusterRoleBinding
python3 -m k3shelperstuff.k8s_user_cert dev1 --role edit -n dev        # RoleBinding in 'dev'
python3 -m k3shelperstuff.k8s_user_cert someone --no-rbac --group ops  # certificate only
python3 -m k3shelperstuff.k8s_user_cert breakglass --group system:masters   # see below
```

| Option                 | Env var                          | Description                                                                             |
|------------------------|----------------------------------|-----------------------------------------------------------------------------------------|
| `USERNAME` (argument)  | —                                | Name of the new user; becomes the certificate's `CN`                                    |
| `-g`, `--group G`      | `K8S_USER_CERT_GROUPS`           | Group membership, repeatable, becomes an `O` (default: none — the binding decides)      |
| `-i`, `--input PATH`   | `K8S_USER_CERT_INPUT`            | Admin kubeconfig used to talk to the cluster (default: `~/.kube/config`)                |
| `-o`, `--output PATH`  | `K8S_USER_CERT_OUTPUT`           | Target kubeconfig (default: same as `--input`)                                          |
| `--validity SECONDS`   | `K8S_USER_CERT_VALIDITY`         | Requested lifetime (default: one year)                                                  |
| `--external-url URL`   | `K8S_USER_CERT_EXTERNAL_URL`     | Server URL for the generated kubeconfig                                                 |
| `--cluster-nickname N` | `K8S_USER_CERT_CLUSTER_NICKNAME` | Cluster name in the generated kubeconfig                                                |
| `--role ROLE`          | `K8S_USER_CERT_ROLE`             | ClusterRole the binding refers to (default: `cluster-admin`)                            |
| `-n`, `--namespace NS` | `K8S_USER_CERT_NAMESPACE`        | Namespace for a RoleBinding (without: ClusterRoleBinding)                               |
| `--no-rbac`            | `K8S_USER_CERT_NO_RBAC`          | Do not create any RBAC binding                                                          |
| `-v`, `--verbose`      | `K8S_USER_CERT_VERBOSE`          | DEBUG logging                                                                           |

### Groups, and why `system:masters` is not the default

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

### Other things worth knowing

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

## keel_drift

### Why it exists

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

### What it checks

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

### Usage

```bash
python3 -m k3shelperstuff.keel_drift                          # all tracked workloads
python3 -m k3shelperstuff.keel_drift --namespace somestuff    # a single namespace
python3 -m k3shelperstuff.keel_drift --drift-only --quiet     # only deviations, terse
python3 -m k3shelperstuff.keel_drift --fix-command            # emit rollout-restart commands
```

| Option                   | Env var                     | Description                                      |
|--------------------------|-----------------------------|--------------------------------------------------|
| `-n`, `--namespace NS`   | `KEEL_NAMESPACE`            | Only check this namespace                        |
| `--context CTX`          | `KEEL_CONTEXT`              | kubeconfig context instead of the active one     |
| `--drift-only`           | `KEEL_DRIFT_ONLY`           | Only show stale and unknown workloads            |
| `--fix-command`          | `KEEL_FIX_COMMAND`          | Emit `kubectl rollout restart` commands          |
| `-q`, `--quiet`          | `KEEL_QUIET`                | Suppress the table, print the summary only       |
| `-v`, `--verbose`        | `KEEL_VERBOSE`              | Log every workload and registry access (DEBUG)   |
| `--no-local-credentials` | `KEEL_NO_LOCAL_CREDENTIALS` | Ignore the local Docker login, query anonymously |

The table and the summary go to **stdout**, logging and the progress display to
**stderr** — so the result can be piped while the diagnostics stay visible.

Without a kubeconfig the in-cluster service account is used, so the tool also
runs as a `Job`/`CronJob` inside the cluster.

### Exit codes

| Code | Meaning                                                     |
|------|-------------------------------------------------------------|
| `0`  | Nothing stale (or no tracked workloads at all)              |
| `1`  | At least one container is stale — usable as a pipeline gate |
| `2`  | No usable kubeconfig and no in-cluster context              |
