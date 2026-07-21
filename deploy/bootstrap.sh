#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu server (Hetzner). Installs Docker, fetches
# the app, and starts it. Safe to re-run — it skips whatever is already done.
#
#   curl -fsSL https://raw.githubusercontent.com/imtiazai004/Soft-clipper-web-app/main/deploy/bootstrap.sh | bash
#
# After it finishes, edit /opt/soft-clipper/deploy/.env and run:
#   cd /opt/soft-clipper/deploy && docker compose up -d --build
set -euo pipefail

REPO="https://github.com/imtiazai004/Soft-clipper-web-app.git"
DIR="/opt/soft-clipper"

echo "==> Installing Docker (if needed)"
if ! command -v docker >/dev/null 2>&1; then
	curl -fsSL https://get.docker.com | sh
fi

echo "==> Fetching the app"
if [ -d "$DIR/.git" ]; then
	git -C "$DIR" pull --ff-only
else
	git clone "$REPO" "$DIR"
fi

echo "==> Preparing the environment file"
cd "$DIR/deploy"
if [ ! -f .env ]; then
	cp .env.example .env
	# give SESSION_SECRET a real value so the first start doesn't need editing
	secret="$(openssl rand -hex 32)"
	sed -i "s/^SESSION_SECRET=.*/SESSION_SECRET=$secret/" .env
	echo
	echo "    A default .env was created. Edit it before going live:"
	echo "      nano $DIR/deploy/.env"
	echo "    Set APP_USERS (real passwords) and DOMAIN, then:"
	echo "      cd $DIR/deploy && docker compose up -d --build"
	echo
	exit 0
fi

echo "==> Building and starting"
docker compose up -d --build
echo "==> Done. Logs:  docker compose -f $DIR/deploy/docker-compose.yml logs -f"
