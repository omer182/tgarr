#!/usr/bin/env bash
# Build & (re)start tgarr on the homeserver. Run from Git Bash: ./deploy.sh
set -euo pipefail
cd "$(dirname "$0")"
tar czf - Dockerfile requirements.txt app | ssh homeserver 'rm -rf ~/tgarr/src && mkdir -p ~/tgarr/src ~/tgarr/config && tar xzf - -C ~/tgarr/src'
ssh homeserver bash -s <<'EOF'
set -e
cd ~/tgarr
ENV=config/.env
touch $ENV && chmod 600 $ENV
key() { grep -oP "(?<=<ApiKey>)[^<]+" /hosted-apps/$1/config/config.xml; }
setv() { if grep -q "^$1=" $ENV; then sed -i "s|^$1=.*|$1=$2|" $ENV; else echo "$1=$2" >> $ENV; fi; }
setv SONARR_KEY "$(key sonarr)"
setv RADARR_KEY "$(key radarr)"
setv PLEX_TOKEN "$(grep -oP '(?<=PlexOnlineToken=")[^"]+' "/hosted-apps/plex/config/Library/Application Support/Plex Media Server/Preferences.xml")"
setv PLEX_URL http://172.18.0.1:32400
grep -q '^TG_API_ID=' $ENV || printf 'TG_API_ID=\nTG_API_HASH=\n' >> $ENV
docker build -q -t tgarr:latest src
docker rm -f tgarr >/dev/null 2>&1 || true
docker run -d --name tgarr --restart unless-stopped --network proxy -p 8765:8765 \
  --env-file $ENV -v $HOME/tgarr/config:/config -v /mnt/storage/data:/data tgarr:latest

# A restart makes Prowlarr and Sonarr see failed requests and mark the indexer "unavailable due to failures";
# searches then silently skip it. A passing test clears that, so re-test in both once tgarr answers again.
for i in $(seq 1 40); do curl -sf localhost:8765/api/status >/dev/null && break; sleep 1; done
retest() {  # container, port, api version
  k=$(key $1)
  id=$(docker exec $1 curl -s -H "X-Api-Key: $k" localhost:$2/api/$3/indexer | python3 -c "import sys,json;print(next((i['id'] for i in json.load(sys.stdin) if 'tgarr' in i['name'].lower()),''))")
  [ -z "$id" ] && return 0
  docker exec $1 curl -s -H "X-Api-Key: $k" localhost:$2/api/$3/indexer/$id > /tmp/tgarr_ix.json
  docker cp /tmp/tgarr_ix.json $1:/tmp/tgarr_ix.json >/dev/null
  echo "$1 indexer test: $(docker exec $1 curl -s -o /dev/null -w '%{http_code}' -X POST -H "X-Api-Key: $k" -H 'Content-Type: application/json' -d @/tmp/tgarr_ix.json localhost:$2/api/$3/indexer/test)"
  docker exec $1 rm -f /tmp/tgarr_ix.json; rm -f /tmp/tgarr_ix.json
}
retest prowlarr 9696 v1
retest sonarr 8989 v3
EOF
echo "tgarr running at http://homeserver.local:8765"
