# NeonHost — Telegram Hosting & Deploy Bot

NeonHost is a self-hosted Telegram bot for Docker-container VPS requests and GitHub web-app deployment.

IMPORTANT: The VPS product is an isolated Docker container, not a dedicated KVM virtual machine. It offers key-only SSH plus enforced RAM, CPU-quota and PID limits. It does not provide systemd, nested Docker, KVM, or per-customer disk quotas. Do not advertise it as a full VM.

## Features
- /start menu, /plans, /vps, /web, /my, /stop, /resume and /help.
- VPS requests accept an OpenSSH public key only. Password login is disabled.
- VPS tiers: 1 GB / 0.5 CPU quota, 2 GB / 1 CPU quota, 4 GB / 2 CPU quota.
- The bot checks detected host RAM, current free RAM, reserved RAM, detected CPU count and already-allocated service resources before approving a service.
- Docker containers are not privileged, do not receive the Docker socket, and have RAM, CPU and PID caps.
- Admin-reviewed GitHub deployments require a Dockerfile.
- Optional HTTPS subdomains through Caddy.
- SQLite order and service records.

## Important limitations
- Disk quotas are not enforced by this Docker-only version. A customer container can consume host disk. Do not offer public multi-tenant hosting until you add and verify storage quotas or isolated storage.
- Web image builds execute repository Dockerfile instructions and are not separately resource-capped. Only approve source you trust.
- The bot mounts the Docker socket. Access to that socket is effectively host-root access. Protect the Telegram token and keep ADMIN_IDS restricted.
- This project does not process payments or invent capacity. Use only the real resource limits detected and enforced by Docker.
- sshx is not used as an SSH fallback. It shares an existing shell in a collaborative browser session; it is not a persistent SSH daemon replacement. Troubleshoot the SSH port, firewall, address and public key instead.

## Requirements
- Linux VPS with Docker Engine and Docker Compose plugin.
- Telegram bot token from BotFather.
- Your Telegram numeric user ID.
- Public server IP or hostname.
- Public TCP port range 22000-22999 available for customer SSH.
- For HTTPS, DNS wildcard pointing to the host and inbound TCP 80/443.

## Install
1. Clone this repository to your Linux VPS:
   git clone https://github.com/ujwalffyt/BOT.git neonhost
   cd neonhost

2. Run setup once. It creates .env from .env.example:
   chmod +x deploy.sh
   ./deploy.sh

3. Edit .env and set TELEGRAM_BOT_TOKEN, ADMIN_IDS, PUBLIC_HOST, HOST_RESERVED_RAM_MB, HOST_RESERVED_CPU_CORES and MIN_FREE_RAM_MB. Send /start to the bot from the admin account once.

4. Optional HTTPS: set WEB_ROOT_DOMAIN to a domain you control, such as hosting.example.com, and create a wildcard DNS A record for *.hosting.example.com pointing to PUBLIC_HOST.

5. Start the stack:
   ./deploy.sh
   docker compose logs -f bot

## Firewall
Preserve your existing administrator SSH port before changing firewall rules. Allow TCP 80, TCP 443, UDP 443 and TCP 22000-22999. If WEB_ROOT_DOMAIN is blank, also allow TCP 8000-8999. Allow the same ports in the provider firewall if it has one. Do not expose the Docker API over public TCP.

## Customer flow
VPS: customer sends /vps, selects a plan, and submits their public SSH key. Admin reviews /orders and runs /approve ORDER_ID. The bot creates an isolated container and returns a command like ssh root@SERVER_IP -p 22000.

Web: customer sends /web, a public GitHub repository, internal app port and site slug. The repository must have a Dockerfile and the app must listen on 0.0.0.0. Admin reviews the source and runs /approve ORDER_ID. With WEB_ROOT_DOMAIN configured, Caddy routes a unique subdomain; otherwise the bot returns a direct HTTP port URL.

## Admin commands
/orders — pending requests
/approve ORDER_ID — approve and provision
/reject ORDER_ID — reject a request
/instances — list created services
/capacity — show live host capacity
/remove SERVICE_ID — remove the container and its writable data

## Operations
docker compose ps
docker compose logs -f bot
docker compose restart bot

Back up the data directory and customer data. Removing a service deletes its container writable layer.

## Security
This container-based design is not equivalent to a virtual machine. Containers share the host kernel. For untrusted customers, use a hypervisor or a separately isolated worker host and implement disk quotas, backups, abuse controls and monitoring before taking orders.
