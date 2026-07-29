#!/usr/bin/env python3
"""Create a client certificate for a Kubernetes user and write it into a kubeconfig.

Generates an RSA key and a CSR, has the cluster's CA sign it through the
``certificates.k8s.io`` API (approving the request on the way), optionally
creates the matching RBAC binding, and merges cluster/user/context entries into
a kubeconfig.

The identity a Kubernetes client certificate carries is entirely in its subject:
``CN`` becomes the user name, every ``O`` becomes a group. Which is why
``--group`` is the option to look at twice — see the warning below.

Usage::

    python3 -m k3shelperstuff.k8s_user_cert extern-admin
    python3 -m k3shelperstuff.k8s_user_cert extern-admin -o /tmp/extern-kubeconfig.yaml
    python3 -m k3shelperstuff.k8s_user_cert extern-admin --cluster-nickname prod \\
        --external-url https://k8s.example.com:6443
    python3 -m k3shelperstuff.k8s_user_cert reader --role view            # ClusterRoleBinding
    python3 -m k3shelperstuff.k8s_user_cert dev1 --role edit -n dev       # RoleBinding in 'dev'
    python3 -m k3shelperstuff.k8s_user_cert someone --no-rbac --group ops # certificate only
    python3 -m k3shelperstuff.k8s_user_cert breakglass --group system:masters   # see below

Every option can also be given as a ``K8S_USER_CERT_*`` environment variable
(the CLI option wins).

By default the certificate carries NO group, so what the user may do is decided
entirely by the RBAC binding — ``--role`` and ``--namespace`` mean what they say.

WARNING — ``--group system:masters``:
    That group is hard-wired to full cluster-admin in the API server, *before*
    RBAC is consulted. A certificate carrying it cannot be restricted by
    ``--role``, ``--namespace`` or ``--no-rbac`` — those then only decide which
    (redundant) binding is written. It also cannot be revoked through RBAC;
    short-lived certificates via ``--validity`` are the only lever left.

Exit codes:

* ``0`` — certificate issued and kubeconfig written.
* ``1`` — the admin kubeconfig could not be loaded, the CSR was not signed in
  time, or the kubeconfig could not be written.

Author: vroomfondel
Source: https://github.com/vroomfondel/somestuff/blob/main/k3shelperstuff/k8s_user_cert.py
"""

import base64
import logging
import time
from pathlib import Path
from typing import Any

import typer
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from kubernetes import client, config
from kubernetes.client.rest import ApiException

from k3shelperstuff import configure_logging, print_banner

logger = logging.getLogger(__name__)

# One year. The API server may hand out less: kube-controller-manager caps every
# signed certificate at its --cluster-signing-duration (1h in some setups), and
# expirationSeconds is a WISH, not a guarantee. Hence the tool logs the notAfter
# it actually got rather than the value that was requested.
DEFAULT_VALIDITY_SECONDS = 365 * 24 * 60 * 60

# Not a default, but the group to recognise: the API server maps it to
# cluster-admin before RBAC runs, so a certificate carrying it cannot be
# restricted. Certificates get no group unless one is asked for explicitly.
SYSTEM_MASTERS_GROUP = "system:masters"

KEY_SIZE = 2048

# Signing is asynchronous: the controller picks the CSR up after approval. Ten
# seconds is plenty for a healthy control plane and short enough that a broken
# signer does not hang a playbook.
SIGN_WAIT_ATTEMPTS = 10
SIGN_WAIT_SECONDS = 1.0

# A kubeconfig written by this tool contains a private key. 0600 from the start,
# because the default umask would leave it world-readable.
KUBECONFIG_MODE = 0o600


