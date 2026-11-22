FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive
ENV SHELL=/bin/bash
ENV PATH=/root/.local/bin:/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl nano sudo iproute2 procps && rm -rf /var/lib/apt/lists/*
# Install the official sshx client in the customer's isolated container.
RUN curl -fsSL https://sshx.io/get -o /tmp/install-sshx \
    && sh /tmp/install-sshx \
    && SSHX_BIN="$(command -v sshx || find /root/.local/bin /root/.cargo/bin /usr/local/bin /usr/bin -name sshx -print -quit 2>/dev/null)" \
    && test -n "$SSHX_BIN" \
    && install -m 0755 "$SSHX_BIN" /usr/local/bin/sshx \
    && rm -f /tmp/install-sshx
COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
