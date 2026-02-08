#!/usr/bin/env bash
#
# Build and install PJSIP with Python bindings from source.
#
# Prerequisites (Debian/Ubuntu):
#   sudo apt install build-essential python3-dev swig \
#       libasound2-dev libssl-dev libopus-dev wget
#
# Usage:
#   ./install_pjsip.sh              # default version 2.16
#   PJSIP_VERSION=2.14.1 ./install_pjsip.sh

set -euo pipefail

PJSIP_VERSION="${PJSIP_VERSION:-2.16}"
BUILD_DIR="/tmp/pjsip-build"
TARBALL_URL="https://github.com/pjsip/pjproject/archive/refs/tags/${PJSIP_VERSION}.tar.gz"

echo "=== Installing PJSIP ${PJSIP_VERSION} with Python bindings ==="

# Check prerequisites
for cmd in python3 swig make gcc; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' not found. Install build dependencies first." >&2
        exit 1
    fi
done

# Python 3.12+ removed distutils; PJSIP's setup.py needs setuptools
python3 -c "import setuptools" 2>/dev/null || {
    echo "=== Installing setuptools (required for Python bindings build) ==="
    pip install --no-cache-dir setuptools
}

PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Python version: ${PYTHON_VERSION}"

# Clean previous build
rm -rf "${BUILD_DIR}"
mkdir -p "${BUILD_DIR}"
cd "${BUILD_DIR}"

echo "=== Downloading pjproject-${PJSIP_VERSION} ==="
wget -q "${TARBALL_URL}" -O pjproject.tar.gz
tar xzf pjproject.tar.gz
cd "pjproject-${PJSIP_VERSION}"

echo "=== Configuring PJSIP ==="
./configure \
    --enable-shared \
    --disable-video \
    --disable-v4l2 \
    --disable-libyuv \
    --disable-libwebrtc \
    --with-external-opus \
    CFLAGS="-O2 -fPIC" \
    CXXFLAGS="-O2 -fPIC"

echo "=== Building PJSIP (this may take a few minutes) ==="
make -j"$(nproc)" dep
make -j"$(nproc)"
sudo make install

echo "=== Building Python bindings ==="
cd pjsip-apps/src/swig/python
make
sudo make install

# Refresh shared library cache
sudo ldconfig

echo "=== Verifying installation ==="
python3 -c "import pjsua2; print(f'pjsua2 imported successfully')" || {
    echo "ERROR: pjsua2 import failed. Check the build output above." >&2
    exit 1
}

echo "=== PJSIP ${PJSIP_VERSION} installed successfully ==="

# Cleanup
rm -rf "${BUILD_DIR}"