class K8sUserCertCreator:
    """Issues Kubernetes user certificates and updates a kubeconfig.

    Attributes:
        username: The user name, which becomes the certificate's ``CN``.
        groups: Group memberships, each becoming an ``O`` in the subject.
        input_kubeconfig: Admin kubeconfig used to talk to the cluster.
        output_kubeconfig: Where the resulting entries are merged into.
        validity_seconds: Requested certificate lifetime.
        external_server_url: Server URL to write into the kubeconfig instead of
            the one from the admin kubeconfig.
        cluster_role: ClusterRole referenced by the RBAC binding.
        create_rbac: Whether to create an RBAC binding at all.
        rbac_namespace: Namespace for a RoleBinding; ``None`` yields a
            ClusterRoleBinding.
    """

    def __init__(
        self,
        username: str,
        groups: list[str] | None = None,
        input_kubeconfig: Path | None = None,
        output_kubeconfig: Path | None = None,
        validity_seconds: int | None = None,
        external_server_url: str | None = None,
        cluster_nickname: str | None = None,
        cluster_role: str | None = None,
        create_rbac: bool = True,
        rbac_namespace: str | None = None,
    ) -> None:
        """Collect the settings without touching the cluster yet.

        Args:
            username: Name of the new Kubernetes user.
            groups: Group memberships; ``None`` or empty means no group at all,
                which leaves every permission to the RBAC binding. Empty strings
                are dropped, so ``--group ''`` is the same as omitting it.
            input_kubeconfig: Admin kubeconfig; ``None`` takes ``~/.kube/config``.
            output_kubeconfig: Target kubeconfig; ``None`` writes back into the
                input file.
            validity_seconds: Requested lifetime; ``None`` takes one year.
            external_server_url: Server URL for the generated kubeconfig;
                ``None`` keeps the one from the admin kubeconfig.
            cluster_nickname: Cluster name in the generated kubeconfig; ``None``
                takes the name from the admin kubeconfig.
            cluster_role: ClusterRole to bind; ``None`` takes ``cluster-admin``.
            create_rbac: Whether to create the binding.
            rbac_namespace: Namespace for a RoleBinding; ``None`` yields a
                ClusterRoleBinding.
        """
        self.username = username
        self.groups = [g for g in (groups or []) if g]
        self.input_kubeconfig = input_kubeconfig or (Path.home() / ".kube" / "config")
        self.output_kubeconfig = output_kubeconfig or self.input_kubeconfig
        self.validity_seconds = validity_seconds or DEFAULT_VALIDITY_SECONDS
        self.external_server_url = external_server_url
        self._cluster_nickname_override = cluster_nickname  # None = read from kubeconfig
        self.cluster_nickname = cluster_nickname or ""
        self.cluster_role = cluster_role or "cluster-admin"
        self.create_rbac = create_rbac
        self.rbac_namespace = rbac_namespace  # None = ClusterRoleBinding, else RoleBinding

    # -- certificate -------------------------------------------------------

    def create_private_key(self) -> rsa.RSAPrivateKey:
        """Generate an RSA private key.

        Returns:
            The freshly generated key; it never leaves this process except as
            the ``client-key-data`` written into the kubeconfig.
        """
        return rsa.generate_private_key(public_exponent=65537, key_size=KEY_SIZE)

    def create_csr(self, key: rsa.RSAPrivateKey, username: str) -> x509.CertificateSigningRequest:
        """Build the certificate signing request.

        The subject IS the identity: ``CN`` becomes the user name, each ``O``
        becomes a group. Nothing else about the request is authorising.

        Args:
            key: The private key to sign the request with.
            username: Name that goes into the ``CN``.

        Returns:
            The signed CSR, ready to be submitted.
        """
        attributes = [x509.NameAttribute(NameOID.COMMON_NAME, username)]
        attributes += [x509.NameAttribute(NameOID.ORGANIZATION_NAME, group) for group in self.groups]
        return x509.CertificateSigningRequestBuilder().subject_name(x509.Name(attributes)).sign(key, hashes.SHA256())

    def _sign_csr(self, csr_pem: str) -> str | None:
        """Submit, approve and collect the certificate.

        Args:
            csr_pem: The PEM-encoded certificate signing request.

        Returns:
            The PEM-encoded certificate, or ``None`` when the signer did not
            produce one within :data:`SIGN_WAIT_ATTEMPTS` attempts.
        """
        certs_api = client.CertificatesV1Api()
        csr_name = f"{self.username}-csr"

        # A CSR object of the same name may still be lying around from an earlier
        # run; its status is immutable, so it has to go before resubmitting.
        try:
            certs_api.delete_certificate_signing_request(name=csr_name)
        except ApiException:
            pass

        csr_object = client.V1CertificateSigningRequest(
            metadata=client.V1ObjectMeta(name=csr_name),
            spec=client.V1CertificateSigningRequestSpec(
                request=base64.b64encode(csr_pem.encode("utf-8")).decode("utf-8"),
                signer_name="kubernetes.io/kube-apiserver-client",
                usages=["client auth"],
                expiration_seconds=self.validity_seconds,
            ),
        )

        logger.info(f"submitting CSR (requested validity: {self.validity_seconds} s)")
        certs_api.create_certificate_signing_request(body=csr_object)

        logger.info("approving CSR")
        csr_object = certs_api.read_certificate_signing_request(name=csr_name)
        csr_object.status.conditions = [
            client.V1CertificateSigningRequestCondition(
                type="Approved",
                status="True",
                reason="ScriptApproval",
                message="Approved by k3shelperstuff.k8s_user_cert",
            )
        ]
        certs_api.patch_certificate_signing_request_approval(name=csr_name, body=csr_object)

        logger.info("waiting for the certificate")
        for _ in range(SIGN_WAIT_ATTEMPTS):
            csr_resp = certs_api.read_certificate_signing_request(name=csr_name)
            if csr_resp.status.certificate:
                return str(base64.b64decode(csr_resp.status.certificate).decode("utf-8"))
            time.sleep(SIGN_WAIT_SECONDS)
        return None

    # -- RBAC --------------------------------------------------------------

    def _apply_rbac(self) -> None:
        """Create the RoleBinding respectively ClusterRoleBinding for the user.

        The binding is deleted first: ``roleRef`` is immutable, so a binding of
        the same name from an earlier run with a different role could not be
        updated in place.
        """
        rbac_api = client.RbacAuthorizationV1Api()

        # Binding name: unique per user, scope and role.
        if self.rbac_namespace:
            binding_name = f"{self.username}-{self.rbac_namespace}-{self.cluster_role}"
        else:
            binding_name = f"{self.username}-cluster-{self.cluster_role}"

        subjects = [client.V1Subject(kind="User", name=self.username, api_group="rbac.authorization.k8s.io")]
        role_ref = client.V1RoleRef(kind="ClusterRole", name=self.cluster_role, api_group="rbac.authorization.k8s.io")

        if self.rbac_namespace:
            try:
                rbac_api.delete_namespaced_role_binding(name=binding_name, namespace=self.rbac_namespace)
            except ApiException:
                pass
            rbac_api.create_namespaced_role_binding(
                namespace=self.rbac_namespace,
                body=client.V1RoleBinding(
                    metadata=client.V1ObjectMeta(name=binding_name, namespace=self.rbac_namespace),
                    subjects=subjects,
                    role_ref=role_ref,
                ),
            )
            logger.info(f"RoleBinding ({self.cluster_role}) set in namespace '{self.rbac_namespace}'")
        else:
            try:
                rbac_api.delete_cluster_role_binding(name=binding_name)
            except ApiException:
                pass
            rbac_api.create_cluster_role_binding(
                body=client.V1ClusterRoleBinding(
                    metadata=client.V1ObjectMeta(name=binding_name),
                    subjects=subjects,
                    role_ref=role_ref,
                )
            )
            logger.info(f"ClusterRoleBinding ({self.cluster_role}) set")

    # -- kubeconfig --------------------------------------------------------

    @staticmethod
    def upsert_in_list(resource_list: list[dict[str, Any]], new_item: dict[str, Any], key: str = "name") -> None:
        """Add an item, or replace the existing one with the same name.

        Args:
            resource_list: One of the kubeconfig's ``clusters``/``users``/
                ``contexts`` lists.
            new_item: The entry to insert.
            key: Field the entries are identified by.
        """
        for i, item in enumerate(resource_list):
            if item.get(key) == new_item.get(key):
                resource_list[i] = new_item
                return
        resource_list.append(new_item)

    def _source_cluster(self) -> tuple[str, str]:
        """Read CA data and server URL of the active cluster from the input.

        Returns:
            The ``certificate-authority-data`` and the ``server`` URL.

        Raises:
            ValueError: The active context names a cluster that is not in the
                file, or that cluster embeds no CA data.
        """
        # Explicitly against the INPUT file: without config_file this reads the
        # default kubeconfig, which with -i would silently describe a different
        # cluster than the one just talked to.
        current_cluster_name = config.list_kube_config_contexts(config_file=str(self.input_kubeconfig))[1]["context"][
            "cluster"
        ]
        self.cluster_nickname = self._cluster_nickname_override or current_cluster_name

        with open(self.input_kubeconfig) as f:
            local_config = yaml.safe_load(f)

        entry = next((c for c in local_config.get("clusters") or [] if c["name"] == current_cluster_name), None)
        if entry is None:
            raise ValueError(f"cluster '{current_cluster_name}' not found in {self.input_kubeconfig}")

        ca_data = entry["cluster"].get("certificate-authority-data")
        if not ca_data:
            # A kubeconfig may reference the CA by path instead of embedding it;
            # the generated one has to be self-contained, so read and embed it.
            ca_path = entry["cluster"].get("certificate-authority")
            if not ca_path:
                raise ValueError(f"cluster '{current_cluster_name}' in {self.input_kubeconfig} carries no CA")
            ca_data = base64.b64encode(Path(ca_path).read_bytes()).decode()

        return str(ca_data), str(entry["cluster"]["server"])

    def _write_kubeconfig(self, signed_cert: str, key_pem: str) -> str:
        """Merge cluster, user and context into the target kubeconfig.

        Args:
            signed_cert: The PEM-encoded certificate.
            key_pem: The PEM-encoded private key.

        Returns:
            The name of the context that was written and made current.

        Raises:
            ValueError: The input kubeconfig does not yield a usable cluster.
        """
        ca_data, original_server = self._source_cluster()
        final_server_url = self.external_server_url or original_server

        target_config: dict[str, Any] = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [],
            "users": [],
            "contexts": [],
        }
        if self.output_kubeconfig.exists():
            with open(self.output_kubeconfig) as f:
                loaded = yaml.safe_load(f)
            if loaded:
                target_config = loaded
            for k in ("clusters", "users", "contexts"):
                if target_config.get(k) is None:
                    target_config[k] = []

        self.upsert_in_list(
            target_config["clusters"],
            {
                "name": self.cluster_nickname,
                "cluster": {"certificate-authority-data": ca_data, "server": final_server_url},
            },
        )
        self.upsert_in_list(
            target_config["users"],
            {
                "name": self.username,
                "user": {
                    "client-certificate-data": base64.b64encode(signed_cert.encode()).decode(),
                    "client-key-data": base64.b64encode(key_pem.encode()).decode(),
                },
            },
        )
        context_name = f"{self.username}@{self.cluster_nickname}"
        self.upsert_in_list(
            target_config["contexts"],
            {"name": context_name, "context": {"cluster": self.cluster_nickname, "user": self.username}},
        )
        target_config["current-context"] = context_name

        if self.output_kubeconfig == self.input_kubeconfig:
            # yaml.dump rewrites the whole document: comments and ordering of the
            # admin kubeconfig are gone afterwards, and current-context moves.
            logger.warning(f"rewriting the admin kubeconfig in place: {self.output_kubeconfig}")

        with open(self.output_kubeconfig, "w") as f:
            yaml.dump(target_config, f, default_flow_style=False)
        # The file holds a private key -- do not leave it at the umask default.
        self.output_kubeconfig.chmod(KUBECONFIG_MODE)

        return context_name

    # -- orchestration -----------------------------------------------------

    def run(self) -> bool:
        """Issue the certificate, apply RBAC and update the kubeconfig.

        Returns:
            ``True`` on success, ``False`` when the admin kubeconfig could not
            be loaded, the CSR was not signed in time, or the kubeconfig could
            not be written.
        """
        try:
            config.load_kube_config(config_file=str(self.input_kubeconfig))
        except Exception as exc:
            logger.error(f"could not load the admin kubeconfig {self.input_kubeconfig}: {exc}")
            return False

        groups_txt = ", ".join(self.groups) or "(none)"
        logger.info(f"creating certificate for user '{self.username}', groups: {groups_txt}")
        self._warn_about_privileges()

        private_key = self.create_private_key()
        csr = self.create_csr(private_key, self.username)
        csr_pem = csr.public_bytes(serialization.Encoding.PEM).decode("utf-8")
        key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")

        signed_cert = self._sign_csr(csr_pem)
        if not signed_cert:
            logger.error(f"timeout while signing after {SIGN_WAIT_ATTEMPTS * SIGN_WAIT_SECONDS:.0f} s")
            return False

        # What the API server actually granted, which can be a lot less than
        # what was asked for (see DEFAULT_VALIDITY_SECONDS).
        cert_obj = x509.load_pem_x509_certificate(signed_cert.encode("utf-8"))
        logger.info(f"certificate received, valid until (UTC): {cert_obj.not_valid_after_utc}")

        if self.create_rbac:
            self._apply_rbac()
        else:
            logger.info("RBAC skipped (--no-rbac)")

        logger.info(f"updating kubeconfig: {self.output_kubeconfig}")
        try:
            context_name = self._write_kubeconfig(signed_cert, key_pem)
        except (ValueError, OSError) as exc:
            logger.error(f"could not write the kubeconfig: {exc}")
            return False

        logger.info(f"done — context '{context_name}' stored in '{self.output_kubeconfig}'")
        if not self.external_server_url:
            logger.warning(
                "no external URL configured — check whether 'server' in the kubeconfig is reachable from outside"
            )
        return True

    def _warn_about_privileges(self) -> None:
        """Point out where the requested privileges do not match what is issued.

        Two ways to be surprised, both silent otherwise: ``system:masters``
        overrides every restriction that was asked for, and a certificate with
        neither a group nor a binding can authenticate but do nothing at all.
        """
        if SYSTEM_MASTERS_GROUP in self.groups:
            logger.warning(
                f"group '{SYSTEM_MASTERS_GROUP}' grants full cluster-admin BEFORE RBAC is consulted — and it "
                f"cannot be revoked through RBAC either; drop it unless that is exactly what you want"
            )
            if self.cluster_role != "cluster-admin" or self.rbac_namespace or not self.create_rbac:
                logger.warning(
                    f"the requested restriction (role={self.cluster_role}, namespace={self.rbac_namespace}, "
                    f"rbac={self.create_rbac}) has NO effect while '{SYSTEM_MASTERS_GROUP}' is in the certificate"
                )
        elif not self.groups and not self.create_rbac:
            logger.warning(
                "certificate carries no group and --no-rbac was given — the user will authenticate but have no "
                "permissions at all until a binding exists"
            )


