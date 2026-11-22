# NeonHost — Discord VPS & Web Deploy Bot

NeonHost is a Discord hosting panel for GitHub website deployments and OS-selected SSHX browser-terminal VPS containers. It provisions per-customer Docker containers on your Linux server and applies per-service RAM, CPU quota and PID limits.

> **Important:** VPS choices are Linux container userlands, not dedicated KVM/QEMU virtual machines. Containers share the host kernel. Per-customer disk quotas are not enabled.

## Simple customer panel

Run `/start`. The panel has four direct buttons:

- **VPS Hosting** — see how to request a VPS.
- **Deploy Website** — opens a short form; customers enter a public GitHub URL, the app port and a site name.
- **My VPS** — opens the VPS control instructions; run `/myvps` for the control panel.
- **My Websites** — opens the website selector or directly opens the only website owned by the customer.

## Website hosting

### Deploy a website

Run `/web` or click **Deploy Website** on `/start`. A modal asks for:
1. Public GitHub repository URL.
2. Internal application port (examples: 3000 for many Node apps, 8080, or 80 for static web servers).
3. A short website name/slug.

The app must listen on `0.0.0.0` and the repository must include a Dockerfile. The request enters the admin review queue. An admin approves it with `/approve ORDER_ID`; only then is the repository cloned, built and started.

### Manage websites

Run `/myweb` or click **My Websites** on `/start`. Select a website to view its URL, status, RAM cap, CPU quota, app port, GitHub repository and creation time.

The website control panel provides:
- **Start** — start a stopped website.
- **Stop** — stop the website container.
- **Redeploy** — build a replacement from the saved GitHub repository and switch to it.
- **Delete** — permanently remove the website after confirmation.
- **Open Website** — open the live URL.

Website redeploy is the equivalent of reinstall/rebuild for a web app. Website hosting does not use SSHX terminal access; SSHX is the separate customer VPS access method.

With `WEB_ROOT_DOMAIN` and wildcard DNS configured, Caddy can provide HTTPS subdomains. Otherwise the bot returns a direct HTTP port URL.

**Only approve code you own or trust.** Dockerfile build instructions execute during image builds through the host Docker builder. This first version is intended for admin-reviewed deployments, not arbitrary untrusted public builds.

## VPS hosting

Run `/vps` and choose:
- Ubuntu 24.04 LTS
- Debian 12 Bookworm
- Alpine Linux 3.22
- Requested RAM and CPU quota

An admin approves with `/approve ORDER_ID` and can override the requested allocation. The bot builds the selected base image, installs SSHX inside the isolated customer container and privately DMs the browser-terminal link.

### Manage VPS

Run `/myvps` for the selected OS, live container status, RAM allocation and usage, CPU quota and sampled CPU usage, service ID and controls.

Buttons:
- **Start** — start the container and provide a fresh SSHX link if it was stopped.
- **Stop** — stop the container; its active SSHX terminal ends.
- **Reinstall** — confirm, delete the current container filesystem and create a fresh container with the same OS/RAM/CPU settings.
- **SSH / New Link** — restart the SSHX terminal session and issue a new link.

You can also use `/ssh SERVICE_ID` and `/reinstall SERVICE_ID`. Reinstall deletes the old container filesystem and cannot be undone.

### SSHX access warning

SSHX provides a live browser-terminal session. Anyone who obtains its link can interact with that terminal. The bot sends links privately by Discord DM; customers should keep them secret. This project uses the hosted `sshx.io` relay and does not self-host the relay. This is not conventional OpenSSH access, and customers do not provide public SSH keys.

## Commands

Customer:
- `/start` — simple hosting dashboard.
- `/vps` — request a VPS and select OS/RAM/CPU.
- `/myvps` — VPS info and controls.
- `/ssh SERVICE_ID` — issue a fresh SSHX link.
- `/reinstall SERVICE_ID` — reinstall a VPS after confirmation.
- `/web` — open the website deploy form.
- `/myweb` — website information and controls.
- `/my` — list all services.
- `/stop SERVICE_ID`, `/resume SERVICE_ID` — generic service controls.
- `/redeploy SERVICE_ID` — rebuild a website.
- `/help`, `/plans`.

Admin:
- `/orders` — pending requests.
- `/approve ORDER_ID [ram_mb] [cpu_quota]` — provision with optional allocation overrides.
- `/reject ORDER_ID` — reject a request.
- `/capacity` — detected server capacity.
- `/instances` — list all managed services.
- `/remove SERVICE_ID` — delete a service.

Only users in `ADMIN_IDS` can run admin commands.

## Requirements and install

- Linux VPS with Docker Engine and Docker Compose plugin.
- Discord application invited with `bot` and `applications.commands` scopes.
- Bot permissions for messages, embeds and application commands.
- Discord numeric administrator ID.
- Public IP/hostname for web URLs.
- Outbound HTTPS/DNS from the host and customer containers so SSHX can reach `sshx.io`.

```bash
git clone https://github.com/ujwalffyt/BOT.git neonhost
cd neonhost
chmod +x deploy.sh
./deploy.sh
```

Edit `.env` with `DISCORD_BOT_TOKEN`, `ADMIN_IDS`, `PUBLIC_HOST`, and optional domain settings, then run:

```bash
./deploy.sh
docker compose logs -f bot
```

Set `DISCORD_GUILD_ID=0` for global slash-command sync. Use a guild ID only if the bot is already invited to that Discord server and you want guild-scoped sync.

## Capacity, security and limitations

- Sample .env settings reserve 6 GB for the host and 2 GB of free memory. Recalculate for actual server load.
- RAM values are Docker memory caps. CPU quota is a cap, not a dedicated physical core.
- The bot mounts `/var/run/docker.sock`; Docker API access is effectively host-root. Keep the bot token secret and restrict `ADMIN_IDS`. Never expose the Docker API over public TCP.
- Containers share the host kernel. For hostile/untrusted tenants, use KVM or separate worker hosts.
- Per-customer disk quotas are not implemented; do not promise a fixed disk quota.
- Payment, renewal and automatic refill/billing systems are not implemented.
- Reinstall and delete remove the writable container filesystem. Customers must back up files first.
- Keep SSHX links private; anyone possessing a link can use the terminal.

## Maintenance

```bash
docker compose ps
docker compose logs -f bot
docker compose restart bot
docker compose down
```
