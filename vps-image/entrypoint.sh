#!/bin/sh
set -eu
if [ -z "${SSH_PUBLIC_KEY:-}" ]; then
  echo "SSH_PUBLIC_KEY is required; refusing to start without key-only access." >&2
  exit 1
fi
printf '%s\n' "$SSH_PUBLIC_KEY" > /root/.ssh/authorized_keys
chmod 0700 /root/.ssh
chmod 0600 /root/.ssh/authorized_keys
mkdir -p /run/sshd
[ -f /etc/ssh/ssh_host_ed25519_key ] || ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/ssh_host_ed25519_key
[ -f /etc/ssh/ssh_host_rsa_key ] || ssh-keygen -q -t rsa -b 3072 -N '' -f /etc/ssh/ssh_host_rsa_key
chmod 0600 /etc/ssh/ssh_host_*_key
cat > /etc/ssh/sshd_config <<'SSHD'
Port 22
AddressFamily any
ListenAddress 0.0.0.0
HostKey /etc/ssh/ssh_host_ed25519_key
HostKey /etc/ssh/ssh_host_rsa_key
PermitRootLogin prohibit-password
PubkeyAuthentication yes
AuthenticationMethods publickey
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
UsePAM no
PermitEmptyPasswords no
X11Forwarding no
AllowTcpForwarding no
AllowAgentForwarding no
PermitTunnel no
GatewayPorts no
PermitUserEnvironment no
PrintMotd no
Subsystem sftp /usr/lib/openssh/sftp-server
LogLevel VERBOSE
SSHD
exec /usr/sbin/sshd -D -e