def main(
    username: str = typer.Argument(..., help="name of the new Kubernetes user (becomes the certificate's CN)"),
    group: list[str] = typer.Option(
        [],
        "--group",
        "-g",
        envvar="K8S_USER_CERT_GROUPS",
        help="group membership (repeatable, becomes an O in the subject); default: none, so the RBAC binding "
        "decides everything. NOTE: 'system:masters' would bypass RBAC entirely",
    ),
    input_kubeconfig: Path = typer.Option(
        Path.home() / ".kube" / "config",
        "--input",
        "-i",
        envvar="K8S_USER_CERT_INPUT",
        help="admin kubeconfig used to talk to the cluster",
    ),
    output_kubeconfig: Path | None = typer.Option(
        None, "--output", "-o", envvar="K8S_USER_CERT_OUTPUT", help="target kubeconfig (default: same as --input)"
    ),
    validity_seconds: int = typer.Option(
        DEFAULT_VALIDITY_SECONDS,
        "--validity",
        envvar="K8S_USER_CERT_VALIDITY",
        help="requested validity in seconds (the signer may grant less)",
    ),
    external_server_url: str | None = typer.Option(
        None,
        "--external-url",
        envvar="K8S_USER_CERT_EXTERNAL_URL",
        help="server URL for the generated kubeconfig (default: taken from --input)",
    ),
    cluster_nickname: str | None = typer.Option(
        None,
        "--cluster-nickname",
        envvar="K8S_USER_CERT_CLUSTER_NICKNAME",
        help="cluster name in the generated kubeconfig (default: taken from --input)",
    ),
    cluster_role: str = typer.Option(
        "cluster-admin", "--role", envvar="K8S_USER_CERT_ROLE", help="ClusterRole the binding refers to"
    ),
    no_rbac: bool = typer.Option(
        False, "--no-rbac", envvar="K8S_USER_CERT_NO_RBAC", help="do not create any RBAC binding"
    ),
    rbac_namespace: str | None = typer.Option(
        None,
        "--namespace",
        "-n",
        envvar="K8S_USER_CERT_NAMESPACE",
        help="namespace for a RoleBinding (without: ClusterRoleBinding)",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", envvar="K8S_USER_CERT_VERBOSE", help="DEBUG logging"),
) -> None:
    """Create a Kubernetes user certificate and update the kubeconfig.

    \f
    Everything below the ``\\f`` is hidden from ``--help`` (click convention).

    Raises:
        typer.Exit: Always — with the module docstring's exit codes.
    """
    configure_logging(verbose=verbose)
    print_banner("k8s_user_cert")

    creator = K8sUserCertCreator(
        username=username,
        groups=list(group),
        input_kubeconfig=input_kubeconfig,
        output_kubeconfig=output_kubeconfig,
        validity_seconds=validity_seconds,
        external_server_url=external_server_url,
        cluster_nickname=cluster_nickname,
        cluster_role=cluster_role,
        create_rbac=not no_rbac,
        rbac_namespace=rbac_namespace,
    )

    raise typer.Exit(code=0 if creator.run() else 1)


if __name__ == "__main__":
    typer.run(main)
