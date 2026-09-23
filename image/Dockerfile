FROM debian:12-slim

# apt runs unattended here (no TTY, no human to answer prompts): default to assume-yes
# + the noninteractive frontend, so a model's `apt-get install X` completes instead of
# aborting at "Do you want to continue? [Y/n]".
ENV DEBIAN_FRONTEND=noninteractive
RUN printf 'APT::Get::Assume-Yes "true";\n' > /etc/apt/apt.conf.d/90assume-yes

# A working toolbox: explore, write, fetch, script, compile, install packages, and do
# Node/JS work with a REAL yarn (Debian's `yarn` package is the unrelated cmdtest tool,
# so we install the actual one globally via npm).
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils util-linux procps \
        curl wget ca-certificates \
        python3 python3-pip python3-dev \
        build-essential git \
        nodejs npm \
        sudo \
        nano vim-tiny less \
        file findutils grep sed gawk \
        iputils-ping dnsutils netcat-openbsd \
    && rm -rf /var/lib/apt/lists/* \
    && ([ -e /usr/bin/pip ] || ln -s /usr/bin/pip3 /usr/bin/pip) \
    && npm install -g yarn

# Match the host user's uid so the bind-mounted home is writable.
# Build with:  docker build --build-arg UID=$(id -u) -t freetime-sandbox:latest image/
ARG UID=1000
RUN useradd -m -u ${UID} -s /bin/bash agent

# Limited passwordless sudo: agent may run ONLY package management as root
# (so `sudo apt-get install X` works, but `sudo bash` / `sudo cp` are refused).
RUN printf 'agent ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/apt, /usr/bin/dpkg\n' \
      > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent

USER agent
# Make plain `pip install X` work as the non-root user despite Debian 12's PEP-668
# lock: installs land in the writable ~/.local, and ~/.local/bin is on PATH.
ENV HOME=/home/agent \
    PIP_USER=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PATH=/home/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
WORKDIR /home/agent

# PID 1 just idles; the host broker drives everything via `docker exec`.
CMD ["sleep", "infinity"]
