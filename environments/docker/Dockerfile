FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

ENV VNC_PASSWORD=testpass123

RUN apt-get update && apt-get install -y --no-install-recommends \
    dbus \
    dbus-x11 \
    xfce4 \
    xfce4-terminal \
    tigervnc-standalone-server \
    tigervnc-common \
    tigervnc-tools \
    supervisor \
    python3 \
    python3-pip \
    python3-venv \
    python3-tk \
    python3-dev \
    curl \
    wget \
    ca-certificates \
    xdotool \
    wmctrl \
    scrot \
    && rm -rf /var/lib/apt/lists/*

COPY controller_client/requirements.txt /tmp/controller-requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/controller-requirements.txt

RUN playwright install --with-deps chromium

COPY controller_client/ /opt/controller_client/

ENV CONTROLLER_HOST=host.docker.internal
ENV CONTROLLER_PORT=8000
ENV CONTROLLER_API_KEY=""

RUN mkdir -p /root/.vnc && \
    echo "${VNC_PASSWORD}" | vncpasswd -f > /root/.vnc/passwd && \
    chmod 600 /root/.vnc/passwd && \
    touch /root/.Xauthority

RUN mkdir -p /var/run/dbus && dbus-uuidgen > /var/lib/dbus/machine-id

COPY environments/docker/supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY environments/docker/healthcheck.sh /usr/local/bin/healthcheck.sh
COPY environments/docker/xfce4-startup.sh /usr/local/bin/xfce4-startup.sh
RUN chmod +x /usr/local/bin/healthcheck.sh /usr/local/bin/xfce4-startup.sh

EXPOSE 5900

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh"]

ENTRYPOINT ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
