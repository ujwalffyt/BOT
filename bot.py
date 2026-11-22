from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import discord
import docker
import psutil
from discord import app_commands
from discord.ext import commands

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("neonhost")

def integer(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc

def number(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "").strip()
if not TOKEN or TOKEN == "PASTE_DISCORD_BOT_TOKEN_HERE":
    raise RuntimeError("Set DISCORD_BOT_TOKEN in .env")
try:
    ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
except ValueError as exc:
    raise RuntimeError("ADMIN_IDS must contain Discord numeric user IDs") from exc
if not ADMIN_IDS:
    raise RuntimeError("Set at least one Discord numeric user ID in ADMIN_IDS")

ADMIN_CHANNEL_ID = integer("ADMIN_CHANNEL_ID", 0)
GUILD_ID = integer("DISCORD_GUILD_ID", 0)
PUBLIC_HOST = os.getenv("PUBLIC_HOST", "").strip()
if not PUBLIC_HOST or PUBLIC_HOST == "YOUR_SERVER_IP":
    raise RuntimeError("Set PUBLIC_HOST to the server public IP or hostname")
DOMAIN = os.getenv("WEB_ROOT_DOMAIN", "").strip().lower().strip(".")
if DOMAIN and not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", DOMAIN):
    raise RuntimeError("WEB_ROOT_DOMAIN must be a valid domain name")

RESERVED_RAM = integer("HOST_RESERVED_RAM_MB", 6144)
MIN_FREE_RAM = integer("MIN_FREE_RAM_MB", 2048)
RESERVED_CPU = number("HOST_RESERVED_CPU_CORES", 1.0)
MAX_RAM = integer("MAX_INSTANCE_RAM_MB", 8192)
MAX_CPU = number("MAX_INSTANCE_CPU_CORES", 4.0)
DEFAULT_VPS_RAM = integer("VPS_RAM_MB", 2048)
DEFAULT_VPS_CPU = number("VPS_CPU_CORES", 1.0)
DEFAULT_WEB_RAM = integer("WEB_RAM_MB", 1024)
DEFAULT_WEB_CPU = number("WEB_CPU_CORES", 1.0)
WEB_FIRST = integer("WEB_PORT_START", 8000)
WEB_LAST = integer("WEB_PORT_END", 8999)
DB_PATH = os.getenv("DB_PATH", "/app/data/hosting.sqlite3")
WORK = Path(os.getenv("WORK_DIR", "/app/work"))
SITES = Path(os.getenv("SITES_DIR", "/app/caddy/sites"))
VPS_IMAGE = "neonhost-vps:sshx-1"
PROXY_NETWORK = "neonhost-proxy"

if not 1024 <= WEB_FIRST <= WEB_LAST <= 65535:
    raise RuntimeError("Invalid web port range")
if min(RESERVED_RAM, MIN_FREE_RAM, MAX_RAM, DEFAULT_VPS_RAM, DEFAULT_WEB_RAM) < 1:
    raise RuntimeError("RAM limits must be positive")
if min(RESERVED_CPU, MAX_CPU, DEFAULT_VPS_CPU, DEFAULT_WEB_CPU) <= 0:
    raise RuntimeError("CPU limits must be positive")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
WORK.mkdir(parents=True, exist_ok=True)
SITES.mkdir(parents=True, exist_ok=True)

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def safe_error(value: str, limit: int = 900) -> str:
    return " ".join(re.sub(r"\x1b\[[0-9;]*m", "", value or "").split())[-limit:]

def check_repo(value: str) -> str:
    parsed = urlparse((value or "").strip())
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise ValueError("Use a public HTTPS GitHub repository URL.")
    if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
        raise ValueError("Do not include credentials, port, query, or fragment in the GitHub URL.")
    if not re.fullmatch(r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?", parsed.path):
        raise ValueError("Send a repository URL, not a branch, file, issue, or pull-request URL.")
    path = parsed.path.rstrip("/")
    return "https://github.com" + (path if path.endswith(".git") else path + ".git")

def check_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", (value or "").strip().lower()).strip("-")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?", slug):
        raise ValueError("Use 1-40 lowercase letters, numbers, and hyphens for the site slug.")
    return slug

def check_app_port(value: int) -> int:
    if not 1024 <= int(value) <= 65535:
        raise ValueError("Internal app port must be between 1024 and 65535.")
    return int(value)

class Store:
    def __init__(self):
        with self.db() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS orders(
              id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, username TEXT NOT NULL,
              kind TEXT NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS services(
              id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, order_id TEXT NOT NULL,
              kind TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
              container TEXT NOT NULL, network TEXT NOT NULL, ram_mb INTEGER NOT NULL,
              cpu REAL NOT NULL, host_port INTEGER, app_port INTEGER, url TEXT NOT NULL,
              domain TEXT NOT NULL, repo TEXT NOT NULL, image TEXT NOT NULL, created TEXT NOT NULL,
              os TEXT NOT NULL DEFAULT 'debian-12'
            );
            CREATE INDEX IF NOT EXISTS order_status_idx ON orders(status);
            CREATE INDEX IF NOT EXISTS service_user_idx ON services(user_id);
            """)
            service_columns = {row["name"] for row in con.execute("PRAGMA table_info(services)")}
            if "os" not in service_columns:
                con.execute("ALTER TABLE services ADD COLUMN os TEXT NOT NULL DEFAULT 'debian-12'")
    def db(self):
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con
    def new_order(self, user_id, username, kind, data):
        oid = uuid.uuid4().hex[:8]
        with self.db() as con:
            con.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?)", (oid, user_id, username, kind, json.dumps(data), "pending", utcnow()))
        return oid
    def order(self, oid):
        with self.db() as con:
            row = con.execute("SELECT * FROM orders WHERE id=?", (oid.lower(),)).fetchone()
        if not row: return None
        result = dict(row)
        result["data"] = json.loads(result["data"])
        return result
    def pending(self):
        with self.db() as con:
            rows = con.execute("SELECT * FROM orders WHERE status='pending' ORDER BY created").fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item["data"])
            items.append(item)
        return items
    def set_order(self, oid, status):
        with self.db() as con:
            con.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    def add_service(self, s):
        with self.db() as con:
            con.execute(
                "INSERT INTO services(id,user_id,order_id,kind,name,status,container,network,ram_mb,cpu,host_port,app_port,url,domain,repo,image,created,os) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    s["id"], s["user_id"], s["order_id"], s["kind"], s["name"], s["status"],
                    s["container"], s["network"], s["ram_mb"], s["cpu"], s.get("host_port"),
                    s.get("app_port"), s["url"], s.get("domain", ""), s.get("repo", ""),
                    s["image"], utcnow(), s.get("os", "debian-12")
                )
            )
    def update_service(self, s):
        with self.db() as con:
            con.execute(
                "UPDATE services SET user_id=?,order_id=?,kind=?,name=?,status=?,container=?,network=?,ram_mb=?,cpu=?,host_port=?,app_port=?,url=?,domain=?,repo=?,image=?,os=? WHERE id=?",
                (
                    s["user_id"], s["order_id"], s["kind"], s["name"], s["status"],
                    s["container"], s["network"], s["ram_mb"], s["cpu"], s.get("host_port"),
                    s.get("app_port"), s["url"], s.get("domain", ""), s.get("repo", ""),
                    s["image"], s.get("os", "debian-12"), s["id"]
                )
            )
    def service(self, sid):
        with self.db() as con:
            row = con.execute("SELECT * FROM services WHERE id=?", (sid.lower(),)).fetchone()
        return dict(row) if row else None
    def services(self, user_id=None):
        with self.db() as con:
            if user_id is None:
                rows = con.execute("SELECT * FROM services ORDER BY created DESC").fetchall()
            else:
                rows = con.execute("SELECT * FROM services WHERE user_id=? ORDER BY created DESC", (user_id,)).fetchall()
        return [dict(row) for row in rows]
    def allocations(self):
        with self.db() as con:
            row = con.execute("SELECT COALESCE(SUM(ram_mb),0) AS ram, COALESCE(SUM(cpu),0) AS cpu FROM services WHERE status IN ('running','stopped','creating')").fetchone()
        return int(row["ram"]), float(row["cpu"])
    def port_used(self, port):
        with self.db() as con:
            return con.execute("SELECT 1 FROM services WHERE host_port=? AND status IN ('running','stopped','creating')", (port,)).fetchone() is not None
    def set_service_status(self, sid, status):
        with self.db() as con:
            con.execute("UPDATE services SET status=? WHERE id=?", (status, sid))
    def update_service_url(self, sid, url):
        with self.db() as con:
            con.execute("UPDATE services SET url=? WHERE id=?", (url, sid))
    def delete_service(self, sid):
        with self.db() as con:
            con.execute("DELETE FROM services WHERE id=?", (sid,))

DB = Store()

class Host:
    def __init__(self):
        self.client = docker.from_env(timeout=900)
        self.ensure_proxy()
    def ensure_proxy(self):
        try:
            self.client.networks.get(PROXY_NETWORK)
        except docker.errors.NotFound:
            self.client.networks.create(PROXY_NETWORK, driver="bridge", labels={"neonhost": "proxy"})
    def capacity(self):
        info = self.client.info()
        total = int(info.get("MemTotal", 0) / 1024 / 1024)
        cpu_count = int(info.get("NCPU", os.cpu_count() or 1))
        free = int(psutil.virtual_memory().available / 1024 / 1024)
        used_ram, used_cpu = DB.allocations()
        remaining_ram = min(max(0, total - RESERVED_RAM - used_ram), max(0, free - MIN_FREE_RAM))
        remaining_cpu = max(0.0, cpu_count - RESERVED_CPU - used_cpu)
        return {"total": total, "free": free, "used_ram": used_ram, "used_cpu": used_cpu,
                "remaining_ram": remaining_ram, "cpus": cpu_count, "remaining_cpu": remaining_cpu}
    def check_capacity(self, ram, cpu):
        if ram < 128 or ram > MAX_RAM:
            raise RuntimeError(f"RAM must be 128-{MAX_RAM} MB.")
        if cpu <= 0 or cpu > MAX_CPU:
            raise RuntimeError(f"CPU quota must be above 0 and no more than {MAX_CPU}.")
        cap = self.capacity()
        if ram > cap["remaining_ram"]:
            raise RuntimeError(f"Not enough safe RAM. Requested {ram} MB; safe remaining is {cap['remaining_ram']} MB.")
        if cpu > cap["remaining_cpu"]:
            raise RuntimeError(f"Not enough safe CPU quota. Requested {cpu}; remaining is {cap['remaining_cpu']:.2f}.")
    def free_port(self, first, last):
        for port in range(first, last + 1):
            if DB.port_used(port): continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("0.0.0.0", port))
                    return port
                except OSError:
                    pass
        raise RuntimeError(f"No free host port in range {first}-{last}.")
    def build_vps_image(self):
        try:
            self.client.images.get(VPS_IMAGE)
        except docker.errors.ImageNotFound:
            self.client.images.build(path="/app/vps-image", tag=VPS_IMAGE, pull=True, rm=True, forcerm=True)
    def wait_for_sshx_link(self, container, timeout=45, since=0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            container.reload()
            try:
                raw = container.logs(stdout=True, stderr=True, since=since, tail=100)
                output = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            except docker.errors.APIError:
                output = ""
            link = next((part for part in output.split() if part.startswith("https://sshx.io/s/")), None)
            if link:
                return link.rstrip(".,;)]}")
            if container.status != "running":
                raise RuntimeError(
                    "The sshx process exited before producing a session link. "
                    "Inspect its logs with docker logs " + container.name
                )
            time.sleep(0.75)
        raise RuntimeError(
            f"sshx did not return a session link within {timeout} seconds. "
            "Check outbound HTTPS/DNS and inspect the container logs."
        )

    def provision_vps(self, order, ram, cpu):
        self.check_capacity(ram, cpu)
        self.build_vps_image()
        self.check_capacity(ram, cpu)
        sid = uuid.uuid4().hex[:8]
        name, net = f"neonhost-vps-{sid}", f"neonhost-vps-net-{sid}"
        network = self.client.networks.create(
            net, driver="bridge", labels={"neonhost.managed": "true", "neonhost.service": sid}
        )
        container = None
        started_at = int(time.time())
        try:
            container = self.client.containers.run(
                VPS_IMAGE, name=name, detach=True,
                tty=True, stdin_open=True, network=net,
                mem_limit=f"{ram}m", memswap_limit=f"{ram}m",
                nano_cpus=int(cpu * 1_000_000_000), pids_limit=256,
                cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
                restart_policy={"Name": "unless-stopped"}, init=True,
                labels={"neonhost.managed": "true", "neonhost.kind": "vps-sshx", "neonhost.service": sid},
            )
            link = self.wait_for_sshx_link(container, since=started_at)
        except Exception:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            try:
                network.remove()
            except Exception:
                pass
            raise
        return {"id": sid, "user_id": order["user_id"], "order_id": order["id"],
                "kind": "sshx-vps-container", "name": "SSHX browser VPS", "status": "running",
                "container": container.name, "network": net, "ram_mb": ram, "cpu": cpu,
                "host_port": None, "app_port": None, "url": link,
                "domain": "", "repo": "", "image": VPS_IMAGE}
    def clone(self, repo, folder):
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(["git", "clone", "--depth=1", "--", repo, str(folder)],
                           check=True, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("GitHub clone timed out after 3 minutes.") from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("Git clone failed: " + safe_error((exc.stderr or "") + " " + (exc.stdout or ""))) from exc
    def provision_web(self, order, ram, cpu):
        data = order["data"]
        repo, slug = check_repo(data["repo"]), check_slug(data["slug"])
        app_port = check_app_port(int(data["port"]))
        self.check_capacity(ram, cpu)
        sid = uuid.uuid4().hex[:8]
        image = f"neonhost-web:{sid}"
        folder = WORK / sid
        self.clone(repo, folder)
        try:
            if not (folder / "Dockerfile").is_file():
                raise RuntimeError("Repository has no Dockerfile. Add a Dockerfile before requesting deployment.")
            # Only approve repositories you own or trust: Dockerfile build instructions execute on the host Docker builder.
            self.client.images.build(path=str(folder), tag=image, pull=True, rm=True, forcerm=True)
        except docker.errors.BuildError as exc:
            raise RuntimeError("Docker build failed: " + safe_error(str(exc))) from exc
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        self.check_capacity(ram, cpu)
        name = f"neonhost-web-{sid}"
        domain = f"{slug}-{sid}.{DOMAIN}" if DOMAIN else ""
        port = None if domain else self.free_port(WEB_FIRST, WEB_LAST)
        site_file = SITES / f"{sid}.caddy"
        try:
            container = self.client.containers.run(
                image, name=name, detach=True,
                ports=None if domain else {f"{app_port}/tcp": port},
                network=PROXY_NETWORK,
                mem_limit=f"{ram}m", memswap_limit=f"{ram}m",
                nano_cpus=int(cpu * 1_000_000_000), pids_limit=256,
                cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
                restart_policy={"Name": "unless-stopped"},
                labels={"neonhost.managed": "true", "neonhost.kind": "web", "neonhost.service": sid},
            )
            if domain:
                site_file.write_text(
                    f"{domain} {{\n    encode zstd gzip\n    reverse_proxy {name}:{app_port}\n}}\n",
                    encoding="utf-8")
                caddy = self.client.containers.get("neonhost-caddy")
                result = caddy.exec_run(["caddy", "reload", "--config", "/etc/caddy/Caddyfile"])
                if result.exit_code:
                    output = result.output.decode("utf-8", errors="replace") if isinstance(result.output, bytes) else str(result.output)
                    raise RuntimeError("Caddy reload failed: " + safe_error(output))
            url = f"https://{domain}" if domain else f"http://{PUBLIC_HOST}:{port}"
        except Exception:
            try: self.client.containers.get(name).remove(force=True)
            except Exception: pass
            site_file.unlink(missing_ok=True)
            raise
        return {"id": sid, "user_id": order["user_id"], "order_id": order["id"],
                "kind": "web-app", "name": slug, "status": "running",
                "container": name, "network": PROXY_NETWORK, "ram_mb": ram, "cpu": cpu,
                "host_port": port, "app_port": app_port, "url": url,
                "domain": domain, "repo": repo, "image": image}
    def set_running(self, service, running):
        container = self.client.containers.get(service["container"])
        if running:
            started_at = int(time.time())
            container.start()
            if service["kind"] == "sshx-vps-container":
                return self.wait_for_sshx_link(container, since=started_at)
            return None
        container.stop(timeout=10)
        return None
    def remove(self, service):
        try: self.client.containers.get(service["container"]).remove(force=True)
        except docker.errors.NotFound: pass
        if service.get("domain"):
            (SITES / f"{service['id']}.caddy").unlink(missing_ok=True)
            try:
                self.client.containers.get("neonhost-caddy").exec_run(["caddy", "reload", "--config", "/etc/caddy/Caddyfile"])
            except docker.errors.NotFound:
                pass
        if service["kind"] in {"vps-container", "sshx-vps-container"}:
            try: self.client.networks.get(service["network"]).remove()
            except docker.errors.NotFound: pass
        # VPS instances share one base image; remove per-service images only for web apps.
        if service["kind"] == "web-app":
            try: self.client.images.remove(service["image"], force=True)
            except docker.errors.ImageNotFound: pass

HOST = None
bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())

async def admin_check(interaction):
    if interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("You are not authorized to use this admin command.", ephemeral=True)
        return False
    return True

async def notify_admin(embed):
    if ADMIN_CHANNEL_ID:
        channel = bot.get_channel(ADMIN_CHANNEL_ID)
        if channel is None:
            try: channel = await bot.fetch_channel(ADMIN_CHANNEL_ID)
            except discord.DiscordException: channel = None
        if channel is not None:
            try:
                await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                return
            except discord.DiscordException: log.exception("Could not send admin channel message")
    for admin_id in ADMIN_IDS:
        try: await (await bot.fetch_user(admin_id)).send(embed=embed)
        except discord.DiscordException: log.warning("Could not DM admin %s", admin_id)

def request_embed(oid, user, kind, data):
    embed = discord.Embed(title=f"New {kind.upper()} hosting request", color=discord.Color.blurple())
    embed.add_field(name="Order ID", value=oid, inline=True)
    embed.add_field(name="Customer", value=f"{user} ({user.id})", inline=True)
    if kind == "vps":
        embed.add_field(name="Requested allocation", value=f"{data['ram_mb']} MB RAM / {data['cpu']} CPU quota", inline=False)
        embed.add_field(name="Access method", value="A private sshx browser-terminal link will be generated after approval.", inline=False)
    else:
        embed.add_field(name="GitHub repository", value=data["repo"], inline=False)
        embed.add_field(name="Internal port", value=str(data["port"]), inline=True)
        embed.add_field(name="Site slug", value=data["slug"], inline=True)
    embed.set_footer(text="Review the request and repository before using /approve.")
    return embed

class HostingPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
    @discord.ui.button(label="VPS hosting", style=discord.ButtonStyle.primary, emoji="🖥️")
    async def vps_button(self, interaction, button):
        await interaction.response.send_message(
            "Request a VPS with /vps. Choose RAM and CPU quota; the bot creates an sshx browser-terminal link after approval. "
            "The administrator controls the final allocation.", ephemeral=True)
    @discord.ui.button(label="Deploy website", style=discord.ButtonStyle.success, emoji="🌐")
    async def web_button(self, interaction, button):
        await interaction.response.send_message(
            "Deploy with /web. Send a public GitHub repo, the app's internal port, and a site slug. "
            "The repository must have a Dockerfile and listen on 0.0.0.0.", ephemeral=True)
    @discord.ui.button(label="My services", style=discord.ButtonStyle.secondary, emoji="📦")
    async def my_button(self, interaction, button):
        items = DB.services(interaction.user.id)
        if not items:
            await interaction.response.send_message("You have no active services.", ephemeral=True)
            return
        lines = [f"{s['id']} • {s['name']} • {s['status']} • {s['ram_mb']} MB / {s['cpu']} CPU" for s in items[:15]]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.event
async def setup_hook():
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        try:
            synced = await bot.tree.sync(guild=guild)
            log.info("Synced %d commands to test guild %d", len(synced), GUILD_ID)
            return
        except discord.Forbidden:
            # Missing Access: wrong guild ID, bot not in the guild, or missing app-command access.
            log.exception(
                "Cannot sync commands to DISCORD_GUILD_ID=%d (Missing Access). "
                "Falling back to global command sync. Check the guild ID and bot invite scopes.",
                GUILD_ID,
            )
        except discord.HTTPException:
            log.exception(
                "Guild command sync failed for DISCORD_GUILD_ID=%d. Trying global sync instead.",
                GUILD_ID,
            )

    try:
        synced = await bot.tree.sync()
        log.info("Synced %d global commands", len(synced))
    except discord.HTTPException:
        # Do not crash the Gateway client if command-sync API is temporarily unavailable.
        log.exception(
            "Global slash-command sync failed. The bot will still connect; "
            "check that the bot invite included the applications.commands scope."
        )

@bot.event
async def on_ready():
    log.info("Ready as %s", bot.user)

@bot.tree.command(name="start", description="Open the hosting and web deployment panel")
async def start(interaction: discord.Interaction):
    # Defer the initial response immediately to avoid Discord's short initial-response deadline.
    await interaction.response.defer(ephemeral=False, thinking=False)
    embed = discord.Embed(
        title="NeonHost • Hosting & Deploy",
        description="Deploy a website from GitHub or request an SSH-enabled container VPS. "
                    "New services are reviewed by an administrator before provisioning.",
        color=discord.Color.blurple())
    embed.add_field(name="Web hosting", value="GitHub → Docker build → live URL", inline=True)
    embed.add_field(name="VPS hosting", value="sshx browser terminal with enforced RAM and CPU caps", inline=True)
    embed.set_footer(text="Container VPS is not a dedicated KVM virtual machine.")
    await interaction.followup.send(embed=embed, view=HostingPanel())

@bot.tree.command(name="help", description="Show hosting commands")
async def help_command(interaction: discord.Interaction):
    # Acknowledge immediately so the interaction token does not expire before the reply.
    await interaction.response.defer(ephemeral=True, thinking=False)
    text = (
        "/start — open the panel\n/plans — show configured limits\n"
        "/vps — request an sshx browser VPS\n/web — request GitHub website deployment\n"
        "/my — list your services\n/stop and /resume — control a service\n"
        "/redeploy — rebuild your website from GitHub"
    )
    if interaction.user.id in ADMIN_IDS:
        text += "\n/orders and /approve — manage requests\n/capacity and /instances — server status\n/remove — remove a service"
    await interaction.followup.send(text, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    original = getattr(error, "original", error)
    log.error("Slash command failed: %s", original, exc_info=(type(original), original, original.__traceback__))
    message = "That command failed. Please try again. If it keeps happening, send the admin the latest bot logs."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.warning("Discord would not accept the error response for interaction %s", interaction.id)

@bot.tree.command(name="plans", description="Show current VPS and website resource limits")
async def plans(interaction: discord.Interaction):
    embed = discord.Embed(title="Hosting resource limits", color=discord.Color.green())
    embed.add_field(name="VPS", value=f"Default request: {DEFAULT_VPS_RAM} MB RAM / {DEFAULT_VPS_CPU} CPU quota\nMaximum per service: {MAX_RAM} MB / {MAX_CPU} CPU quota", inline=False)
    embed.add_field(name="Web hosting", value=f"Default: {DEFAULT_WEB_RAM} MB RAM / {DEFAULT_WEB_CPU} CPU quota per app", inline=False)
    embed.add_field(name="Read carefully", value="CPU values are limits, not dedicated cores. sshx links are live shared terminal sessions; anyone with a link can interact. Disk quotas are not enabled in this version.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="vps", description="Request a VPS with an sshx browser-terminal link")
@app_commands.describe(ram_mb="RAM requested in MB; admin approves the actual allocation",
                       cpu_quota="CPU quota requested; it is not a dedicated core count")
async def vps(interaction: discord.Interaction,
              ram_mb: app_commands.Range[int, 128, 8192],
              cpu_quota: app_commands.Range[float, 0.1, 4.0]):
    if ram_mb > MAX_RAM or cpu_quota > MAX_CPU:
        await interaction.response.send_message(
            f"Configured maximum is {MAX_RAM} MB RAM and {MAX_CPU} CPU quota.", ephemeral=True)
        return
    data = {"ram_mb": int(ram_mb), "cpu": float(cpu_quota)}
    oid = DB.new_order(interaction.user.id, str(interaction.user), "vps", data)
    await interaction.response.send_message(
        f"VPS request submitted. Order ID: {oid}. An admin must approve it. "
        "After provisioning, a private sshx browser-terminal link will be sent to your DMs.",
        ephemeral=True)
    await notify_admin(request_embed(oid, interaction.user, "vps", data))

@bot.tree.command(name="web", description="Request deployment from a public GitHub repository")
@app_commands.describe(repo="Public GitHub repository URL; must include a Dockerfile",
                       app_port="Port the app listens on inside the container",
                       site_slug="Short name for the website, e.g. portfolio")
async def web(interaction: discord.Interaction, repo: str,
              app_port: app_commands.Range[int, 1024, 65535], site_slug: str):
    try:
        data = {"repo": check_repo(repo), "port": check_app_port(app_port), "slug": check_slug(site_slug)}
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return
    oid = DB.new_order(interaction.user.id, str(interaction.user), "web", data)
    await interaction.response.send_message(f"Website deployment request submitted. Order ID: {oid}. An admin will review it.", ephemeral=True)
    await notify_admin(request_embed(oid, interaction.user, "web", data))

@bot.tree.command(name="my", description="List your hosting services")
async def my(interaction: discord.Interaction):
    items = DB.services(interaction.user.id)
    if not items:
        await interaction.response.send_message("You do not have any services yet.", ephemeral=True)
        return
    embed = discord.Embed(title="Your services", color=discord.Color.blurple())
    for s in items[:15]:
        embed.add_field(name=f"{s['name']} ({s['id']})",
                        value=f"{s['kind']} • {s['status']}\nRAM cap {s['ram_mb']} MB • CPU quota {s['cpu']}\n{s['url']}",
                        inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

async def set_service(interaction, sid, running):
    s = DB.service(sid)
    if not s or (s["user_id"] != interaction.user.id and interaction.user.id not in ADMIN_IDS):
        await interaction.response.send_message("Service not found or you do not own it.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        new_link = await asyncio.to_thread(HOST.set_running, s, running)
        DB.set_service_status(sid, "running" if running else "stopped")
        if new_link:
            DB.update_service_url(sid, new_link)
            await interaction.followup.send(
                f"Service {sid} is running again. Your new sshx session link is:\n{new_link}\n"
                "Keep this link private; anyone who has it can interact with the terminal.",
                ephemeral=True)
            try:
                customer = await bot.fetch_user(s["user_id"])
                await customer.send(
                    f"Your NeonHost VPS {sid} has restarted. New sshx session link:\n{new_link}\n"
                    "Keep this link private; anyone who has it can interact with the terminal.")
            except discord.DiscordException:
                log.info("Could not DM refreshed sshx link for service %s", sid)
        else:
            await interaction.followup.send(f"Service {sid} is now {'running' if running else 'stopped'}.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send("Could not change service state: " + safe_error(str(exc)), ephemeral=True)

@bot.tree.command(name="stop", description="Stop one of your hosting services")
async def stop(interaction: discord.Interaction, service_id: str):
    await set_service(interaction, service_id, False)

@bot.tree.command(name="resume", description="Start one of your stopped services")
async def resume(interaction: discord.Interaction, service_id: str):
    await set_service(interaction, service_id, True)

@bot.tree.command(name="redeploy", description="Rebuild your website using its saved GitHub repository")
async def redeploy(interaction: discord.Interaction, service_id: str):
    s = DB.service(service_id)
    if not s or s["user_id"] != interaction.user.id or s["kind"] != "web-app":
        await interaction.response.send_message("Website not found or you do not own it.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    old_order = {"id": s["order_id"], "user_id": s["user_id"],
                 "data": {"repo": s["repo"], "slug": s["name"], "port": s["app_port"]}}
    try:
        replacement = await asyncio.to_thread(HOST.provision_web, old_order, s["ram_mb"], s["cpu"])
        DB.add_service(replacement)
        await asyncio.to_thread(HOST.remove, s)
        DB.delete_service(service_id)
        await interaction.followup.send(f"Website redeployed. New service ID: {replacement['id']} • {replacement['url']}", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send("Redeploy failed; previous service was kept where possible. " + safe_error(str(exc)), ephemeral=True)

@bot.tree.command(name="orders", description="Admin: list pending hosting requests")
async def orders(interaction: discord.Interaction):
    if not await admin_check(interaction): return
    items = DB.pending()
    if not items:
        await interaction.response.send_message("No pending orders.", ephemeral=True)
        return
    lines = []
    for o in items[:20]:
        d = o["data"]
        summary = (f"{d['ram_mb']} MB / {d['cpu']} CPU quota" if o["kind"] == "vps"
                   else f"{d['repo']} • port {d['port']} • {d['slug']}")
        lines.append(f"{o['id']} • {o['kind']} • customer {o['user_id']}\n{summary}")
    await interaction.response.send_message("\n\n".join(lines), ephemeral=True)

@bot.tree.command(name="approve", description="Admin: approve and provision an order")
@app_commands.describe(order_id="Order ID shown by /orders",
                       ram_mb="Final RAM allocation in MB; optional override",
                       cpu_quota="Final CPU quota; optional override")
async def approve(interaction: discord.Interaction, order_id: str,
                  ram_mb: app_commands.Range[int, 128, 8192] | None = None,
                  cpu_quota: float | None = None):
    if not await admin_check(interaction): return
    order = DB.order(order_id)
    if not order or order["status"] != "pending":
        await interaction.response.send_message("Order not found or no longer pending.", ephemeral=True)
        return
    data = order["data"]
    ram = int(ram_mb if ram_mb is not None else (data["ram_mb"] if order["kind"] == "vps" else DEFAULT_WEB_RAM))
    cpu = float(cpu_quota if cpu_quota is not None else (data["cpu"] if order["kind"] == "vps" else DEFAULT_WEB_CPU))
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        service = await asyncio.to_thread(HOST.provision_vps if order["kind"] == "vps" else HOST.provision_web,
                                          order, ram, cpu)
        DB.add_service(service)
        DB.set_order(order_id, "approved")
        await interaction.followup.send(f"Order {order_id} approved. Service {service['id']} is running.\n{service['url']}", ephemeral=True)
        try:
            customer = await bot.fetch_user(order["user_id"])
            await customer.send(
                f"Your NeonHost service is ready.\nService ID: {service['id']}\n"
                f"Type: {service['kind']}\nRAM cap: {ram} MB\nCPU quota: {cpu}\n"
                f"Access: {service['url']}\n"
                "This is an sshx live browser-terminal session inside your isolated container. "
                "Anyone with the link can interact with its terminal. Keep it private; restarting the VPS creates a new link.")
        except discord.DiscordException:
            log.info("Unable to DM customer for service %s", service["id"])
    except Exception as exc:
        DB.set_order(order_id, "pending")
        await interaction.followup.send("Provisioning failed; order remains pending. " + safe_error(str(exc)), ephemeral=True)
        log.exception("Failed provisioning order %s", order_id)

@bot.tree.command(name="reject", description="Admin: reject a pending order")
async def reject(interaction: discord.Interaction, order_id: str):
    if not await admin_check(interaction): return
    order = DB.order(order_id)
    if not order or order["status"] != "pending":
        await interaction.response.send_message("Order not found or no longer pending.", ephemeral=True)
        return
    DB.set_order(order_id, "rejected")
    await interaction.response.send_message(f"Order {order_id} rejected.", ephemeral=True)
    try: await (await bot.fetch_user(order["user_id"])).send(f"Your NeonHost request {order_id} was not approved.")
    except discord.DiscordException: pass

@bot.tree.command(name="capacity", description="Admin: inspect detected server RAM and CPU capacity")
async def capacity(interaction: discord.Interaction):
    if not await admin_check(interaction): return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        c = await asyncio.to_thread(HOST.capacity)
        embed = discord.Embed(title="Detected host capacity", color=discord.Color.green())
        embed.add_field(name="Total RAM", value=f"{c['total']:,} MB", inline=True)
        embed.add_field(name="Available RAM", value=f"{c['free']:,} MB", inline=True)
        embed.add_field(name="Allocated RAM", value=f"{c['used_ram']:,} MB", inline=True)
        embed.add_field(name="Safe RAM remaining", value=f"{c['remaining_ram']:,} MB", inline=True)
        embed.add_field(name="Detected CPU count", value=str(c["cpus"]), inline=True)
        embed.add_field(name="Safe CPU quota remaining", value=f"{c['remaining_cpu']:.2f}", inline=True)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as exc:
        await interaction.followup.send("Capacity check failed: " + safe_error(str(exc)), ephemeral=True)

@bot.tree.command(name="instances", description="Admin: list all managed services")
async def instances(interaction: discord.Interaction):
    if not await admin_check(interaction): return
    items = DB.services()
    if not items:
        await interaction.response.send_message("No services have been provisioned.", ephemeral=True)
        return
    lines = [f"{s['id']} • owner {s['user_id']} • {s['kind']} • {s['status']} • {s['ram_mb']} MB / {s['cpu']} CPU" for s in items[:25]]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="remove", description="Admin: permanently remove a managed service")
async def remove(interaction: discord.Interaction, service_id: str):
    if not await admin_check(interaction): return
    s = DB.service(service_id)
    if not s:
        await interaction.response.send_message("Service not found.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await asyncio.to_thread(HOST.remove, s)
        DB.delete_service(service_id)
        await interaction.followup.send(f"Service {service_id} removed. Container data is deleted; back up customer files first.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send("Removal failed: " + safe_error(str(exc)), ephemeral=True)

async def main():
    global HOST
    HOST = Host()
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
