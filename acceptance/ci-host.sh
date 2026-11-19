#!/usr/bin/env bash
# Provision a disposable loopback PeerTube destination strictly on a hosted CI runner.
set -euo pipefail
: "${GITHUB_ACTIONS:?CI only}" "${RUNNER_TEMP:?}" "${GITHUB_ENV:?}"
secret=$(openssl rand -hex 24)
echo "::add-mask::$secret"
origin=http://localhost:19000
if [[ "$RUNNER_OS" == Linux ]]; then
  python deploy/bootstrap.py "$RUNNER_TEMP/video-host" --port 19000 --rtmp-port 11935
  secret=$(sed -n 's/^OPERATOR_PASSWORD=//p' "$RUNNER_TEMP/video-host/.env")
  echo "::add-mask::$secret"
else
  brew install postgresql@17 redis ffmpeg > "$RUNNER_TEMP/brew.log" 2>&1 || { tail -40 "$RUNNER_TEMP/brew.log"; exit 1; }
  export PATH="$(brew --prefix postgresql@17)/bin:$PATH"
  initdb -D "$RUNNER_TEMP/pg" -A trust --no-locale -E UTF8 > /dev/null
  pg_ctl -D "$RUNNER_TEMP/pg" -l "$RUNNER_TEMP/pg.log" -o '-p 15432 -h 127.0.0.1' start -w
  export PGHOST=127.0.0.1 PGPORT=15432
  createuser -s peertube
  createdb -O peertube peertube
  redis-server --bind 127.0.0.1 --port 16379 --daemonize yes --dir "$RUNNER_TEMP"
  url=https://github.com/Chocobozzz/PeerTube/releases/download/v8.3.2/peertube-v8.3.2.tar.xz
  curl --retry 3 -fsSL "$url" -o "$RUNNER_TEMP/peer.tar.xz"
  echo "93103439e1c348cfdc6ce493843a9be4e2605c3a0607d2ecdac35ca88464ac13  $RUNNER_TEMP/peer.tar.xz" | shasum -a 256 -c -
  mkdir "$RUNNER_TEMP/peer"
  tar -xf "$RUNNER_TEMP/peer.tar.xz" --strip-components=1 -C "$RUNNER_TEMP/peer"
  cd "$RUNNER_TEMP/peer"
  npm install -g pnpm@10.15.1 > /dev/null
  NODE_ENV=production npm run install-node-dependencies -- --production > "$RUNNER_TEMP/peer-install.log" 2>&1 || { tail -30 "$RUNNER_TEMP/peer-install.log"; exit 1; }
  cat > config/production.yaml <<EOF
listen:
  hostname: '127.0.0.1'
  port: 19000
webserver:
  hostname: 'localhost'
  port: 19000
  https: false
database:
  name: peertube
  port: 15432
redis:
  port: 16379
secrets:
  peertube: '$(openssl rand -hex 32)'
transcoding:
  enabled: true
  threads: 2
EOF
  NODE_ENV=production PT_INITIAL_ROOT_PASSWORD="$secret" node dist/server > "$RUNNER_TEMP/peer.log" 2>&1 &
fi
for i in {1..180}; do
  if curl -fsS "$origin/api/v1/config" >/dev/null 2>&1; then
    echo "PEERTUBE_USERNAME=root" >> "$GITHUB_ENV"
    echo "PEERTUBE_PASSWORD=$secret" >> "$GITHUB_ENV"
    exit 0
  fi
  sleep 2
done
echo 'PeerTube did not start'
if [[ -f "$RUNNER_TEMP/peer.log" ]]; then tail -25 "$RUNNER_TEMP/peer.log" | sed -E 's/[a-f0-9]{40,}/[redacted]/g'; fi
exit 1
