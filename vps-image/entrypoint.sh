#!/bin/sh
set -eu

# The bot starts sshx as the container's only public access path.
# Do not expose the host shell: each container has its own Docker resource limits.
if [ "$#" -eq 0 ]; then
  set -- /bin/bash -l
fi

exec "$@"
