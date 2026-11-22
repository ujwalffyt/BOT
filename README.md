# NeonHost — Discord VPS & Web Deploy Bot

A Discord slash-command bot for GitHub web deployments and SSH-accessible container VPS hosting. It provisions Docker containers on your Linux VPS and enforces per-service RAM, CPU quota, and PID limits.

> **VPS type:** this build provides an isolated Docker container with SSH, not a dedicated KVM/QEMU virtual machine. Containers share the host kernel; they do not provide systemd, nested Docker, KVM, or a per-customer disk quota.

## Features

- `/start` — Discord hosting panel with buttons.
- `/vps ram_mb cpu_quota ssh_public_key` — request an SSH-enabled container.
- `/web repo app_port site_slug` — request a GitHub Docker deployment.
- `/my`, `/stop`, `/resume`, `/redeploy`.
- Admin: `/orders`, `/approve`, `/reject`, `/capacity`, `/instances`, `/remove`.
- SQLite order/service state persists across restarts.
- Resource allocations checked against detected Docker host RAM, live available RAM, reserved RAM, CPU count, and resources already allocated by the bot.
- Customer SSH is public-key only; password login is disabled.
- Optional HTTPS subdomains through Caddy when you own a domain and configure wildcard DNS.

## Requirements

- Linux VPS with Docker Engine and the Docker Compose plugin.
- Discord application/bot created in the Discord Developer Portal, with the **applications.commands** and **bot** scopes.
- Bot invited to your Discord server with permission to view/send messages, embed links, and use application commands.
- Numeric Discord user ID for each administrator.
- Public server IPv4 address or hostname.
- TCP 22000–22999 open for customer SSH; TCP 8000–8999 is used for direct website ports when no domain is configured.
- For automatic HTTPS: a domain you control, wildcard DNS pointing to the server, and inbound TCP 80/443.

## Install

1. On the Linux VPS, install Docker Engine and the Docker Compose plugin.
2. Clone and enter the repository:

   ```bash
   git clone https://github.com/ujwalffyt/BOT.git neonhost
   cd neonhost
   chmod +x deploy.sh
   ./deploy.sh
   ```

3. Edit `.env` and set:
   - `DISCORD_BOT_TOKEN`: the bot token from the Discord Developer Portal.
   - `ADMIN_IDS`: your Discord numeric user ID.
   - `PUBLIC_HOST`: the actual public IP/hostname of the VPS.
   - Optionally `ADMIN_CHANNEL_ID` and `DISCORD_GUILD_ID`.
   - Optionally `WEB_ROOT_DOMAIN` and wildcard DNS for HTTPS.

4. Start the services:

   ```bash
   ./deploy.sh
   docker compose logs -f bot
   ```

5. Invite the bot with the `bot` and `applications.commands` scopes. Use `/start` in your server.

When `DISCORD_GUILD_ID` is set, slash commands are synced to that server for development. Otherwise, commands are synced globally; global command visibility can take longer to propagate.

## Customer flow

### SSH container VPS

1. Customer runs `/vps` and supplies RAM MB, CPU quota, and an **SSH public key**.
2. An administrator reviews the request in the admin channel or DMs and runs `/approve ORDER_ID`. They may override RAM/CPU allocation in the approval command.
3. The bot checks safe capacity, creates an isolated Docker network and SSH container, and sends the customer an SSH command:

   ```bash
   ssh root@YOUR_SERVER_IP -p 22000
   ```

4. The customer connects using the private key that matches the public key they submitted.

The SSH port is allocated from the configured range. Only the public key is sent to the bot; never ask a customer to share a private key or password.

### GitHub web hosting

1. Customer runs `/web` and supplies a public GitHub repository, the app's internal listening port, and a site slug.
2. An administrator inspects the repository and approves with `/approve ORDER_ID`.
3. The bot clones the repository, builds its Dockerfile, and runs it with the configured RAM/CPU limits.
4. With `WEB_ROOT_DOMAIN` and wildcard DNS configured, Caddy routes HTTPS to the website. Otherwise, a direct HTTP port URL is returned.

The app must listen on `0.0.0.0`, not just `127.0.0.1`. Its Dockerfile is executed during build, so **only approve repositories you own or trust**.

## Admin commands

- `/orders` — pending requests.
- `/approve ORDER_ID [ram_mb] [cpu_quota]` — provision a request with optional allocation overrides.
- `/reject ORDER_ID` — reject a request.
- `/capacity` — inspect detected host capacity.
- `/instances` — list managed services.
- `/remove SERVICE_ID` — permanently delete the container.

Customers can use `/my`, `/stop SERVICE_ID`, and `/resume SERVICE_ID`. Website customers can use `/redeploy SERVICE_ID`.

## Firewall

Allow inbound TCP 80 and 443 for Caddy (when using a domain). Allow TCP 22000–22999 for SSH containers. Without a domain, allow TCP 8000–8999 for direct website URLs. Apply the same rules in the VPS provider firewall. Preserve your own administrator SSH port; do not replace or close it.

## Important operational and security notes

- **Docker socket:** the bot mounts `/var/run/docker.sock`. Docker API access is effectively host-root access. Keep the bot token secret, restrict `ADMIN_IDS`, and do not grant the bot's admin role to customers.
- **Untrusted Dockerfiles:** image builds run repository instructions through the host Docker builder. This first version is for owner-reviewed code, not arbitrary public multi-tenant builds. Use isolated build workers for untrusted source.
- **Disk quotas:** this build does not enforce per-customer storage limits. Do not sell fixed disk quotas until you add and verify filesystem/project quotas or isolated volumes.
- **Isolation:** Docker containers share the host kernel. For hostile customers, use a hypervisor such as KVM/Proxmox or a separate worker host.
- **CPU:** CPU quota limits execution time; it is not a promise of a dedicated physical core.
- **RAM:** values are enforced Docker memory caps. The bot rejects provisioning when its conservative capacity check says resources are unavailable; it does not invent allocator RAM.
- **SSH troubleshooting:** check the assigned port, provider firewall, OS firewall, container status, and the customer's matching private key. sshx is not a persistent SSH fallback.
- **Backups:** back up `./data`, the database, and customer data. Removing a container deletes its writable filesystem.

## Management

```bash
docker compose ps
docker compose logs -f bot
docker compose restart bot
docker compose down
```

NeonHost does not yet include payments, automated renewals/refills, customer disk quotas, or dedicated KVM VM provisioning. Add these explicitly before offering them as services.
