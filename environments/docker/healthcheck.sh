#!/bin/bash
# Health check script for testing environment container
# Verifies that VNC service is listening

set -e

# Check if VNC server is listening on port 5900
if ! timeout 1 bash -c "echo > /dev/tcp/localhost/5900" 2>/dev/null; then
    echo "ERROR: VNC server (port 5900) is not responding"
    exit 1
fi

echo "Health check passed: All services are running"
exit 0
