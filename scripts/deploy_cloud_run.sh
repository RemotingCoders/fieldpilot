#!/usr/bin/env bash
#
# Deploy FieldPilot to Cloud Run.
#
# Run from the repository root:
#   ./scripts/deploy_cloud_run.sh <PROJECT_ID>
#
# Re-running is safe: every step either creates or updates in place, so this
# is also how a fix gets shipped.
#
# Choices worth explaining, because each one could go the other way:
#
# - **A dedicated service account with two narrow roles.** The default compute
#   account ships with broad project permissions; this one can call Vertex AI
#   and read one secret, and nothing else. If the service is compromised, that
#   sentence is the whole blast radius.
# - **The Maps key goes through Secret Manager, never --set-env-vars.** An env
#   var set on the command line lands in the service's console page, in
#   `gcloud run services describe`, and in the shell history of whoever
#   deployed. A secret reference appears everywhere as a name.
# - **max-instances=2.** This is a demo on a credit budget. The failure mode
#   being bought off is a traffic spike (or a judge with a load tester)
#   silently converting the remaining credits into idle containers.
# - **--allow-unauthenticated,** because judges must be able to poke the URL
#   without being sent an IAM invitation first. The endpoints mutate nothing
#   and the expensive one is capped by max-instances.
# - **1Gi / 1 CPU.** OR-Tools and the ADK import heavily; 512Mi OOMs during
#   cold start and produces exactly the kind of intermittent 503 that eats an
#   evening. Memory is cheap, evenings are not.
#
set -euo pipefail

# Never let gcloud ask a question. Prompts in a script either hang it or, when
# a pipe holds stdin, answer themselves with the default — which is how this
# script once said "No" to its own rescue without anyone seeing the question.
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

PROJECT_ID="${1:?usage: deploy_cloud_run.sh <PROJECT_ID>}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-fieldpilot}"
SA_NAME="fieldpilot-run"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "${PROJECT_ID}" --quiet

# ---------------------------------------------------------------------------
# 0. The APIs this script itself needs, enabled idempotently
#
# Learned the hard way: this script once assumed setup_gcp.sh had enabled
# everything, but the project predated the version of setup that knew about
# Secret Manager. Worse, gcloud's own "enable and retry?" prompt was silently
# answered "No" because a pipe was occupying stdin. A deploy script that
# depends on another script having run before it is a deploy script that
# fails on exactly the machine that matters.
# ---------------------------------------------------------------------------
echo "==> Making sure required APIs are enabled (no-op when already on)"
gcloud services enable \
  aiplatform.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com

# Enabling reports success before the API actually answers: propagation takes
# up to a few minutes on a project where it was never on. Waiting here, against
# the real API rather than the metadata, is the script's job — not the job of
# whoever runs it at midnight and gets told to "wait a few minutes and retry".
echo "==> Waiting for Secret Manager to actually answer (first enable can take ~2 min)"
for attempt in $(seq 1 24); do
  if gcloud secrets list --limit=1 >/dev/null 2>&1; then
    echo "    ready (attempt ${attempt})"
    break
  fi
  if [[ "${attempt}" -eq 24 ]]; then
    echo "    still not answering after 4 minutes — rerun the script in a few minutes" >&2
    exit 1
  fi
  sleep 10
done

# ---------------------------------------------------------------------------
# 1. Service account, created once
# ---------------------------------------------------------------------------
if ! gcloud iam service-accounts describe "${SA_EMAIL}" >/dev/null 2>&1; then
  echo "==> Creating service account ${SA_NAME}"
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="FieldPilot Cloud Run runtime"
fi

echo "==> Granting Vertex AI access"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/aiplatform.user" \
  --condition=None --quiet >/dev/null

# ---------------------------------------------------------------------------
# 2. The Maps key, if one is configured locally
#
# Optional on purpose: without it the service runs with the offline geocoder
# stand-in, clearly labelled as such, exactly like a local run without a key.
# ---------------------------------------------------------------------------
SECRET_ARGS=()
MAPS_KEY="$(grep -E '^FIELDPILOT_MAPS_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)"
if [[ -n "${MAPS_KEY}" ]]; then
  echo "==> Storing the Maps key in Secret Manager"
  if ! gcloud secrets describe fieldpilot-maps-key >/dev/null 2>&1; then
    printf '%s' "${MAPS_KEY}" | gcloud secrets create fieldpilot-maps-key --data-file=-
  else
    printf '%s' "${MAPS_KEY}" | gcloud secrets versions add fieldpilot-maps-key --data-file=-
  fi
  gcloud secrets add-iam-policy-binding fieldpilot-maps-key \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
  SECRET_ARGS=(--set-secrets "FIELDPILOT_MAPS_API_KEY=fieldpilot-maps-key:latest")
else
  echo "==> No Maps key in .env — deploying with the offline geocoder stand-in"
fi

# ---------------------------------------------------------------------------
# 3. Let the build read its own source
#
# On projects created since 2024 the default compute service account no longer
# carries the Editor role, and `run deploy --source` runs its build as exactly
# that account — which then cannot read the source zip the same command just
# uploaded to GCS. The builder role bundles precisely what a build needs:
# read the source, push the image, write the logs.
# ---------------------------------------------------------------------------
PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
BUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
echo "==> Granting the build role to ${BUILD_SA}"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${BUILD_SA}" \
  --role="roles/cloudbuild.builds.builder" \
  --condition=None --quiet >/dev/null

# IAM grants propagate in seconds, not instantly. Ten of margin is cheaper
# than one confusing retry.
sleep 10

# ---------------------------------------------------------------------------
# 4. Build and deploy from source (Cloud Build uses the Dockerfile)
# ---------------------------------------------------------------------------
echo "==> Deploying ${SERVICE} to ${REGION}"
gcloud run deploy "${SERVICE}" \
  --source . \
  --region "${REGION}" \
  --service-account "${SA_EMAIL}" \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --max-instances 2 \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,FIELDPILOT_MODEL=gemini-3.5-flash" \
  "${SECRET_ARGS[@]}"

# ---------------------------------------------------------------------------
# 5. Prove it, from outside
# ---------------------------------------------------------------------------
URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
echo
echo "==> Service URL: ${URL}"
echo
echo "==> /healthz"
curl -sf "${URL}/healthz" && echo
echo
echo "==> /compare (offline, reproducible, no model call)"
curl -sf "${URL}/compare?seed=42&orders=20" && echo
echo
echo "==> /intake (one real Gemini call through Vertex AI)"
curl -sf -X POST "${URL}/intake" \
  -H 'Content-Type: application/json' \
  -d '{"text": "el aire de la pared no calienta, Av. Cabildo 2340 piso 3, CABA"}' && echo
echo
echo "Deploy verified end to end."
