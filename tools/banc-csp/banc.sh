#!/bin/sh
# Banc CSP : lance une sonde CDP dans un Edge headless à profil neuf, une fois
# par page visée. Profil neuf = localStorage vide, donc appairage réel par
# /pair (le cas d'un premier lancement, celui qui a révélé #73).
#
#   sh tools/banc-csp/banc.sh                     # sonde_connect, 127.0.0.1 puis localhost
#   sh tools/banc-csp/banc.sh csp http://localhost:5173/
#
# Premier argument : la sonde (connect | csp), défaut connect. Arguments
# suivants : les URL de page, défaut le build servi par le backend sur les
# deux hôtes. Edge surchargeable par $EDGE.
S=$(dirname "$0")
sonde=${1:-connect}
[ $# -gt 0 ] && shift
[ $# -eq 0 ] && set -- http://127.0.0.1:8000/ http://localhost:8000/
EDGE=${EDGE:-"/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"}
port=9340
for page in "$@"; do
  port=$((port + 1))
  prof=$(mktemp -d)
  "$EDGE" --headless=new --remote-debugging-port=$port --user-data-dir="$(cygpath -w "$prof")" \
    --no-first-run about:blank >/dev/null 2>&1 &
  node "$S/sonde_$sonde.mjs" "$page" $port
  # Seuls les Edge de CE profil : jamais le navigateur de l'utilisateur.
  powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | Where-Object { \$_.CommandLine -like '*$(basename "$prof")*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force -ErrorAction SilentlyContinue }"
  rm -rf "$prof" 2>/dev/null
  echo ----
done
