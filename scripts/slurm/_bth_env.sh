# Source from SLURM scripts: source scripts/slurm/_bth_env.sh
# Sets BTH_PROJECT_SLUG, BTH_PROJECT_ROOT, and BTH_CATALOG_DIR so bth run works transparently in batch jobs.
set -euo pipefail
export BTH_PROJECT_SLUG="test_smoke"
export BTH_PROJECT_ROOT="/home/marielle/projects/bathos"
export BTH_CATALOG_DIR="/home/marielle/.bth/catalog"
