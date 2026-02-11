FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

ARG SSH_PASSWORD=testpass123
ARG VNC_PASSWORD=testpass123
ENV SSH_PASSWORD=${SSH_PASSWORD}
ENV VNC_PASSWORD=${VNC_PASSWORD}

RUN apt-get update && apt-get install -y --no-install-recommends \
    dbus \
    dbus-x11 \
    xfce4 \
    xfce4-terminal \
    tigervnc-standalone-server \
    tigervnc-common \
    tigervnc-tools \
    openssh-server \
    supervisor \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    wget \
    socat \
    ca-certificates \
    xdotool \
    wmctrl \
    scrot \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages playwright~=1.58.0 && \
    playwright install --with-deps chromium && \
    ln -s "$(find /root/.cache/ms-playwright -name chrome -path '*/chrome-linux*/chrome' | head -1)" \
        /usr/local/bin/chromium-browser

RUN mkdir -p /var/run/sshd && \
    sed -i 's/#PermitRootLogin prohibit-password/PermitRootLogin yes/' /etc/ssh/sshd_config && \
    sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config && \
    sed -i 's/UsePAM yes/UsePAM no/' /etc/ssh/sshd_config

RUN echo "root:${SSH_PASSWORD}" | chpasswd

RUN mkdir -p /root/.vnc && \
    echo "${VNC_PASSWORD}" | vncpasswd -f > /root/.vnc/passwd && \
    chmod 600 /root/.vnc/passwd

RUN mkdir -p /var/run/dbus && dbus-uuidgen > /var/lib/dbus/machine-id

COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf
COPY healthcheck.sh /usr/local/bin/healthcheck.sh
COPY xfce4-startup.sh /usr/local/bin/xfce4-startup.sh
RUN chmod +x /usr/local/bin/healthcheck.sh /usr/local/bin/xfce4-startup.sh

EXPOSE 22 5900 9223

HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD ["/usr/local/bin/healthcheck.sh"]

ENTRYPOINT ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
