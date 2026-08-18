#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly DOCKERFILE="${REPO_ROOT}/webapp/hosted/runsc/Dockerfile"
readonly PINS="${REPO_ROOT}/deploy/pins.json"
readonly OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/deploy/images/out}"
readonly IMAGE="${IMAGE:-se-skills/sandbox}"
readonly TAG="${TAG:-pinned}"

fail() {
  printf 'sandbox image build failed: %s\n' "$1" >&2
  exit 1
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || fail "required tool '$1' is missing; install it before building"
}

require_tool docker
require_tool syft
if command -v grype >/dev/null 2>&1; then
  SCANNER=grype
elif command -v trivy >/dev/null 2>&1; then
  SCANNER=trivy
else
  fail "required vulnerability scanner 'grype' or 'trivy' is missing; install one before building"
fi

python3 - "$PINS" "$DOCKERFILE" <<'PY'
import json
import re
import sys
from pathlib import Path

pins = json.loads(Path(sys.argv[1]).read_text())
dockerfile = Path(sys.argv[2]).read_text()
for digest in pins["sandbox_image"]["base_digests"].values():
    if not re.search(re.escape(digest), dockerfile):
        raise SystemExit("sandbox base image digest does not match deploy/pins.json")
PY

mkdir -p "$OUTPUT_DIR"
work_dir="$(mktemp -d "${OUTPUT_DIR}/.build.XXXXXX")"
container_id=""
cleanup() {
  if [[ -n "$container_id" ]]; then
    docker rm "$container_id" >/dev/null 2>&1 || true
  fi
  rm -rf "$work_dir"
}
trap cleanup EXIT

image_ref="${IMAGE}:${TAG}"
docker build --pull=false --file "$DOCKERFILE" --tag "$image_ref" "$REPO_ROOT" >/dev/null
image_id="$(docker image inspect --format '{{.Id}}' "$image_ref")"
[[ "$image_id" == sha256:* ]] || fail "docker did not return an immutable image digest"
image_digest="${image_id#sha256:}"

syft "$image_ref" --output cyclonedx-json >"${work_dir}/sbom.json"
if [[ "$SCANNER" == "grype" ]]; then
  grype "$image_ref" --fail-on high --quiet >/dev/null
else
  trivy image --exit-code 1 --severity HIGH,CRITICAL --quiet "$image_ref" >/dev/null
fi

if git -C "$REPO_ROOT" grep -nEI \
  '(^|[^A-Za-z0-9])(AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9]{20,})' \
  -- . ':!deploy/images/out' >/dev/null 2>&1; then
  fail "repository secret scan found a credential-shaped value"
fi

if [[ "${COSIGN_SIGN:-0}" == "1" ]]; then
  require_tool cosign
  cosign sign --yes "${IMAGE}@sha256:${image_digest}" >/dev/null
fi

container_id="$(docker create "$image_ref")"
rootfs_dir="${work_dir}/rootfs"
mkdir -p "$rootfs_dir"
docker export "$container_id" | tar --extract --directory "$rootfs_dir" --no-same-owner
rootfs_digest="$(
  tar --create --sort=name --mtime='UTC 1970-01-01' \
    --owner=0 --group=0 --numeric-owner --directory "$rootfs_dir" . |
    sha256sum | awk '{print $1}'
)"
manifest="${OUTPUT_DIR}/${image_digest}.manifest.json"
cp "${work_dir}/sbom.json" "${OUTPUT_DIR}/${image_digest}.sbom.json"
tar --create --sort=name --mtime='UTC 1970-01-01' \
  --owner=0 --group=0 --numeric-owner --directory "$rootfs_dir" . |
  gzip -n >"${OUTPUT_DIR}/${image_digest}.rootfs.tar.gz"
python3 - "$manifest" "$image_digest" "$rootfs_digest" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "image_digest": f"sha256:{sys.argv[2]}",
            "rootfs_digest": f"sha256:{sys.argv[3]}",
            "sbom": f"{sys.argv[2]}.sbom.json",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
)
PY
printf 'sandbox image digest: sha256:%s\nrootfs digest: sha256:%s\nmanifest: %s\n' \
  "$image_digest" "$rootfs_digest" "$manifest"
