[![https://github.com/vroomfondel/somestuff/raw/main/tangstuff/Gemini_Generated_Image_tangstuff_y9wdi7y9wdi7y9wd_250x250.png](https://github.com/vroomfondel/somestuff/raw/main/tangstuff/Gemini_Generated_Image_tangstuff_y9wdi7y9wdi7y9wd_250x250.png)](https://github.com/vroomfondel/somestuff/raw/main/tangstuff)

# Tang, LUKS, and Clevis: Automated Network-Bound Disk Encryption (NBDE)

This directory provides a comprehensive solution for setting up a **Tang** server and utilizing **Clevis** for automated decryption of **LUKS**-encrypted partitions upon system boot. It is specifically designed for high availability and flexibility using modern container orchestration patterns and robust process management.

## Table of Contents
1. [Architecture Overview](#architecture-overview)
2. [Tang Server Container](#tang-server-container)
    - [Process Management (s6-overlay)](#process-management-s6-overlay)
    - [Integrated Frontends (HAProxy & Nginx)](#integrated-frontends-haproxy--nginx)
    - [Proxy Modes: PROXY Protocol vs. HTTP Forwarded](#proxy-modes-proxy-protocol-vs-http-forwarded)
    - [Trusted Proxies Configuration](#trusted-proxies-configuration)
    - [Configuration (Environment Variables)](#configuration-environment-variables)
    - [Docker Usage](#docker-usage)
3. [Deployment (Kubernetes / k3s)](#deployment-kubernetes--k3s)
    - [Service & Routing](#service--routing)
    - [Security Middlewares](#security-middlewares)
4. [Client Setup (LUKS & Clevis)](#client-setup-luks--clevis)
    - [Prerequisites](#prerequisites)
    - [Encryption Workflow](#encryption-workflow)
    - [Automated Decryption](#automated-decryption)
5. [Maintenance & Troubleshooting](#maintenance--troubleshooting)
    - [Connectivity Checks](#connectivity-checks)
    - [Security Notes](#security-notes)

---

## Architecture Overview

Network-Bound Disk Encryption (NBDE) allows for secure, automated unlocking of encrypted drives when the system is connected to a trusted network.

- **LUKS (Linux Unified Key Setup)**: The standard disk encryption layer for Linux.
- **Tang**: A stateless, lightweight server that facilitates network-bound key exchange. It doesn't store keys; it derives them using cryptographic exchange (ECMR).
- **Clevis**: A decryption framework that resides on the client. It uses "pins" (like Tang or TPM2) to automatically unlock LUKS volumes.
- **s6-overlay**: A process supervisor used within the container to manage multiple services reliably and handle container lifecycle signals properly.

---

## Tang Server Container

The provided [Dockerfile](Dockerfile) builds a robust Tang server image based on Debian Trixie.

### Process Management (s6-overlay)

We use [s6-overlay](https://github.com/just-containers/s6-overlay) (v3) to manage the container's processes. Unlike traditional "one process per container" approaches, s6-overlay allows us to run `tangd` alongside a frontend (HAProxy or Nginx) while ensuring:
- **Clean initialization**: Services start in the correct order.
- **Reliable supervision**: If `tangd`, HAProxy, or Nginx crashes, s6-overlay restarts them immediately.
- **Zombie reaping**: Properly handles defunct processes.
- **Signal handling**: Gracefully shuts down all services when the container receives a termination signal.

The service definitions are located in `s6-initstuff/s6-overlay/s6-rc.d/`.

### Integrated Frontends (HAProxy & Nginx)

The container includes both **HAProxy** and **Nginx** as optional frontends. They serve several purposes:
1. **SSL Termination**: Although Tang is often used over HTTP in trusted networks, frontends can provide HTTPS.
2. **Access Control**: Filter requests based on IP or headers.
3. **Logging**: Enhanced request logging for auditing.
4. **Real IP Transparency**: Passing the actual client IP to the backend.

### Proxy Modes: PROXY Protocol vs. HTTP Forwarded

Understanding how the frontend receives and passes client information is crucial:

#### 1. PROXY Protocol Mode (`HAPROXYACCEPTPROXY=1` or `NGINXACCEPTPROXY=1`)
*   **Usage**: Use this when your container is behind another load balancer (like Traefik in k3s) that is configured to send the [PROXY protocol](https://www.haproxy.org/download/1.8/doc/proxy-protocol.txt) header.
*   **How it works**: The external load balancer prepends a small header to the TCP connection containing the original source and destination IP/ports. The internal frontend (HAProxy/Nginx) parses this header to identify the real client.
*   **Reference**: See `k3s_tang_deployment.yml` where `IngressRouteTCP` uses `proxyProtocol: { version: 2 }`.

#### 2. HTTP Forwarded Mode (Non-Proxy Protocol)
*   **Usage**: Use this when the external proxy sends standard HTTP headers like `X-Forwarded-For` but does NOT use the binary PROXY protocol.
*   **How it works**: The frontend trusts the `X-Forwarded-For` header ONLY if it comes from a "trusted proxy" (see below).

### Trusted Proxies Configuration

The file `/etc/haproxy/trusted_proxies.lst` (populated via `Dockerfile` and used in `haproxy.cfg.pre`) defines which upstream IP addresses are allowed to provide `X-Forwarded-For` headers.

*   **Mechanism**: In `haproxy.cfg`, we use an ACL:
    ```haproxy
    acl from_proxy src -f /etc/haproxy/trusted_proxies.lst
    http-request set-src hdr(x-forwarded-for) if from_proxy
    ```
*   **Dynamic Behavior**: If `HAPROXYACCEPTPROXY=1` is enabled, the `run` script for HAProxy clears this list to prevent mixing PROXY protocol data with potentially spoofed HTTP headers.
*   **Customization**: You can modify this list in the `Dockerfile` or mount a custom version to allow specific upstream proxies in your network.

### Configuration (Environment Variables)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TANGPORT` | `9090` | Internal port where `tangd` listens. |
| `TANGDATADIR` | `/var/lib/tang` | Directory where Tang keys are stored. |
| `USEHAPROXY` | `1` | Enable/Disable HAProxy (1 to enable, 0 to disable). |
| `HAPROXYPORT` | `80` | Port HAProxy listens on. |
| `HAPROXYACCEPTPROXY`| `1` | Enable PROXY protocol support in HAProxy. |
| `USENGINX` | `0` | Enable/Disable Nginx (1 to enable, 0 to disable). |
| `NGINXPORT` | `80` | Port Nginx listens on. |
| `NGINXACCEPTPROXY` | `1` | Enable PROXY protocol support in Nginx. |

### Docker Usage

**Build:**
```bash
./build.sh
```

**Run (with HAProxy and PROXY protocol enabled):**
```bash
docker run -it --cap-add=NET_ADMIN -p 80:80 \
  -e TANGDATADIR=/var/lib/tang \
  -e USEHAPROXY=1 \
  -e HAPROXYPORT=80 \
  -e HAPROXYACCEPTPROXY=1 \
  -v $(pwd)/TANGDATA:/var/lib/tang \
  --rm xomoxcc/tang:latest
```
*Note: `--cap-add=NET_ADMIN` is required if you want the container to automatically set up `iptables` rules to protect the internal `TANGPORT`.*

---

## Deployment (Kubernetes / k3s)

The [`k3s_tang_deployment.yml`](k3s_tang_deployment.yml) file provides a production-ready manifest.

### Service & Routing

*   **IngressRouteTCP**: Configured for maximum transparency. It uses Traefik's TCP entrypoint and passes the PROXY protocol v2 to the container. This is the preferred way for Tang as it allows HAProxy to see the real client IP for all types of requests.
*   **IngressRoute (HTTP)**: A standard HTTP route that can be used for health checks or monitoring. It includes a "breadcrumb" query parameter `action=tang` which can be used for specific routing logic in more complex setups.

### Security Middlewares

To protect the Tang server:
- **IP Allow-List**: A Traefik `Middleware` restricts access to authorized subnets only.
- **iptables (Internal)**: The container's `runtang.sh` script attempts to block direct access to port `9090` from outside the container, forcing traffic through HAProxy/Nginx.
- **InitContainer**: Ensures the host-mounted volume `/var/lib/tang` has correct permissions for the `_tang` user.

---

## Client Setup (LUKS & Clevis)

Instructions optimized for Ubuntu 25.10.

### Prerequisites

Install the necessary client packages:
```bash
sudo apt update
sudo apt install cryptsetup clevis clevis-luks clevis-initramfs clevis-systemd
```

### Encryption Workflow

If you are migrating an existing LVM partition (e.g., `/dev/mapper/vg0-lv_data`):

1. **Backup & Rename**:
   ```bash
   sudo umount /path/to/data
   sudo lvrename vg0 lv_data lv_data_old
   ```
2. **Create & Format New Volume**:
   ```bash
   sudo lvcreate -L 10G -n lv_data vg0
   sudo cryptsetup luksFormat /dev/mapper/vg0-lv_data
   sudo cryptsetup open /dev/mapper/vg0-lv_data data_crypt
   sudo mkfs.ext4 /dev/mapper/data_crypt
   ```
3. **Data Migration**:
   ```bash
   sudo mount /dev/mapper/data_crypt /path/to/data
   sudo mount /dev/mapper/vg0-lv_data_old /mnt
   sudo rsync -avrlp /mnt/ /path/to/data/
   ```
4. **Configuration Update**:
   - **`/etc/crypttab`**:
     ```text
     data_crypt  UUID=<UUID_OF_VG0_LV_DATA>  none  luks,_netdev
     ```
   - **`/etc/fstab`**:
     ```text
     /dev/mapper/data_crypt  /path/to/data  ext4  defaults,_netdev  0  2
     ```
   *Note: Use `_netdev` to ensure the system waits for network availability.*

### Automated Decryption

Bind the partition to your Tang server(s). Example using Shamir Secret Sharing (SSS) for redundancy (requiring 1 out of 2 servers):

```bash
sudo clevis luks bind -d /dev/mapper/vg0-lv_data sss \
  '{"t":1, "pins":{"tang":[{"url":"http://tang1.local"},{"url":"http://tang2.remote.tld:9090"}]}}'
```

Update initramfs to apply changes:
```bash
sudo update-initramfs -u
```

---

## Maintenance & Troubleshooting

### Connectivity Checks

Use the provided script [`tang_check_connection.sh`](tang_check_connection.sh) to verify your setup:
```bash
sudo ./tang_check_connection.sh /dev/mapper/vg0-lv_data
```
This script tests all Clevis slots by attempting a trial decryption.

### Security Notes

- **Key Persistence**: If you lose the keys in `TANGDATADIR`, you will lose access to your encrypted data unless you have the manual passphrase. **Backup your keys!**
- **Network Trust**: Tang does not authenticate the client. Security relies on the client's ability to reach the server. Use network-level protections (VPNs, VLANs, Firewalls, or the included Traefik Middlewares) to limit who can talk to Tang.
- **Production Warning**: This project demonstrates a secure setup but should be reviewed by your security team. Use at your own risk.

## ⚠️ Note

This is a development/experimental project. For production use, review security settings, customize configurations, and test thoroughly in your environment. Provided "as is" without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose and noninfringement. In no event shall the authors or copyright holders be liable for any claim, damages or other liability, whether in an action of contract, tort or otherwise, arising from, out of or in connection with the software or the use or other dealings in the software. Use at your own risk.