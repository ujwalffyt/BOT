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
VPS_IMAGE_PREFIX = "neonhost-vps"
VPS_OS_OPTIONS = {
    "ubuntu-24.04": {"label": "Ubuntu 24.04 LTS", "base": "ubuntu:24.04"},
    "debian-12": {"label": "Debian 12 Bookworm", "base": "debian:12-slim"},
    "alpine-3.22": {"label": "Alpine Linux 3.22", "base": "alpine:3.22"},
}
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
    def build_vps_image(self, os_key):
        if os_key not in VPS_OS_OPTIONS:
            raise RuntimeError("Unsupported VPS operating system.")
        image_tag = f"{VPS_IMAGE_PREFIX}:{os_key}"
        try:
            self.client.images.get(image_tag)
        except docker.errors.ImageNotFound:
            self.client.images.build(
                path="/app/vps-image",
                tag=image_tag,
                buildargs={"BASE_IMAGE": VPS_OS_OPTIONS[os_key]["base"]},
                pull=True,
                rm=True,
                forcerm=True,
            )
        return image_tag
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

    def provision_vps(self, order, ram, cpu, service_id=None):
        os_key = order["data"].get("os", "debian-12")
        if os_key not in VPS_OS_OPTIONS:
            raise RuntimeError("Unsupported VPS operating system.")
        self.check_capacity(ram, cpu)
        image_tag = self.build_vps_image(os_key)
        self.check_capacity(ram, cpu)
        sid = service_id or uuid.uuid4().hex[:8]
        name, net = f"neonhost-vps-{sid}", f"neonhost-vps-net-{sid}"
        network = self.client.networks.create(
            net, driver="bridge", labels={"neonhost.managed": "true", "neonhost.service": sid}
        )
        container = None
        started_at = int(time.time())
        try:
            container = self.client.containers.run(
                image_tag, name=name, detach=True,
                command=["sshx"], tty=True, stdin_open=True, network=net,
                mem_limit=f"{ram}m", memswap_limit=f"{ram}m",
                nano_cpus=int(cpu * 1_000_000_000), pids_limit=256,
                cap_drop=["ALL"], security_opt=["no-new-privileges:true"],
                restart_policy={"Name": "unless-stopped"}, init=True,
                labels={
                    "neonhost.managed": "true", "neonhost.kind": "vps-sshx",
                    "neonhost.service": sid, "neonhost.os": os_key,
                },
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
        return {
            "id": sid, "user_id": order["user_id"], "order_id": order["id"],
            "kind": "sshx-vps-container", "name": VPS_OS_OPTIONS[os_key]["label"],
            "status": "running", "container": container.name, "network": net,
            "ram_mb": ram, "cpu": cpu, "host_port": None, "app_port": None,
            "url": link, "domain": "", "repo": "", "image": image_tag, "os": os_key,
        }

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
        container.reload()
        if running:
            if container.status == "running":
                return None
            started_at = int(time.time())
            container.start()
            if service["kind"] == "sshx-vps-container":
                return self.wait_for_sshx_link(container, since=started_at)
            return None
        if container.status == "running":
            container.stop(timeout=10)
        return None

    def generate_sshx_link(self, service):
        if service["kind"] != "sshx-vps-container":
            raise RuntimeError("This service does not use sshx access.")
        container = self.client.containers.get(service["container"])
        container.reload()
        if container.status == "running":
            container.stop(timeout=10)
        started_at = int(time.time())
        container.start()
        return self.wait_for_sshx_link(container, since=started_at)

    def reinstall_vps(self, service):
        if service["kind"] != "sshx-vps-container":
            raise RuntimeError("Only sshx VPS containers can be reinstalled.")
        os_key = service.get("os", "debian-12")
        self.remove(service)
        order = {
            "id": service["order_id"],
            "user_id": service["user_id"],
            "data": {"os": os_key},
        }
        return self.provision_vps(
            order, service["ram_mb"], service["cpu"], service_id=service["id"]
        )
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
        embed.add_field(name="Operating system", value=VPS_OS_OPTIONS.get(data.get("os", "debian-12"), VPS_OS_OPTIONS["debian-12"])["label"], inline=False)
        embed.add_field(name="Access method", value="A private sshx browser-terminal link will be generated after approval.", inline=False)
    else:
        embed.add_field(name="GitHub repository", value=data["repo"], inline=False)
        embed.add_field(name="Internal port", value=str(data["port"]), inline=True)
        embed.add_field(name="Site slug", value=data["slug"], inline=True)
    embed.set_footer(text="Review the request and repository before using /approve.")
    return embed

class HostingPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)

    @discord.ui.button(label="VPS Hosting", style=discord.ButtonStyle.primary, emoji="🖥️", row=0)
    async def vps_button(self, interaction, button):
        await interaction.response.send_message(
            "Use /vps to choose Ubuntu, Debian or Alpine, then select RAM and CPU. "
            "Your sshx terminal link is sent privately after admin approval.",
            ephemeral=True,
        )

    @discord.ui.button(label="Deploy Website", style=discord.ButtonStyle.success, emoji="🌐", row=0)
    async def web_button(self, interaction, button):
        await interaction.response.send_modal(WebsiteDeployModal())

    @discord.ui.button(label="My VPS", style=discord.ButtonStyle.secondary, emoji="🖥️", row=1)
    async def my_vps_button(self, interaction, button):
        await interaction.response.send_message(
            "Run /myvps to open your VPS details and its Start, Stop, Reinstall and SSH controls.",
            ephemeral=True,
        )

    @discord.ui.button(label="My Websites", style=discord.ButtonStyle.secondary, emoji="📂", row=1)
    async def my_websites_button(self, interaction, button):
        await show_mywebsites(interaction)


