#!/usr/bin/env bash
set -euo pipefail

# Configure or update a Google Cloud Scheduler job that periodically invokes
# the protected DigiForms synchronization endpoint exposed by Dataris backend.
#
# Required environment variables:
#   GCP_PROJECT_ID
#   GCP_REGION
#   DATARIS_BACKEND_URL              Example: https://dataris-api.example.com
#   DIGIFORMS_SYNC_CRON_SECRET       Must match the backend environment variable
#
# Optional:
#   DIGIFORMS_SYNC_SCHEDULE          Default: */10 * * * *
#   DIGIFORMS_SYNC_TIME_ZONE         Default: America/Guatemala
#   DIGIFORMS_SYNC_JOB_NAME          Default: dataris-digiforms-sync

: "${GCP_PROJECT_ID:?Define GCP_PROJECT_ID}"
: "${GCP_REGION:?Define GCP_REGION}"
: "${DATARIS_BACKEND_URL:?Define DATARIS_BACKEND_URL}"
: "${DIGIFORMS_SYNC_CRON_SECRET:?Define DIGIFORMS_SYNC_CRON_SECRET}"

JOB_NAME="${DIGIFORMS_SYNC_JOB_NAME:-dataris-digiforms-sync}"
SCHEDULE="${DIGIFORMS_SYNC_SCHEDULE:-*/10 * * * *}"
TIME_ZONE="${DIGIFORMS_SYNC_TIME_ZONE:-America/Guatemala}"
URI="${DATARIS_BACKEND_URL%/}/api/compat/sig-agricola/sync/cron"
HEADERS="X-Dataris-Cron-Secret=${DIGIFORMS_SYNC_CRON_SECRET},Content-Type=application/json"

COMMON_ARGS=(
  --project "$GCP_PROJECT_ID"
  --location "$GCP_REGION"
  --schedule "$SCHEDULE"
  --time-zone "$TIME_ZONE"
  --uri "$URI"
  --http-method POST
  --headers "$HEADERS"
  --message-body '{}'
)

if gcloud scheduler jobs describe "$JOB_NAME" \
  --project "$GCP_PROJECT_ID" \
  --location "$GCP_REGION" >/dev/null 2>&1; then
  echo "Updating Cloud Scheduler job: $JOB_NAME"
  gcloud scheduler jobs update http "$JOB_NAME" "${COMMON_ARGS[@]}"
else
  echo "Creating Cloud Scheduler job: $JOB_NAME"
  gcloud scheduler jobs create http "$JOB_NAME" "${COMMON_ARGS[@]}"
fi

echo "Configured $JOB_NAME -> $URI"
echo "Schedule: $SCHEDULE ($TIME_ZONE)"
