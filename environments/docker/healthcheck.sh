#!/bin/bash
# Health check script for testing environment container
# Verifies that SSH, VNC, and CDP services are listening

set -e

# Check if SSH server is listening on port 22
if ! timeout 1 bash -c "echo > /dev/tcp/localhost/22" 2>/dev/null; then
    echo "ERROR: SSH server (port 22) is not responding"
    exit 1
fi

# Check if VNC server is listening on port 5900
if ! timeout 1 bash -c "echo > /dev/tcp/localhost/5900" 2>/dev/null; then
    echo "ERROR: VNC server (port 5900) is not responding"
    exit 1
fi

# Check if Chromium CDP is listening on port 9222
if ! timeout 1 bash -c "echo > /dev/tcp/localhost/9223" 2>/dev/null; then
    echo "ERROR: Chromium CDP (port 9223) is not responding"
    exit 1
fi

echo "Health check passed: All services are running"
exit 0