class VPSControlView(discord.ui.View):
    def __init__(self, owner_id: int, service_id: str):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.service_id = service_id
        actions = [
            ("Start", discord.ButtonStyle.success, "start"),
            ("Stop", discord.ButtonStyle.secondary, "stop"),
            ("Reinstall", discord.ButtonStyle.danger, "reinstall"),
            ("SSH / New Link", discord.ButtonStyle.primary, "ssh"),
        ]
        for label, style, action in actions:
            button = discord.ui.Button(label=label, style=style)

            async def callback(interaction: discord.Interaction, selected_action=action):
                await vps_control_action(interaction, self.service_id, selected_action)

            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id and interaction.user.id not in ADMIN_IDS:
            await interaction.response.send_message("You do not own this VPS.", ephemeral=True)
            return False
        return True


class ConfirmReinstallView(discord.ui.View):
    def __init__(self, owner_id: int, service_id: str):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.service_id = service_id

        confirm = discord.ui.Button(label="Confirm reinstall", style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

        async def confirm_callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id and interaction.user.id not in ADMIN_IDS:
                await interaction.response.send_message("You do not own this VPS.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            service = DB.service(self.service_id)
            if not service or (service["user_id"] != self.owner_id and interaction.user.id not in ADMIN_IDS):
                await interaction.followup.send("VPS not found or access denied.", ephemeral=True)
                return
            try:
                replacement = await asyncio.to_thread(HOST.reinstall_vps, service)
                DB.update_service(replacement)
                await interaction.followup.send(
                    f"VPS reinstalled successfully. OS: {VPS_OS_OPTIONS[replacement['os']]['label']}\n"
                    f"RAM: {replacement['ram_mb']} MB • CPU quota: {replacement['cpu']}\n"
                    f"New sshx link: {replacement['url']}\n"
                    "Keep this link private; anyone who has it can use the terminal.",
                    ephemeral=True,
                )
                try:
                    customer = await bot.fetch_user(replacement["user_id"])
                    await customer.send(
                        f"Your VPS {replacement['id']} was reinstalled.\n"
                        f"OS: {VPS_OS_OPTIONS[replacement['os']]['label']}\n"
                        f"RAM: {replacement['ram_mb']} MB • CPU quota: {replacement['cpu']}\n"
                        f"New sshx link: {replacement['url']}\n"
                        "The old container filesystem was deleted. Keep the new link private."
                    )
                except discord.DiscordException:
                    log.info("Could not DM new sshx link after reinstall for %s", replacement["id"])
            except Exception as exc:
                DB.set_service_status(self.service_id, "error")
                await interaction.followup.send(
                    "Reinstall failed. The previous container may already have been removed. "
                    + safe_error(str(exc)),
                    ephemeral=True,
                )

        async def cancel_callback(interaction: discord.Interaction):
            await interaction.response.edit_message(content="Reinstall cancelled.", view=None)

        confirm.callback = confirm_callback
        cancel.callback = cancel_callback
        self.add_item(confirm)
        self.add_item(cancel)


async def vps_control_action(interaction: discord.Interaction, service_id: str, action: str):
    service = DB.service(service_id)
    if not service or service["kind"] != "sshx-vps-container":
        await interaction.response.send_message("VPS not found.", ephemeral=True)
        return
    if service["user_id"] != interaction.user.id and interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("You do not own this VPS.", ephemeral=True)
        return
    if action == "reinstall":
        await interaction.response.send_message(
            "Reinstalling permanently deletes everything inside this VPS container, then creates a fresh OS image. "
            "This cannot be undone. Continue?",
            view=ConfirmReinstallView(service["user_id"], service_id),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if action == "stop":
            await asyncio.to_thread(HOST.set_running, service, False)
            DB.set_service_status(service_id, "stopped")
            await interaction.followup.send(
                f"VPS {service_id} stopped. Its current sshx session link is no longer active.",
                ephemeral=True,
            )
            return
        if action == "start":
            link = await asyncio.to_thread(HOST.set_running, service, True)
            DB.set_service_status(service_id, "running")
            if link:
                DB.update_service_url(service_id, link)
                await interaction.followup.send(
                    f"VPS {service_id} started. New sshx link:\n{link}",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"VPS {service_id} is already running. Use SSH / New Link to rotate the session link.",
                    ephemeral=True,
                )
            return
        if action == "ssh":
            link = await asyncio.to_thread(HOST.generate_sshx_link, service)
            DB.update_service_url(service_id, link)
            DB.set_service_status(service_id, "running")
            await interaction.followup.send(
                f"New sshx terminal link for VPS {service_id}:\n{link}\n"
                "The previous session was restarted. Anyone with this link can interact with the terminal; keep it private.",
                ephemeral=True,
            )
            return
        await interaction.followup.send("Unknown VPS action.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send("VPS action failed: " + safe_error(str(exc)), ephemeral=True)
        log.exception("VPS action %s failed for %s", action, service_id)


def website_embed(service: dict, status: str | None = None) -> discord.Embed:
    current_status = status or service["status"]
    embed = discord.Embed(
        title="My Website • " + service["name"],
        description=f"Service ID: {service['id']}",
        color=discord.Color.green() if current_status == "running" else discord.Color.orange(),
    )
    embed.add_field(name="Status", value=current_status, inline=True)
    embed.add_field(name="Website URL", value=service["url"], inline=False)
    embed.add_field(name="RAM cap", value=f"{service['ram_mb']} MB", inline=True)
    embed.add_field(name="CPU quota", value=str(service["cpu"]), inline=True)
    embed.add_field(name="App port", value=str(service["app_port"]), inline=True)
    embed.add_field(name="GitHub repository", value=service["repo"], inline=False)
    embed.add_field(name="Created", value=service["created"][:19].replace("T", " ") + " UTC", inline=True)
    return embed


async def actual_container_status(service: dict) -> str:
    try:
        container = await asyncio.to_thread(HOST.client.containers.get, service["container"])
        await asyncio.to_thread(container.reload)
        return container.status
    except docker.errors.NotFound:
        return "missing"
    except Exception as exc:
        return "unknown (" + safe_error(str(exc), 100) + ")"


class WebsiteControlView(discord.ui.View):
    def __init__(self, owner_id: int, service_id: str):
        super().__init__(timeout=900)
        self.owner_id = owner_id
        self.service_id = service_id

        actions = [
            ("Start", discord.ButtonStyle.success, "start"),
            ("Stop", discord.ButtonStyle.secondary, "stop"),
            ("Redeploy", discord.ButtonStyle.primary, "redeploy"),
            ("Delete", discord.ButtonStyle.danger, "delete"),
        ]
        for label, style, action in actions:
            button = discord.ui.Button(label=label, style=style)

            async def callback(interaction: discord.Interaction, selected_action=action):
                await website_control_action(interaction, self.service_id, selected_action)

            button.callback = callback
            self.add_item(button)

        service = DB.service(service_id)
        if service and service["url"].startswith(("https://", "http://")):
            self.add_item(discord.ui.Button(label="Open Website", style=discord.ButtonStyle.link, url=service["url"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id and interaction.user.id not in ADMIN_IDS:
            await interaction.response.send_message("You do not own this website.", ephemeral=True)
            return False
        return True


class MyWebsitesSelect(discord.ui.Select):
    def __init__(self, owner_id: int, items: list[dict]):
        self.owner_id = owner_id
        options = []
        for item in items[:25]:
            description = f"{item['status']} • {item['ram_mb']} MB RAM • {item['cpu']} CPU"
            options.append(discord.SelectOption(
                label=(item["name"] + " • " + item["id"])[:100],
                value=item["id"],
                description=description[:100],
            ))
        super().__init__(
            placeholder="Choose a website to manage",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.owner_id and interaction.user.id not in ADMIN_IDS:
            await interaction.response.send_message("You do not own these websites.", ephemeral=True)
            return
        service = DB.service(self.values[0])
        if not service or service["kind"] != "web-app":
            await interaction.response.send_message("Website not found.", ephemeral=True)
            return
        status = await actual_container_status(service)
        await interaction.response.edit_message(
            content=None,
            embed=website_embed(service, status),
            view=WebsiteControlView(service["user_id"], service["id"]),
        )


class MyWebsitesView(discord.ui.View):
    def __init__(self, owner_id: int, items: list[dict]):
        super().__init__(timeout=900)
        self.add_item(MyWebsitesSelect(owner_id, items))


class ConfirmDeleteWebsiteView(discord.ui.View):
    def __init__(self, owner_id: int, service_id: str):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.service_id = service_id

        confirm = discord.ui.Button(label="Confirm delete", style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

        async def confirm_callback(interaction: discord.Interaction):
            if interaction.user.id != self.owner_id and interaction.user.id not in ADMIN_IDS:
                await interaction.response.send_message("You do not own this website.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            service = DB.service(self.service_id)
            if not service or (service["user_id"] != self.owner_id and interaction.user.id not in ADMIN_IDS):
                await interaction.followup.send("Website not found or access denied.", ephemeral=True)
                return
            try:
                await asyncio.to_thread(HOST.remove, service)
                DB.delete_service(self.service_id)
                await interaction.followup.send(
                    f"Website {service['name']} was deleted. Its container filesystem and deployment image were removed.",
                    ephemeral=True,
                )
            except Exception as exc:
                await interaction.followup.send("Delete failed: " + safe_error(str(exc)), ephemeral=True)

        async def cancel_callback(interaction: discord.Interaction):
            await interaction.response.edit_message(content="Delete cancelled.", view=None)

        confirm.callback = confirm_callback
        cancel.callback = cancel_callback
        self.add_item(confirm)
        self.add_item(cancel)


async def website_control_action(interaction: discord.Interaction, service_id: str, action: str):
    service = DB.service(service_id)
    if not service or service["kind"] != "web-app":
        await interaction.response.send_message("Website not found.", ephemeral=True)
        return
    if service["user_id"] != interaction.user.id and interaction.user.id not in ADMIN_IDS:
        await interaction.response.send_message("You do not own this website.", ephemeral=True)
        return

    if action == "delete":
        await interaction.response.send_message(
            "Delete this website? Its running container and deployment image will be removed. "
            "Make sure you have a copy of your source and customer data.",
            view=ConfirmDeleteWebsiteView(service["user_id"], service_id),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if action in {"start", "stop"}:
            running = action == "start"
            await asyncio.to_thread(HOST.set_running, service, running)
            DB.set_service_status(service_id, "running" if running else "stopped")
            status = await actual_container_status(DB.service(service_id))
            updated = DB.service(service_id)
            await interaction.followup.send(
                embed=website_embed(updated, status),
                view=WebsiteControlView(updated["user_id"], updated["id"]),
                ephemeral=True,
            )
            return

        if action == "redeploy":
            order = {
                "id": service["order_id"],
                "user_id": service["user_id"],
                "data": {
                    "repo": service["repo"],
                    "slug": service["name"],
                    "port": service["app_port"],
                },
            }
            replacement = await asyncio.to_thread(
                HOST.provision_web, order, service["ram_mb"], service["cpu"]
            )
            try:
                await asyncio.to_thread(HOST.remove, service)
            except Exception:
                await asyncio.to_thread(HOST.remove, replacement)
                raise
            DB.delete_service(service_id)
            DB.add_service(replacement)
            await interaction.followup.send(
                "Website redeployed successfully.",
                embed=website_embed(replacement, "running"),
                view=WebsiteControlView(replacement["user_id"], replacement["id"]),
                ephemeral=True,
            )
            return

        await interaction.followup.send("Unknown website action.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send("Website action failed: " + safe_error(str(exc)), ephemeral=True)
        log.exception("Website action %s failed for %s", action, service_id)


async def show_mywebsites(interaction: discord.Interaction, service_id: str | None = None):
    items = [item for item in DB.services(interaction.user.id) if item["kind"] == "web-app"]
    if service_id:
        selected = DB.service(service_id)
        if not selected or selected["kind"] != "web-app" or (
            selected["user_id"] != interaction.user.id and interaction.user.id not in ADMIN_IDS
        ):
            await interaction.response.send_message("Website not found or you do not own it.", ephemeral=True)
            return
        status = await actual_container_status(selected)
        await interaction.response.send_message(
            embed=website_embed(selected, status),
            view=WebsiteControlView(selected["user_id"], selected["id"]),
            ephemeral=True,
        )
        return

    if not items:
        await interaction.response.send_message(
            "You do not have a website yet. Press **Deploy Website** on /start or run /web.",
            ephemeral=True,
        )
        return

    if len(items) == 1:
        selected = items[0]
        status = await actual_container_status(selected)
        await interaction.response.send_message(
            embed=website_embed(selected, status),
            view=WebsiteControlView(selected["user_id"], selected["id"]),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "Choose the website you want to manage:",
        view=MyWebsitesView(interaction.user.id, items),
        ephemeral=True,
    )


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
        description="Deploy a website from GitHub or request an OS-selected VPS with an sshx browser terminal. "
                    "New services are reviewed by an administrator before provisioning.",
        color=discord.Color.blurple())
    embed.add_field(name="Web hosting", value="GitHub → Docker build → live URL", inline=True)
    embed.add_field(name="VPS hosting", value="OS of choice + sshx terminal with enforced RAM and CPU caps", inline=True)
    embed.set_footer(text="Container VPS is not a dedicated KVM virtual machine.")
    await interaction.followup.send(embed=embed, view=HostingPanel())

@bot.tree.command(name="help", description="Show hosting commands")
async def help_command(interaction: discord.Interaction):
    # Acknowledge immediately so the interaction token does not expire before the reply.
    await interaction.response.defer(ephemeral=True, thinking=False)
    text = (
        "/start — open the panel\n/plans — show configured limits\n"
        "/vps — request an sshx browser VPS\n/web — request GitHub website deployment\n"
        "/my — list your services\n/myvps — show VPS info and controls\n"
        "/ssh — generate a new sshx terminal link\n/reinstall — reinstall your VPS\n"
        "/stop and /resume — stop or start a service\n/redeploy — rebuild your website from GitHub"
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
    embed.add_field(
        name="Available VPS operating systems",
        value="Ubuntu 24.04 LTS\nDebian 12 Bookworm\nAlpine Linux 3.22",
        inline=False,
    )
    embed.add_field(name="Web hosting", value=f"Default: {DEFAULT_WEB_RAM} MB RAM / {DEFAULT_WEB_CPU} CPU quota per app", inline=False)
    embed.add_field(name="Read carefully", value="CPU values are limits, not dedicated cores. sshx links are live shared terminal sessions; anyone with a link can interact. Disk quotas are not enabled in this version.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="vps", description="Request a VPS with an sshx browser-terminal link")
@app_commands.describe(
    ram_mb="RAM requested in MB; admin approves the actual allocation",
    cpu_quota="CPU quota requested; it is not a dedicated core count",
    os_choice="Linux distribution to install in your VPS container",
)
@app_commands.choices(os_choice=[
    app_commands.Choice(name="Ubuntu 24.04 LTS", value="ubuntu-24.04"),
    app_commands.Choice(name="Debian 12 Bookworm", value="debian-12"),
    app_commands.Choice(name="Alpine Linux 3.22", value="alpine-3.22"),
])
async def vps(
    interaction: discord.Interaction,
    ram_mb: app_commands.Range[int, 128, 8192],
    cpu_quota: app_commands.Range[float, 0.1, 4.0],
    os_choice: app_commands.Choice[str],
):
    if ram_mb > MAX_RAM or cpu_quota > MAX_CPU:
        await interaction.response.send_message(
            f"Configured maximum is {MAX_RAM} MB RAM and {MAX_CPU} CPU quota.", ephemeral=True)
        return
    data = {"ram_mb": int(ram_mb), "cpu": float(cpu_quota), "os": os_choice.value}
    oid = DB.new_order(interaction.user.id, str(interaction.user), "vps", data)
    await interaction.response.send_message(
        f"VPS request submitted. Order ID: {oid}. OS: {VPS_OS_OPTIONS[os_choice.value]['label']}. "
        "An admin must approve it. Your sshx browser-terminal link will be sent by DM after provisioning.",
        ephemeral=True)
    await notify_admin(request_embed(oid, interaction.user, "vps", data))

class WebsiteDeployModal(discord.ui.Modal, title="Deploy a website"):
    repo = discord.ui.TextInput(
        label="Public GitHub repository",
        placeholder="https://github.com/username/project",
        max_length=250,
    )
    app_port = discord.ui.TextInput(
        label="Application port",
        placeholder="3000 (Node/Next), 8080, or 80",
        max_length=5,
    )
    site_slug = discord.ui.TextInput(
        label="Website name",
        placeholder="my-portfolio",
        max_length=40,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            data = {
                "repo": check_repo(self.repo.value),
                "port": check_app_port(int(self.app_port.value.strip())),
                "slug": check_slug(self.site_slug.value),
            }
        except (ValueError, TypeError) as exc:
            await interaction.response.send_message(
                "Please check the repository URL, app port and website name. " + str(exc),
                ephemeral=True,
            )
            return
        oid = DB.new_order(interaction.user.id, str(interaction.user), "web", data)
        await interaction.response.send_message(
            f"Website request submitted! Order ID: {oid}. "
            "An admin will review your GitHub repository and approve the deployment.",
            ephemeral=True,
        )
        await notify_admin(request_embed(oid, interaction.user, "web", data))


@bot.tree.command(name="web", description="Open the simple GitHub website deployment form")
async def web(interaction: discord.Interaction):
    await interaction.response.send_modal(WebsiteDeployModal())


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

@bot.tree.command(name="myvps", description="Show your VPS OS, RAM, CPU, status and controls")
@app_commands.describe(service_id="Optional VPS service ID, needed only if you have more than one VPS")
async def myvps(interaction: discord.Interaction, service_id: str | None = None):
    items = [
        item for item in DB.services(interaction.user.id)
        if item["kind"] == "sshx-vps-container"
    ]
    if service_id:
        selected = DB.service(service_id)
        if not selected or selected["kind"] != "sshx-vps-container" or selected["user_id"] != interaction.user.id:
            await interaction.response.send_message("VPS not found or you do not own it.", ephemeral=True)
            return
        items = [selected]
    if not items:
        await interaction.response.send_message(
            "You do not have a VPS yet. Use /vps to request one.", ephemeral=True)
        return
    if len(items) > 1 and not service_id:
        lines = []
        for item in items[:20]:
            os_name = VPS_OS_OPTIONS.get(item.get("os", "debian-12"), VPS_OS_OPTIONS["debian-12"])["label"]
            lines.append(
                f"ID: {item['id']} • {os_name}\n"
                f"RAM: {item['ram_mb']} MB • CPU quota: {item['cpu']} • Status: {item['status']}"
            )
        await interaction.response.send_message(
            "Your VPS instances:\n\n" + "\n\n".join(lines) + "\n\n"
            "Run /myvps again with a service_id to open its control panel.",
            ephemeral=True)
        return

    item = items[0]
    memory_usage = "Not available (container stopped)"
    cpu_usage = "Not available (container stopped)"
    container_short_id = "unknown"
    try:
        container = await asyncio.to_thread(HOST.client.containers.get, item["container"])
        await asyncio.to_thread(container.reload)
        actual_status = container.status
        container_short_id = container.short_id
        if actual_status == "running":
            stats = await asyncio.to_thread(container.stats, stream=False)
            memory_stats = stats.get("memory_stats", {})
            memory_bytes = int(memory_stats.get("usage", 0))
            memory_cache = int(memory_stats.get("stats", {}).get("inactive_file", 0))
            memory_used_mb = max(0, memory_bytes - memory_cache) / 1024 / 1024
            memory_usage = f"{memory_used_mb:.1f} MB used / {item['ram_mb']} MB cap"

            cpu_stats = stats.get("cpu_stats", {})
            previous_cpu = stats.get("precpu_stats", {})
            cpu_delta = (
                cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
                - previous_cpu.get("cpu_usage", {}).get("total_usage", 0)
            )
            system_delta = (
                cpu_stats.get("system_cpu_usage", 0)
                - previous_cpu.get("system_cpu_usage", 0)
            )
            cpu_count = cpu_stats.get("online_cpus") or len(cpu_stats.get("cpu_usage", {}).get("percpu_usage", [])) or 1
            if system_delta > 0 and cpu_delta >= 0:
                cpu_usage = f"{(cpu_delta / system_delta) * cpu_count * 100:.1f}% at sample time"
            else:
                cpu_usage = "Collecting first CPU sample"
    except docker.errors.NotFound:
        actual_status = "missing"
    except Exception as exc:
        actual_status = "unknown (" + safe_error(str(exc), 120) + ")"

    os_name = VPS_OS_OPTIONS.get(item.get("os", "debian-12"), VPS_OS_OPTIONS["debian-12"])["label"]
    embed = discord.Embed(
        title="My VPS • " + os_name,
        description=f"Instance ID: {item['id']} • Container: {container_short_id}",
        color=discord.Color.green() if actual_status == "running" else discord.Color.orange(),
    )
    embed.add_field(name="Operating system", value=os_name, inline=True)
    embed.add_field(name="Container status", value=actual_status, inline=True)
    embed.add_field(name="RAM allocation", value=f"{item['ram_mb']} MB cap", inline=True)
    embed.add_field(name="RAM usage", value=memory_usage, inline=True)
    embed.add_field(name="CPU allocation", value=f"{item['cpu']} CPU quota", inline=True)
    embed.add_field(name="CPU usage", value=cpu_usage, inline=True)
    embed.add_field(name="Created", value=item["created"][:19].replace("T", " ") + " UTC", inline=True)
    embed.add_field(name="Access", value="Use SSH / New Link to generate a fresh sshx browser-terminal URL.", inline=False)
    embed.add_field(name="Storage", value="Per-customer disk quota is not configured; reinstall deletes the container filesystem.", inline=False)
    await interaction.response.send_message(
        embed=embed, view=VPSControlView(item["user_id"], item["id"]), ephemeral=True)


@bot.tree.command(name="ssh", description="Generate a fresh sshx browser-terminal link for your VPS")
async def ssh(interaction: discord.Interaction, service_id: str):
    await vps_control_action(interaction, service_id, "ssh")


@bot.tree.command(name="reinstall", description="Reinstall your VPS OS (deletes its container files)")
async def reinstall(interaction: discord.Interaction, service_id: str):
    await vps_control_action(interaction, service_id, "reinstall")


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
            if service["kind"] == "sshx-vps-container":
                message = (
                    f"Your NeonHost VPS is ready.\nService ID: {service['id']}\n"
                    f"OS: {VPS_OS_OPTIONS[service['os']]['label']}\n"
                    f"RAM cap: {ram} MB\nCPU quota: {cpu}\n"
                    f"sshx browser-terminal link: {service['url']}\n"
                    "Anyone with this link can interact with your terminal. Keep it private. "
                    "Use /myvps for your controls; reinstalling erases the container filesystem."
                )
            else:
                message = (
                    f"Your NeonHost website is ready.\nService ID: {service['id']}\n"
                    f"RAM cap: {ram} MB\nCPU quota: {cpu}\n"
                    f"Website URL: {service['url']}"
                )
            await customer.send(message)
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
