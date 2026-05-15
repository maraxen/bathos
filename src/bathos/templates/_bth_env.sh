# Source from SLURM scripts: source scripts/slurm/_bth_env.sh
# Sets BTH_PROJECT_SLUG, BTH_PROJECT_ROOT, and BTH_CATALOG_DIR so bth run works transparently in batch jobs.
set -euo pipefail
export BTH_PROJECT_SLUG="{slug}"
export BTH_PROJECT_ROOT="{root}"
export BTH_CATALOG_DIR="{catalog_dir}"
