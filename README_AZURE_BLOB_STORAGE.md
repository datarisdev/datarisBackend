# Dataris – Azure Blob Storage migration

This backend no longer uses Google Cloud Storage for Sentinel-2 files, parcel
uploads, or avatars. All persistent objects live in the private Azure Blob
container provisioned by `datarisInfra`.

## Runtime authentication

The backend does not use a storage account key or connection string.

- **Azure Container Apps:** `ManagedIdentityCredential` uses the user-assigned
  identity selected through `AZURE_CLIENT_ID`.
- **Local development:** `DefaultAzureCredential` uses your local Azure sign-in
  (normally `az login`) or another standard Azure Identity credential.

Terraform configures the application with:

```text
AZURE_STORAGE_ACCOUNT_URL
AZURE_STORAGE_ACCOUNT_NAME
AZURE_STORAGE_CONTAINER_NAME
AZURE_PARCELS_STORAGE_CONTAINER
AZURE_SATELLITE_STORAGE_CONTAINER
AZURE_AVATARS_STORAGE_CONTAINER
AZURE_CLIENT_ID
SATELLITE_STORAGE_PROVIDER=azure
DISABLE_AZURE_BLOB_STORAGE=false
AZURE_BLOB_STORAGE_STRICT=false
AZURE_BLOB_READ_SAS_TTL_HOURS=1
```

## Required Azure RBAC roles

The backend managed identity needs both roles at the Storage Account scope:

1. `Storage Blob Data Contributor` for read/write/delete operations.
2. `Storage Blob Delegator` for short-lived user-delegation SAS URLs used by
   the browser to read private avatars and satellite TIFF files.

The supplied `datarisInfra/identity.tf` patch creates both assignments.

## Deployment order for `dev`

```bash
cd ~/Documentos/GIthubProjects/Dataris/datarisInfra
./scripts/terraform_env.sh dev plan
terraform apply .plans/dev.tfplan

./scripts/build_backend_image_for_environment.sh dev
./scripts/terraform_env.sh dev plan
terraform apply .plans/dev.tfplan
```

After the first Terraform apply, Azure RBAC propagation can take several
minutes. Do not enable strict mode until a Blob upload/download has succeeded.

## Validation

```bash
export RG="datarisjm26-dev-rg"
export APP="dataris-api-dev"

export REVISION="$(az containerapp show --name "$APP" --resource-group "$RG" --query properties.latestRevisionName -o tsv)"
export REPLICA="$(az containerapp replica list --name "$APP" --resource-group "$RG" --revision "$REVISION" --query '[0].name' -o tsv)"

az containerapp logs show \
  --name "$APP" \
  --resource-group "$RG" \
  --revision "$REVISION" \
  --replica "$REPLICA" \
  --container dataris-api \
  --tail 200 \
  --format text
```

The successful deployment must not show `Google Cloud Storage credentials are
not configured`. Empty Azure Blob containers are normal on the first run: they
are treated as cache misses without a traceback. After Sentinel-2 generates a
layer, the container should contain paths such as:

```text
cache/<hash>.png
cache/<hash>.json
manifests/latest/<hash>.json
manifests/date/<hash>.json
satellite/<user>/<parcel>/<index>/<date>.png
```

## Avatar and parcel references

Private files are stored in PostgreSQL as durable references such as:

```text
azureblob://avatars/<user-id>/avatar.png
azureblob://parcels/<user-id>/<parcel-id>/original.zip
```

The backend resolves them to a short-lived SAS URL only when a browser needs
access. Existing external avatar URLs are not overwritten automatically.
