# NeonHost — Discord VPS & Web Deploy Bot

NeonHost is a Discord slash-command bot for GitHub website deployments and SSHX browser-terminal VPS containers. It provisions per-customer Docker containers on your Linux server and enforces RAM, CPU quota and PID limits.

> **Important:** These are Linux userland containers, not dedicated KVM/QEMU virtual machines. All containers share the host Linux kernel. A selected OS changes the userspace/base image; it does not provide a separate kernel, systemd, nested Docker or KVM. Per-customer disk quotas are not enabled.

## Customer commands

- `/start` — show the hosting entry panel.
- `/vps` — request a VPS and choose Ubuntu, Debian or Alpine plus RAM/CPU limits.
- `/myvps` — show your VPS OS, actual container status, RAM cap, CPU quota, creation time and control buttons.
- `/ssh SERVICE_ID` — restart the SSHX terminal session and generate a fresh browser link.
- `/stop SERVICE_ID` and `/resume SERVICE_ID` — stop/start a service.
- `/reinstall SERVICE_ID` — confirm and rebuild your VPS using the same OS/RAM/CPU allocation. This deletes the old container filesystem.
- `/web` — request deployment of a public GitHub repository containing a Dockerfile.
- `/my`, `/redeploy SERVICE_ID`, `/help`, `/plans`.

The `/myvps` control panel also has **Start**, **Stop**, **Reinstall**, and **SSH / New Link** buttons. Reinstall requires a second confirmation.

## Operating systems offered

- Ubuntu 24.04 LTS
- Debian 12 Bookworm
- Alpine Linux 3.22

These are the supported container base images in this version. They are not full virtual machines.

## SSHX access model

The bot runs the official `sshx` client inside each customer's isolated container. SSHX prints a unique browser-session link. The link is a bearer credential: anyone who obtains it can interact with that terminal. Send it only by customer DM, do not post it in a public channel, and ask customers to keep it private.

The SSHX client connects to the hosted `sshx.io` service. This project does not self-host the SSHX relay. If the customer stops the VPS, the current terminal session ends. Starting it again or pressing **SSH / New Link** starts a new session and issues a new link.

This is not a conventional SSH server and does not provide an OpenSSH host/port. Customers do not submit SSH public keys in this mode.

## Admin commands

- `/orders` — pending requests.
- `/approve ORDER_ID [ram_mb] [cpu_quota]` — inspect/approve and provision a service. Optional limits override the requested/default values.
- `/reject ORDER_ID` — reject a request.
- `/capacity` — show detected host capacity and safe remaining limits.
- `/instances` — list services.
- `/remove SERVICE_ID` — delete a managed service.

Only users listed in `ADMIN_IDS` can run admin commands.

## Requirements

- Ubuntu/Debian Linux VPS with Docker Engine and Docker Compose plugin.
- Discord application with the `bot` and `applications.commands` OAuth scopes.
- Bot permissions to view/send messages, embed links and use application commands.
- Discord numeric admin user ID.
- Public IP/hostname for website links.
- Inbound TCP 80/443 for Caddy when using a domain. Without a domain, direct website ports 8000–8999 are used.
- Outbound HTTPS/DNS from the VPS and from customer containers so the SSHX client can reach `sshx.io`.

## Install

```bash
git clone https://github.com/ujwalffyt/BOT.git neonhost
cd neonhost
chmod +x deploy.sh
./deploy.sh
```

The first run creates `.env`. Edit it:

- `DISCORD_BOT_TOKEN` — bot token from the Discord Developer Portal.
- `ADMIN_IDS` — comma-separated numeric Discord user IDs.
- `PUBLIC_HOST` — actual public IP/hostname.
- `DISCORD_GUILD_ID=0` — global slash-command sync. Set a server ID only when the bot is already invited to that server and you want test-server sync.
- `WEB_ROOT_DOMAIN` — optional domain you control. Create wildcard DNS for `*.WEB_ROOT_DOMAIN` pointing to `PUBLIC_HOST`.

Then start the stack:

```bash
./deploy.sh
docker compose logs -f bot
```

Invite the bot with both the `bot` and `applications.commands` scopes. Slash commands are synced globally unless `DISCORD_GUILD_ID` is configured.

## Capacity settings

The sample `.env.example` reserves 6 GB RAM for the host and keeps 2 GB free as a safety margin. Adjust these after checking the server's actual workloads. The bot also checks Docker's detected RAM, live available memory, CPU count and existing NeonHost allocations before provisioning.

- `MAX_INSTANCE_RAM_MB` and `MAX_INSTANCE_CPU_CORES` cap a single allocation.
- `VPS_RAM_MB` and `VPS_CPU_CORES` are the default VPS request limits.
- `WEB_RAM_MB` and `WEB_CPU_CORES` are default website container limits.

CPU quota is a cap, not a promise of a dedicated physical core. Allocated RAM is a Docker memory cap, not extra RAM invented by the bot.

## Website deployment

Customers submit a public GitHub repo, the internal app port and a slug using `/web`. The repo must contain a Dockerfile, and the app must listen on `0.0.0.0`. An admin must inspect and approve the repository before it is built.

**Only approve source code you own or trust.** Dockerfile instructions execute during image builds on the host's Docker builder. This first version is intended for reviewed repositories, not arbitrary untrusted public builds.

With `WEB_ROOT_DOMAIN` and wildcard DNS configured, Caddy routes HTTPS to the app. Otherwise, the bot returns a direct HTTP port URL.

## VPS lifecycle

1. Customer requests a VPS with `/vps`, chooses OS, RAM and CPU quota.
2. Admin reviews the request and runs `/approve ORDER_ID`. Admin can override RAM/CPU during approval.
3. Bot builds the selected base image with SSHX installed, starts a resource-limited container and DMs the link.
4. Customer runs `/myvps` for details and controls.

The VPS control buttons:
- **Start** — starts a stopped container and returns its new SSHX link.
- **Stop** — stops the container and ends the current SSHX session.
- **Reinstall** — requires confirmation, deletes the old container filesystem, then rebuilds the same OS with the same RAM/CPU limits.
- **SSH / New Link** — restarts the terminal session and sends a fresh SSHX link; the old link's session ends.

## Security and operational limitations

- **SSHX links are secrets.** Anyone who gets the URL may use the live terminal. Send links privately and rotate them if exposed.
- **Third-party relay:** SSHX uses the hosted `sshx.io` relay; this project does not operate its own SSHX relay. Do not use this access model for workloads that require a self-hosted terminal relay.
- **Docker socket:** the bot mounts `/var/run/docker.sock`. Docker API access is effectively host-root access. Keep the bot token secret and keep `ADMIN_IDS` restricted. Do not expose Docker's API over public TCP.
- **Container isolation:** containers share the host kernel. For hostile or mutually untrusted tenants, use a hypervisor or separately isolated worker machines.
- **Disk quotas:** per-customer disk/storage quotas are not implemented. A container may consume host disk. Do not promise disk quota until filesystem/project quotas or isolated volumes are added and tested.
- **Build safety:** reviewed Dockerfiles only. Untrusted builds need separately isolated, resource-limited build workers.
- **No payments/refills:** payments, subscriptions, automatic renewals/refills and billing are not implemented.
- **Backups:** reinstall and removal delete the container's writable filesystem. Customers must back up files before reinstalling. Back up `./data` and customer files.

## Maintenance

```bash
docker compose ps
docker compose logs -f bot
docker compose restart bot
docker compose down
```
