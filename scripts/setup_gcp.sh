#!/usr/bin/env bash
#
# One-time Google Cloud setup for FieldPilot.
#
# This script doubles as the spin-up instructions the hackathon submission
# requires: anyone with gcloud and their own billing account can reproduce the
# environment by running it.
#
# Usage:
#   ./scripts/setup_gcp.sh <PROJECT_ID> <BILLING_ACCOUNT_ID> [ORG_ID]
#
set -euo pipefail

PROJECT_ID="${1:?usage: setup_gcp.sh <PROJECT_ID> <BILLING_ACCOUNT_ID> [ORG_ID]}"
BILLING_ACCOUNT="${2:?missing billing account id, e.g. 0187XX-XXXXXX-XXXXXX}"
ORG_ID="${3:-}"
REGION="${REGION:-us-central1}"

echo "==> Project      : ${PROJECT_ID}"
echo "==> Billing      : ${BILLING_ACCOUNT}"
echo "==> Region       : ${REGION}"
echo

# ---------------------------------------------------------------------------
# 1. Create the project
# ---------------------------------------------------------------------------
if gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "==> Project already exists, skipping creation"
else
  if [[ -n "${ORG_ID}" ]]; then
    gcloud projects create "${PROJECT_ID}" --name="FieldPilot" --organization="${ORG_ID}"
  else
    gcloud projects create "${PROJECT_ID}" --name="FieldPilot"
  fi
fi

gcloud config set project "${PROJECT_ID}"

# ---------------------------------------------------------------------------
# 2. Link the dedicated billing account
#
# This is the step that matters most. Credits attach to a billing account, not
# to a project, so linking the wrong one means the hackathon credits get spent
# by unrelated work.
# ---------------------------------------------------------------------------
gcloud billing projects link "${PROJECT_ID}" --billing-account="${BILLING_ACCOUNT}"

echo
echo "==> Verifying the billing link"
gcloud billing projects describe "${PROJECT_ID}"

# ---------------------------------------------------------------------------
# 3. Enable the APIs the system actually uses
# ---------------------------------------------------------------------------
echo
echo "==> Enabling APIs (this takes a minute)"
# Firestore, Pub/Sub and Cloud Scheduler were in this list from day one and
# nothing in the codebase uses them. Enabling APIs costs nothing, but a setup
# script is documentation, and documentation that names services the system
# does not touch is wrong documentation.
gcloud services enable \
  aiplatform.googleapis.com \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com

# ---------------------------------------------------------------------------
# 4. Application default credentials, so local runs can reach Vertex AI
# ---------------------------------------------------------------------------
echo
echo "==> Next, authorise local credentials if you have not already:"
echo "      gcloud auth application-default login"
echo
echo "==> Then verify the whole chain works:"
echo "      python scripts/smoke_test.py ${PROJECT_ID} ${REGION}"
echo
echo "Setup complete."
