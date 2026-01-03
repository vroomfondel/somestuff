[![https://github.com/vroomfondel/somestuff/raw/main/tangstuff/Gemini_Generated_Image_tangstuff_y9wdi7y9wdi7y9wd_250x250.png](https://github.com/vroomfondel/somestuff/raw/main/tangstuff/Gemini_Generated_Image_tangstuff_y9wdi7y9wdi7y9wd_250x250.png)](https://github.com/vroomfondel/somestuff/raw/main/tangstuff)

# Tang, LUKS, and Clevis: Automated Network-Bound Disk Encryption (NBDE)

This directory contains tools and configurations for setting up a **Tang** server and using **Clevis** to automatically decrypt **LUKS**-encrypted partitions upon booting.

## Overview

- **LUKS**: Standard for Linux disk encryption.
- **Tang**: Stateless server for network-bound key exchange.
- **Clevis**: Framework for automated decryption using pins (Tang, TPM2, SSS).
- **s6-overlay**: Manages processes (Tang, HAProxy, Nginx) within the container.

---

## Tang Server

### Docker Usage

**Build:** `./build.sh`

**Run (with HAProxy):**
```bash
docker run -it --cap-add=NET_ADMIN -p 9090:80 \
  -e TANGDATADIR=/var/lib/tang \
  -e USEHAPROXY=1 \
  -e HAPROXYPORT=80 \
  -e HAPROXYACCEPTPROXY=0 \
  -v $(pwd)/TANGDATA:/var/lib/tang \
  --rm xomoxcc/tang:latest
```

### Configuration (Environment Variables)

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TANGPORT` | `9090` | Internal port for `tangd`. |
| `USEHAPROXY` | `1` | Enable HAProxy (1=on, 0=off). |
| `USENGINX` | `0` | Enable Nginx (1=on, 0=off). |
| `HAPROXYACCEPTPROXY` | `1` | Enable PROXY protocol for HAProxy. |

---

## Deployment (Kubernetes / k3s)

The [`k3s_tang_deployment.yml`](k3s_tang_deployment.yml) provides a full stack:
- **Namespace**: `tang`
- **Service**: Exposes Tang on port `9090`.
- **IngressRoute**: Advanced routing via Traefik (Host & Query rules).
- **Middleware**: IP allow-listing for security.
- **Persistence**: HostPath at `/mnt/DATA/tang` (Critical!).

---

## Client Setup (Ubuntu 25.10)

1. **Install Packages**: `sudo apt install cryptsetup clevis clevis-luks clevis-initramfs clevis-systemd`
2. **Bind Partition**:
   ```bash
   sudo clevis luks bind -d /dev/mapper/vg0-lv_data sss \
     '{"t":1, "pins":{"tang":[{"url":"http://tang.example.com"}]}}'
   ```
3. **Update Initramfs**: `sudo update-initramfs -u`

---

## Maintenance

Verify connectivity using [`tang_check_connection.sh`](tang_check_connection.sh):
```bash
sudo ./tang_check_connection.sh /dev/mapper/vg0-lv_data
```

⚠️ **Security Warning**: Backup your keys! This project is provided "as is". Use at your own risk.