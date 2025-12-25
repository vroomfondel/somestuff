[![Docker Pulls](https://img.shields.io/docker/pulls/xomoxcc/tang?logo=docker)](https://hub.docker.com/r/xomoxcc/tang/tags)

[![Gemini_Generated_Image_tangstuff_y9wdi7y9wdi7y9wd_250x250.png](Gemini_Generated_Image_tangstuff_y9wdi7y9wdi7y9wd_250x250.png)](https://hub.docker.com/r/xomoxcc/tang/tags)

# Tang, LUKS, and Clevis: Automated Network-Bound Disk Encryption (NBDE)

This directory contains tools and configurations for setting up a **Tang** server and using **Clevis** to automatically decrypt **LUKS**-encrypted partitions upon booting.

## Table of Contents
1. [Overview](#overview)
2. [Tang Server](#tang-server)
    - [Docker Usage](#docker-usage)
    - [Kubernetes / k3s Deployment](#kubernetes--k3s-deployment)
3. [LUKS & Clevis Setup (Ubuntu 25.10)](#luks--clevis-setup-ubuntu-2510)
    - [Required Packages](#required-packages)
    - [Encrypting an Existing Partition (LVM)](#encrypting-an-existing-partition-lvm)
    - [Automated Decryption with Clevis](#automated-decryption-with-clevis)
4. [Maintenance Tools](#maintenance-tools)
    - [Checking Connectivity](#checking-connectivity)

---

## Overview

- **LUKS (Linux Unified Key Setup)**: The standard for Linux hard disk encryption.
- **Tang**: A server for binding data to network presence. It makes nodes available to a client only when they are on a particular network.
- **Clevis**: A pluggable framework for automated decryption. It uses "pins" to interact with various systems like Tang, TPM2, or Shamir Secret Sharing (SSS).

This setup allows for "Network-Bound Disk Encryption" (NBDE). When the system boots, Clevis contacts the Tang server(s) to fetch the decryption key. If the servers are unreachable (e.g., the hardware was stolen and moved to another network), the disk remains locked.

---

## Tang Server

Tang is lightweight and stateless. It does not store keys; it derives them based on a secret exchange.

### Docker Usage

You can run Tang easily using Docker.

**Build:**
```bash
./build.sh
```

**Run:**
```bash
docker run -it -p 9090:9090 \
  -e DATADIR=/var/lib/tang \
  -e PORT=9090 \
  -v $(pwd)/TANGDATA:/var/lib/tang \
  --rm xomoxcc/tang:latest 
```

### Kubernetes / k3s Deployment

The file [`k3s_tang_deployment.yml`](k3s_tang_deployment.yml) provides a full stack for deploying Tang in a Kubernetes environment:
- **Namespace**: `tang`
- **Deployment**: Runs the `xomoxcc/tang:latest` image.
- **Service**: Exposes Tang on port `9090`.
- **Persistence**: Uses a `hostPath` at `/mnt/DATA/tang` to store the Tang keys (critical for consistency).
- **Security**: Includes a `Middleware` (Traefik) for IP allow-listing and an `IngressRoute` for external access via Host and Query rules.

---

## LUKS & Clevis Setup (Ubuntu 25.10)

### Required Packages

To manage LUKS partitions and Clevis bindings:

```bash
sudo apt update
sudo apt install cryptsetup clevis clevis-luks clevis-initramfs
```

### Encrypting an Existing Partition (LVM)

*Assumption: You have data on an LVM partition (e.g., `/dev/mapper/vg0-lv_data`) and want to migrate it to a LUKS-encrypted volume with the same name.*

1.  **Rename the original Logical Volume**:
    Rename the existing LV to a "backup" name. This avoids double-copying.
    ```bash
    sudo umount /path/to/data
    sudo lvrename vg0 lv_data lv_data_old
    ```
2.  **Create a new Logical Volume**:
    Create a new LV with the original name (make sure to make it large enough to store your data - usually 10G might be too small).
    ```bash
    sudo lvcreate -L 10G -n lv_data vg0
    ```
3.  **Format the new partition with LUKS**:
    ```bash
    sudo cryptsetup luksFormat /dev/mapper/vg0-lv_data
    ```
4.  **Open the LUKS device**:
    ```bash
    sudo cryptsetup open /dev/mapper/vg0-lv_data data_crypt
    ```
5.  **Create a filesystem on the encrypted volume**:
    ```bash
    sudo mkfs.ext4 /dev/mapper/data_crypt
    ```
6.  **Mount both volumes**:
    ```bash
    sudo mount /dev/mapper/data_crypt /path/to/data
    sudo mount /dev/mapper/vg0-lv_data_old /mnt
    ```
7.  **Migrate data**:
    Copy data directly from the old volume to the new encrypted one.
    ```bash
    sudo rsync -avrlp /mnt/ /path/to/data/
    ```
8.  **Update `/etc/crypttab`**:
    ```text
    data_crypt  /dev/mapper/vg0-lv_data  none  luks
    ```
9.  **Cleanup**:
    Unmount and remove the old logical volume.
    ```bash
    sudo umount /mnt
    sudo lvremove /dev/mapper/vg0-lv_data_old
    ```
    
### changes to /etc/fstab

To ensure the encrypted volume is mounted automatically and that the system waits for the network if necessary (for Tang), add a line to `/etc/fstab`:

```text
/dev/mapper/data_crypt  /path/to/data  ext4  defaults,_netdev  0  2
```

*Note: `_netdev` is important if the volume depends on network-bound decryption.*

### changes to /etc/crypttab

Add the mapping to `/etc/crypttab`. It is recommended to use the `UUID` of the underlying partition instead of the device path:

```text
# <name>       <device>                                 <password>  <options>
data_crypt     UUID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx  none        luks,_netdev
```

You can find the UUID using `sudo blkid /dev/mapper/vg0-lv_data`.

### Automated Decryption with Clevis

To bind a LUKS partition to one or more Tang servers using Shamir Secret Sharing (SSS) for redundancy (e.g., requiring 2 out of 2 servers):

```bash
sudo clevis luks bind -d /dev/mapper/data_crypt sss \
  '{"t":2, "pins":{"tang":[{"url":"http://tang1.local"},{"url":"http://tang2.remote.tld:9090"}]}}'
```

- `-d`: The encrypted device.
- `sss`: Shamir Secret Sharing pin.
- `t`: Threshold (number of pins required to decrypt).

After binding, update your initramfs:
```bash
sudo update-initramfs -u
```

---

## Maintenance Tools

### Checking Connectivity

The script [`tang_check_connection.sh`](tang_check_connection.sh) is provided to verify that your Clevis bindings are still valid and that the Tang servers are reachable.

**Usage:**
```bash
sudo ./tang_check_connection.sh /dev/mapper/vg0-lv_data
```
It iterates through all Clevis slots on the device and attempts a trial decryption (`clevis luks pass`) without actually unlocking the device.


## ⚠️ Note

This is a development/experimental project. For production use, review security settings, customize configurations, and test thoroughly in your environment. Provided "as is" without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose and noninfringement. In no event shall the authors or copyright holders be liable for any claim, damages or other liability, whether in an action of contract, tort or otherwise, arising from, out of or in connection with the software or the use or other dealings in the software. Use at your own risk.