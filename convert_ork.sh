#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
JSBSIM_REPO=${JSBSIM_REPO:-/home/ramirm9/jsbsim-rocket-hitl}

usage() {
  cat <<USAGE
Usage:
  ./convert_ork.sh <path-to-openrocket.ork> [aircraft_name] [--run] [--hil]

Examples:
  ./convert_ork.sh "/mnt/c/Users/ramirm9/Downloads/CURRENT_Subscale.ork"
  ./convert_ork.sh "/mnt/c/Users/ramirm9/Downloads/My Rocket.ork" my_rocket_convert --run
  JSBSIM_REPO=/home/ramirm9/jsbsim-rocket-hitl ./convert_ork.sh "/mnt/c/Users/ramirm9/Downloads/My Rocket.ork"

What it does:
  1. Converts the .ork file into JSBSim XML/C++ files.
  2. Finds/downloads the OpenRocket motor database for real thrust curves.
  3. Installs the generated aircraft and C++ files into JSBSim.
  4. Builds rocket_sim_<name> and hitl_sim_<name>.
  5. If --run is passed, runs the standalone sim.
  6. If --hil is passed with --run, runs the HIL sim instead.
USAGE
}

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

ORK_PATH=$1
shift

if [[ ! -f "$ORK_PATH" ]]; then
  echo "ERROR: OpenRocket file not found: $ORK_PATH" >&2
  exit 1
fi

RUN_AFTER=0
RUN_HIL=0
NAME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --run)
      RUN_AFTER=1
      ;;
    --hil)
      RUN_HIL=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      if [[ -z "$NAME" ]]; then
        NAME=$1
      else
        echo "ERROR: unexpected argument: $1" >&2
        usage
        exit 1
      fi
      ;;
  esac
  shift
done

if [[ -z "$NAME" ]]; then
  base=$(basename "$ORK_PATH")
  base=${base%.*}
  NAME=$(echo "${base}_convert" | tr "[:upper:]" "[:lower:]" | sed -E "s/[^a-z0-9]+/_/g; s/^_+|_+$//g; s/_+/_/g")
fi

if [[ ! -d "$JSBSIM_REPO" ]]; then
  echo "ERROR: JSBSim repo not found: $JSBSIM_REPO" >&2
  echo "Set JSBSIM_REPO=/path/to/jsbsim-rocket-hitl if yours is somewhere else." >&2
  exit 1
fi

echo "Converter: $SCRIPT_DIR/openrocket_to_jsbsim.py"
echo "OpenRocket file: $ORK_PATH"
echo "Aircraft name: $NAME"
echo "JSBSim repo: $JSBSIM_REPO"
echo

python3 "$SCRIPT_DIR/openrocket_to_jsbsim.py" \
  "$ORK_PATH" \
  --name "$NAME" \
  --install-jsbsim "$JSBSIM_REPO" \
  --build-jsbsim

echo
echo "Done. Generated package: $SCRIPT_DIR/open rocket convert/$NAME"
echo "Installed aircraft: $JSBSIM_REPO/aircraft/$NAME"
echo "Standalone executable: $JSBSIM_REPO/build/rocket_sim_$NAME"
echo "HIL executable: $JSBSIM_REPO/build/hitl_sim_$NAME"

if [[ $RUN_AFTER -eq 1 ]]; then
  cd "$JSBSIM_REPO"
  if [[ $RUN_HIL -eq 1 ]]; then
    echo
    echo "Running HIL sim..."
    "./build/hitl_sim_$NAME"
  else
    echo
    echo "Running standalone sim..."
    "./build/rocket_sim_$NAME"
    echo
    echo "CSV output should be in: $JSBSIM_REPO/data/${NAME}_trajectory.csv"
  fi
fi

