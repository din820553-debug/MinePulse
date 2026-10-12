#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
if [ ! -f .env ]; then
    cp .env.example .env
    echo 'Fill BOT_TOKEN in .env and run this script again.'
    exit 1
fi
exec python3 -m minepulse
