ARG BASE_IMAGE=debian:12-slim
FROM ${BASE_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive
ENV SHELL=/bin/bash
ENV PATH=/root/.local/bin:/root/.cargo/bin:/usr/local/bin:/usr/bin:/bin

RUN set -eux; \
    if command -v apt-get >/dev/null 2>&1; then \
      apt-get update; \
      apt-get install -y --no-install-recommends bash ca-certificates curl gzip iproute2 nano procps sudo tar; \
      rm -rf /var/lib/apt/lists/*; \
    elif command -v apk >/dev/null 2>&1; then \
      apk add --no-cache bash ca-certificates curl gzip iproute2 nano procps sudo tar; \
    else \
      echo "Unsupported base distribution" >&2; exit 1; \
    fi

# Install the official sshx client inside each customer container.
RUN set -eux; \
    curl -fsSL https://sshx.io/get -o /tmp/install-sshx; \
    sh /tmp/install-sshx; \
    SSHX_BIN="$(command -v sshx || find /root/.local/bin /root/.cargo/bin /usr/local/bin /usr/bin -type f -name sshx -print -quit 2>/dev/null || true)"; \
    test -n "$SSHX_BIN"; \
    install -m 0755 "$SSHX_BIN" /usr/local/bin/sshx; \
    rm -f /tmp/install-sshx

COPY entrypoint.sh /entrypoint.sh
RUN chmod 0755 /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["bash", "-l"]
