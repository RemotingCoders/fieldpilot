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
#   and read its two secrets, and nothing else. If the service is compromised,
#   that sentence is the whole blast radius.
# - **The Maps key goes through Secret Manager, never --set-env-vars.** An env
#   var set on the command line lands in the service's console page, in
#   `gcloud run services describe`, and in the shell history of whoever
#   deployed. A secret reference appears everywhere as a name.
# - **max-instances=2.** This is a demo on a credit budget. The failure mode
#   being bought off is a traffic spike (or a judge with a load tester)
#   silently converting the remaining credits into idle containers.
# - **--allow-unauthenticated, with the paid routes behind a key.** Judges
#   must be able to open the URL without an IAM invitation, so the service is
#   public. But /intake and /intake/multimodal each spend a Gemini call, and a
#   public endpoint that spends money is a budget with a stranger's hand on
#   it — so those two ask for `X-API-Key`. The key is generated once, lives in
#   Secret Manager as fieldpilot-api-key, is kept across deploys (the value
#   pasted into the Devpost testing instructions has to keep working), and
#   reaches judges through that private field only. Everything free —
#   /health, /compare, /docs — stays open. To rotate it:
#     openssl rand -hex 24 | tr -d '\n' | gcloud secrets versions add fieldpilot-api-key --data-file=-
#   then rerun this script, because running instances keep the version they
#   started with.
# - **concurrency=8, timeout=60s.** The key decides who may spend; these two
#   cap how fast anyone can, even if it leaks. Cloud Run enforces both outside
#   the container, so no bug in the application can lift them: at most 16
#   model calls in flight across the two instances, none older than a minute.
#   The defaults (80 per instance, 5 minutes) would let one leaked key run
#   ~160 audio transcriptions at once.
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
# 2. Secrets: the intake API key (always) and the Maps key (if configured)
#
# The API key is created here, once, and never rotated by this script — the
# value pasted into the Devpost testing instructions has to survive every
# redeploy (rotation is a one-liner in the header). The Maps key is optional
# on purpose: without it the service runs with the offline geocoder stand-in,
# clearly labelled as such, exactly like a local run without a key.
# ---------------------------------------------------------------------------
grant_secret() {
  gcloud secrets add-iam-policy-binding "$1" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" --quiet >/dev/null
}

if ! gcloud secrets describe fieldpilot-api-key >/dev/null 2>&1; then
  echo "==> Generating the intake API key (first deploy only)"
  # tr strips the newline openssl appends: a secret payload with a trailing
  # newline is a key nobody can paste correctly.
  openssl rand -hex 24 | tr -d '\n' | gcloud secrets create fieldpilot-api-key --data-file=-
else
  echo "==> Intake API key already exists — keeping it (see the header to rotate)"
fi
grant_secret fieldpilot-api-key
SECRETS="FIELDPILOT_API_KEY=fieldpilot-api-key:latest"

MAPS_KEY="$(grep -E '^FIELDPILOT_MAPS_API_KEY=' .env 2>/dev/null | cut -d= -f2- || true)"
if [[ -n "${MAPS_KEY}" ]]; then
  echo "==> Storing the Maps key in Secret Manager"
  if ! gcloud secrets describe fieldpilot-maps-key >/dev/null 2>&1; then
    printf '%s' "${MAPS_KEY}" | gcloud secrets create fieldpilot-maps-key --data-file=-
  else
    printf '%s' "${MAPS_KEY}" | gcloud secrets versions add fieldpilot-maps-key --data-file=-
  fi
  grant_secret fieldpilot-maps-key
  SECRETS+=",FIELDPILOT_MAPS_API_KEY=fieldpilot-maps-key:latest"
else
  echo "==> No Maps key in .env — deploying with the offline geocoder stand-in"
fi

# ---------------------------------------------------------------------------
# 2b. The shared geocode cache bucket
#
# Without it every Cloud Run instance keeps its own disposable cache and
# re-pays the Geocoding API for addresses another instance already resolved.
# The runtime account gets objectAdmin on THIS bucket only — not storage
# access on the project.
# ---------------------------------------------------------------------------
BUCKET="${PROJECT_ID}-fieldpilot-cache"
if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  echo "==> Creating cache bucket gs://${BUCKET}"
  gcloud storage buckets create "gs://${BUCKET}" \
    --location="${REGION}" --uniform-bucket-level-access
fi
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectAdmin" --quiet >/dev/null

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
  --concurrency 8 \
  --timeout 60 \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global,FIELDPILOT_MODEL=gemini-3.5-flash,FIELDPILOT_GCS_BUCKET=${BUCKET}" \
  --set-secrets "${SECRETS}"

# ---------------------------------------------------------------------------
# 5. Prove it, from outside
# ---------------------------------------------------------------------------
URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format='value(status.url)')"
echo
echo "==> Service URL: ${URL}"
echo
echo "==> /health   (NOT /healthz: that path is reserved by Cloud Run's frontend"
echo "               and 404s before reaching the container — cloud.google.com/run/docs/issues)"
curl -sf "${URL}/health" && echo
echo
echo "==> /compare (offline, reproducible, no model call)"
curl -sf "${URL}/compare?seed=42&orders=20" && echo
echo
echo "==> /intake without the key (must be 401: the paid door is closed)"
CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST "${URL}/intake" \
  -H 'Content-Type: application/json' -d '{"text": "prueba"}')"
if [[ "${CODE}" != "401" ]]; then
  # A deploy that leaves a paid endpoint open is worse than no deploy. Take
  # the public door away before reporting the failure, so the URL is a 403
  # while someone reads this message rather than a free Gemini for everyone.
  echo "    expected 401, got ${CODE}: the key is not enforced. Closing the public door." >&2
  gcloud run services remove-iam-policy-binding "${SERVICE}" --region "${REGION}" \
    --member=allUsers --role=roles/run.invoker --quiet >/dev/null
  exit 1
fi
echo "    401, as it should be"
echo
echo "==> /intake with the key (one real Gemini call through Vertex AI)"
API_KEY="$(gcloud secrets versions access latest --secret=fieldpilot-api-key)"
curl -sf -X POST "${URL}/intake" \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: ${API_KEY}" \
  -d '{"text": "el aire de la pared no calienta, Av. Cabildo 2340 piso 3, CABA"}' && echo
echo
echo "Deploy verified end to end."
echo
echo "The intake key goes in the Devpost 'Testing instructions' field (judges only):"
echo "    gcloud secrets versions access latest --secret=fieldpilot-api-key"
