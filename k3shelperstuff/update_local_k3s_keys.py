#!/usr/bin/env python3
"""
Script to compare K3s kubeconfig from remote server with local ~/.kube/config
and optionally update local credentials for a specific context.
"""

import argparse
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml


def get_remote_kubeconfig(host: str, remote_path: str) -> dict:
    """Fetch kubeconfig from remote host via SSH."""
    cmd = ["ssh", host, f"cat {remote_path}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return yaml.safe_load(result.stdout)
    except subprocess.CalledProcessError as e:
        print(f"Error accessing {host} via SSH: {e.stderr}")
        sys.exit(1)


def load_local_kubeconfig(path: Path) -> dict:
    """Load local kubeconfig file."""
    if not path.exists():
        print(f"Local kubeconfig not found: {path}")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


def find_context_user(kubeconfig: dict, context_name: str) -> str | None:
    """Find the user name associated with a context."""
    for ctx in kubeconfig.get("contexts", []):
        if ctx.get("name") == context_name:
            return ctx.get("context", {}).get("user")
    return None


def find_context_cluster(kubeconfig: dict, context_name: str) -> str | None:
    """Find the cluster name associated with a context."""
    for ctx in kubeconfig.get("contexts", []):
        if ctx.get("name") == context_name:
            return ctx.get("context", {}).get("cluster")
    return None


def get_user_credentials(kubeconfig: dict, user_name: str) -> dict:
    """Get credentials for a specific user."""
    for user in kubeconfig.get("users", []):
        if user.get("name") == user_name:
            return user.get("user", {})
    return {}


def get_cluster_ca(kubeconfig: dict, cluster_name: str) -> str | None:
    """Get certificate-authority-data for a specific cluster."""
    for cluster in kubeconfig.get("clusters", []):
        if cluster.get("name") == cluster_name:
            return cluster.get("cluster", {}).get("certificate-authority-data")
    return None


def compare_credentials(remote: dict, local: dict, remote_ca: str, local_ca: str) -> dict:
    """Compare remote and local credentials, return differences."""
    differences = {}

    remote_cert = remote.get("client-certificate-data")
    local_cert = local.get("client-certificate-data")
    if remote_cert != local_cert:
        differences["client-certificate-data"] = {
            "remote": remote_cert[:50] + "..." if remote_cert else None,
            "local": local_cert[:50] + "..." if local_cert else None,
        }

    remote_key = remote.get("client-key-data")
    local_key = local.get("client-key-data")
    if remote_key != local_key:
        differences["client-key-data"] = {
            "remote": remote_key[:50] + "..." if remote_key else None,
            "local": local_key[:50] + "..." if local_key else None,
        }

    if remote_ca != local_ca:
        differences["certificate-authority-data"] = {
            "remote": remote_ca[:50] + "..." if remote_ca else None,
            "local": local_ca[:50] + "..." if local_ca else None,
        }

    return differences


def update_local_kubeconfig(
    local_path: Path,
    kubeconfig: dict,
    user_name: str,
    cluster_name: str,
    remote_creds: dict,
    remote_ca: str,
) -> None:
    """Update local kubeconfig with remote credentials."""
    # Update user credentials
    for user in kubeconfig.get("users", []):
        if user.get("name") == user_name:
            user["user"]["client-certificate-data"] = remote_creds.get(
                "client-certificate-data"
            )
            user["user"]["client-key-data"] = remote_creds.get("client-key-data")
            break

    # Update cluster CA
    for cluster in kubeconfig.get("clusters", []):
        if cluster.get("name") == cluster_name:
            cluster["cluster"]["certificate-authority-data"] = remote_ca
            break

    # Write back
    with open(local_path, "w") as f:
        yaml.dump(kubeconfig, f, default_flow_style=False)

    print(f"Local kubeconfig updated: {local_path}")


def get_defaults_from_kubeconfig() -> tuple[str | None, str | None]:
    """Try to read default host and context from local kubeconfig's current-context."""
    local_path = Path.home() / ".kube" / "config"
    if not local_path.exists():
        return None, None

    try:
        with open(local_path) as f:
            config = yaml.safe_load(f)
    except Exception:
        return None, None

    context = config.get("current-context")
    host = None

    if context:
        cluster_name = find_context_cluster(config, context)
        if cluster_name:
            for cluster in config.get("clusters", []):
                if cluster.get("name") == cluster_name:
                    server = cluster.get("cluster", {}).get("server", "")
                    parsed = urlparse(server)
                    if parsed.hostname:
                        host = parsed.hostname

    return host, context


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    default_host, default_context = get_defaults_from_kubeconfig()

    parser = argparse.ArgumentParser(
        description="Compare K3s kubeconfig with local ~/.kube/config"
    )
    parser.add_argument(
        "-u", "--user",
        default="root",
        help="SSH user for remote connection (default: root)",
    )
    parser.add_argument(
        "-H", "--host",
        default=default_host,
        help=f"Remote host (default: {default_host or 'from kubeconfig'})",
    )
    parser.add_argument(
        "-c", "--context",
        default=default_context,
        help=f"Local kubeconfig context (default: {default_context or 'current-context from kubeconfig'})",
    )

    args = parser.parse_args()

    if not args.host:
        print("Error: could not determine remote host from kubeconfig. Please specify with -H.")
        sys.exit(1)
    if not args.context:
        print("Error: could not determine context from kubeconfig. Please specify with -c.")
        sys.exit(1)

    return args


def main():
    args = parse_args()

    remote_host = f"{args.user}@{args.host}"
    remote_kubeconfig_path = "/etc/rancher/k3s/k3s.yaml"
    local_kubeconfig_path = Path.home() / ".kube" / "config"
    target_context = args.context

    print(f"Fetching kubeconfig from {remote_host}:{remote_kubeconfig_path}...")
    remote_kubeconfig = get_remote_kubeconfig(remote_host, remote_kubeconfig_path)

    print(f"Loading local kubeconfig: {local_kubeconfig_path}...")
    local_kubeconfig = load_local_kubeconfig(local_kubeconfig_path)

    # Find user and cluster for target context
    local_user = find_context_user(local_kubeconfig, target_context)
    local_cluster = find_context_cluster(local_kubeconfig, target_context)

    if not local_user:
        print(f"Context '{target_context}' not found in local kubeconfig.")
        sys.exit(1)

    print(f"Context: {target_context}")
    print(f"  User: {local_user}")
    print(f"  Cluster: {local_cluster}")

    # Get credentials from both configs
    # Remote uses default user name from k3s.yaml (usually "default")
    remote_users = remote_kubeconfig.get("users", [])
    if not remote_users:
        print("No users found in remote kubeconfig.")
        sys.exit(1)
    remote_creds = remote_users[0].get("user", {})

    # Remote CA from first cluster
    remote_clusters = remote_kubeconfig.get("clusters", [])
    remote_ca = (
        remote_clusters[0].get("cluster", {}).get("certificate-authority-data")
        if remote_clusters
        else None
    )

    local_creds = get_user_credentials(local_kubeconfig, local_user)
    local_ca = get_cluster_ca(local_kubeconfig, local_cluster)

    # Compare
    print("\nComparing certificate data...")
    differences = compare_credentials(remote_creds, local_creds, remote_ca, local_ca)

    if not differences:
        print("All certificate data matches.")
        sys.exit(0)

    print("\nDifferences found:")
    for key, vals in differences.items():
        print(f"  {key}:")
        print(f"    Remote: {vals['remote']}")
        print(f"    Local:  {vals['local']}")

    # Ask user
    print()
    response = input(
        f"Update local credentials for context '{target_context}'? [y/N]: "
    )

    if response.lower() in ("y", "yes"):
        update_local_kubeconfig(
            local_kubeconfig_path,
            local_kubeconfig,
            local_user,
            local_cluster,
            remote_creds,
            remote_ca,
        )
    else:
        print("No changes made.")


if __name__ == "__main__":
    main()