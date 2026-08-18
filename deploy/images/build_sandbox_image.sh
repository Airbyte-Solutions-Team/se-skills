#!/usr/bin/env bash
set -euo pipefail

readonly REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly DOCKERFILE="${REPO_ROOT}/webapp/hosted/runsc/Dockerfile"
readonly PINS="${REPO_ROOT}/deploy/pins.json"
readonly OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT}/deploy/images/out}"
readonly IMAGE="${IMAGE:-se-skills/sandbox}"
readonly TAG="${TAG:-pinned}"
readonly PUBLISH="${PUBLISH:-0}"

fail() {
  printf 'sandbox image build failed: %s\n' "$1" >&2
  exit 1
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || fail "required tool '$1' is missing; install it before building"
}

require_tool docker
require_tool syft
if [[ "${COSIGN_SIGN:-0}" == "1" ]]; then
  require_tool cosign
fi
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
image_config_id="$(docker image inspect --format '{{.Id}}' "$image_ref")"
[[ "$image_config_id" == sha256:* ]] || fail "docker did not return an immutable image config ID"

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

container_id="$(docker create "$image_ref")"
rootfs_dir="${work_dir}/rootfs"
mkdir -p "$rootfs_dir"
[[ "$(id -u)" == "0" ]] || fail "rootfs export requires root to preserve image file ownership"
docker export "$container_id" | tar --extract --directory "$rootfs_dir" --same-owner
rootfs_digest="$(
  PYTHONPATH="$REPO_ROOT" python3 -m webapp.hosted.rootfs_digest "$rootfs_dir"
)"
registry_digest=""
if [[ "$PUBLISH" == "1" ]]; then
  docker push "$image_ref" >/dev/null
  registry_digest="$(
    docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$image_ref" |
      grep -F "${IMAGE}@" | head -n 1 | cut -d@ -f2
  )"
  [[ "$registry_digest" == sha256:* ]] || fail "published image has no registry manifest digest"
  syft "${IMAGE}@${registry_digest}" --output cyclonedx-json >"${work_dir}/sbom.json"
  provenance="${work_dir}/provenance.json"
  python3 - "$provenance" "$IMAGE" "$registry_digest" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "_type": "https://in-toto.io/Statement/v1",
            "predicateType": "https://slsa.dev/provenance/v1",
            "subject": [{"name": sys.argv[2], "digest": {"sha256": sys.argv[3].split(":", 1)[1]}}],
            "predicate": {"buildType": "https://se-skills.dev/sandbox-image"},
        },
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"
)
PY
  if [[ "${COSIGN_SIGN:-0}" == "1" ]]; then
    require_tool cosign
    cosign sign --yes "${IMAGE}@${registry_digest}" >/dev/null
    cosign attest --yes --type cyclonedx --predicate "${work_dir}/sbom.json" "${IMAGE}@${registry_digest}" >/dev/null
    cosign attest --yes --type slsaprovenance --predicate "$provenance" "${IMAGE}@${registry_digest}" >/dev/null
  fi
else
  registry_digest="unpublished"
  syft "$image_ref" --output cyclonedx-json >"${work_dir}/sbom.json"
  provenance="${work_dir}/provenance.json"
  printf '{}\n' >"$provenance"
fi

python3 - "$work_dir/sbom.json" "$registry_digest" <<'PY'
import json
import sys
from pathlib import Path

sbom = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
digest = sys.argv[2]
if digest == "unpublished":
    raise SystemExit(0)
component = sbom.get("metadata", {}).get("component", {})
expected = digest.removeprefix("sha256:")
matched = False
hashes = component.get("hashes", [])
if isinstance(hashes, list):
    matched = any(
        isinstance(item, dict)
        and item.get("alg") == "SHA-256"
        and item.get("content") == expected
        for item in hashes
    )
if not matched:
    for field in ("version", "purl", "bom-ref"):
        value = component.get(field)
        if isinstance(value, str) and (
            expected in value or f"sha256:{expected}" in value
        ):
            matched = True
            break
if not matched:
    raise SystemExit("SBOM metadata.component does not cover the image digest")
PY

evidence_stem="${registry_digest#sha256:}"
[[ "$registry_digest" == "unpublished" ]] && evidence_stem="${image_config_id#sha256:}"
manifest="${OUTPUT_DIR}/${evidence_stem}.manifest.json"
sbom_path="${OUTPUT_DIR}/${evidence_stem}.sbom.json"
provenance_path="${OUTPUT_DIR}/${evidence_stem}.provenance.json"
cp "${work_dir}/sbom.json" "$sbom_path"
cp "$provenance" "$provenance_path"
tar --create --sort=name --mtime='UTC 1970-01-01' \
  --numeric-owner --directory "$rootfs_dir" . |
  gzip -n >"${OUTPUT_DIR}/${evidence_stem}.rootfs.tar.gz"
python3 - "$manifest" "$registry_digest" "$rootfs_digest" "$IMAGE" \
  "${CERTIFICATE_IDENTITY:-}" "${CERTIFICATE_OIDC_ISSUER:-}" "$evidence_stem" <<'PY'
import json
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(
    json.dumps(
        {
            "image_digest": sys.argv[2],
            "rootfs_digest": sys.argv[3],
            "sbom_name": f"{sys.argv[7]}.sbom.json",
            "provenance_name": f"{sys.argv[7]}.provenance.json",
            "signature": {
                "image_reference": (
                    f"{sys.argv[4]}@{sys.argv[2]}"
                    if sys.argv[2] != "unpublished"
                    else f"{sys.argv[4]}:unpublished"
                ),
                "certificate_identity": sys.argv[5] or None,
                "certificate_oidc_issuer": sys.argv[6] or None,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
)
PY
printf 'image config ID: %s\nregistry manifest digest: %s\nrootfs digest: %s\nmanifest: %s\n' \
  "$image_config_id" "${registry_digest:-unpublished}" "$rootfs_digest" "$manifest"
