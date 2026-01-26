[![https://github.com/vroomfondel/somestuff/raw/main/mosquitto-2.1/Gemini_Generated_Image_mosquitto_7f2v9m7f2v9m7f2v_250x250.png](https://github.com/vroomfondel/somestuff/raw/main/mosquitto-2.1/Gemini_Generated_Image_mosquitto_7f2v9m7f2v9m7f2v_250x250.png)](https://github.com/vroomfondel/somestuff/raw/main/mosquitto-2.1/)

# Mosquitto 2.1 MQTT Broker

This directory contains a custom, production-ready build of **Mosquitto 2.1.0-test1**. It is specifically tailored for deployment on Ubuntu-based environments (Ubuntu 24.04) and includes support for modern MQTT features, enhanced security, and containerized orchestration via Kubernetes.

---

## 🚀 Features & Usefulness

- **Mosquitto 2.1.0-test1**: Built from source to leverage the latest experimental features, including the new HTTP API.
- **Dynamic Security**: Integrated `mosquitto_dynamic_security.so` plugin allows for real-time management of users, groups, and ACLs without broker restarts.
- **SQLite Persistence**: Utilizes the `mosquitto_persist_sqlite.so` plugin for robust, high-performance persistent storage, superior to standard file-based persistence in containerized environments.
- **WebSockets Support**: Native support enabled on port `9001`, allowing web-based clients to communicate via MQTT.
- **HTTP API & Dashboard**: Includes a built-in HTTP management API and a web dashboard on port `9883`.
- **Multi-Arch Docker Image**: Optimized for both `amd64` and `arm64` architectures, making it suitable for both cloud servers and edge devices (like Raspberry Pi).
- **Proxy Protocol v2 Support**: Configured to work behind Load Balancers (like Traefik or HAProxy) while preserving client IP addresses.
- **Enhanced IP Logging**: Includes a custom patch that adds client IP addresses to all disconnect log messages. Now it is finally possible to properly use fail2ban based on IP addresses for MQTT authentication failures.

---

## 📁 Directory Structure

| File | Description |
| :--- | :--- |
| `Dockerfile` | Multi-stage build file compiling Mosquitto from source on Ubuntu 24.04. It handles complex dependency linking for plugins and `mosquitto_ctrl`. |
| `build.sh` | A utility script for building multi-architecture Docker images and pushing them to a registry. |
| `mosquitto_test.conf` | The primary configuration file. Includes listeners for MQTT (1883), WebSockets (9001), and the HTTP API (9883). |
| `mosquitto_test.acl` | Initial Access Control List template. |
| `mosquitto_test.passwd` | Default password file (Default: `admin:public`). |
| `mosquitto_dynamic-security_test.json` | Initial state configuration for the Dynamic Security plugin. |
| `k3s_mosquitto_deployment.yml` | Kubernetes manifest for full-stack deployment including Namespace, Deployment, PVCs, Services, and Traefik IngressRoutes. |
| `haproxy_test.cfg` | Example HAProxy configuration for testing Proxy Protocol v2 locally. |

---

## 🛠️ Build and Run

### Local Image Build
You can build the image locally using the provided `build.sh` script:
```bash
./build.sh onlylocal
```

### Local Execution (Docker) with HAProxy (Proxy Protocol v2)
The default configuration `mosquitto_test.conf` has `enable_proxy_protocol 2` enabled. To test this locally, you must run the broker behind a proxy that sends Proxy Protocol v2 headers (like HAProxy).

**1. Start Mosquitto** (mapping to "twisted" ports as expected by the test HAProxy config):
```bash
docker run -d \
  --name mosquitto-test \
  -p 1884:1883 -p 9002:9001 -p 9884:9883 \
  -v $(pwd)/mosquitto_test.conf:/mosquitto/config/mosquitto.conf \
  -v $(pwd)/mosquitto_test.passwd:/mosquitto/config/mosquitto.passwd \
  -v $(pwd)/mosquitto_dynamic-security_test.json:/mosquitto/data/dynamic-security.json \
  -v $(pwd)/mosquitto_test.acl:/mosquitto/config/mosquitto.acl \
  --rm xomoxcc/mosquitto:2.1
```

**2. Start HAProxy**:
```bash
docker run -d \
  --name haproxy-test \
  --net=host \
  -v $(pwd)/haproxy_test.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro \
  --rm haproxy:latest
```
*Note: Using `--net=host` allows HAProxy to easily reach Mosquitto on `127.0.0.1` as configured in `haproxy_test.cfg`.*

**3. Test Connection**:
You can now connect to Mosquitto via HAProxy on the standard ports (1883, 9001, 9883).

---

## ☸️ Kubernetes Deployment

The included `k3s_mosquitto_deployment.yml` is a comprehensive manifest designed for K3s/Kubernetes.

### Key Components:
- **Namespace**: `mosquitto` - Keeps the deployment isolated.
- **Deployment**: A single-replica deployment with defined resource limits and **Liveness/Readiness Probes** (TCP check on port 1883).
- **Persistence**: Switched to **PersistentVolumeClaims (PVC)** (`mosquitto-data-pvc`, `mosquitto-config-pvc`, `mosquitto-log-pvc`) to ensure data survives pod restarts and migrations.
- **Service**: A LoadBalancer service exposing all necessary ports.
- **IngressRoutes (Traefik)**: Provides external access to the HTTP API and WebSockets with **Proxy Protocol v2** support enabled.

### Deploying:
```bash
kubectl apply -f k3s_mosquitto_deployment.yml
```

---

## 🔧 Implementation Details (Breadcrumbs)

- **Library Linking**: The build process specifically addresses shared library issues common with Mosquitto plugins. `libmosquitto_common.so.1` and `libedit.so.0` are correctly handled to ensure `mosquitto_ctrl` and plugins function out-of-the-box.
- **Bootstrap Logic**: `runmosquitto.sh` handles the initial setup of the environment, ensuring that if no configuration or user database exists, a default `admin` user is created to allow immediate access for testing.
- **Proxy Protocol**: Enabled for all listeners to support modern ingress controllers. Ensure your Load Balancer is configured to send Proxy Protocol v2 headers.

---
*Reference: [Mosquitto HAProxy Documentation](https://github.com/ralight/mosquitto/blob/develop/www/pages/documentation/haproxy.md#direct-pass-through-with-proxy-protocol-v2)*


## ⚠️ Note

This is a development/experimental project. For production use, review security settings, customize configurations, and test thoroughly in your environment. Provided "as is" without warranty of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose and noninfringement. In no event shall the authors or copyright holders be liable for any claim, damages or other liability, whether in an action of contract, tort or otherwise, arising from, out of or in connection with the software or the use or other dealings in the software. Use at your own risk.