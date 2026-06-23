# Dataris backend deployment on Azure Container Apps

The backend is deployed through the `datarisInfra` repository. It uses Azure
Container Apps, Azure Container Registry, Azure PostgreSQL, Key Vault and a
private Azure Blob container.

```bash
cd ../datarisInfra
./scripts/build_backend_image_for_environment.sh dev
./scripts/terraform_env.sh dev plan
terraform apply .plans/dev.tfplan
```

The backend authenticates to Blob Storage with its user-assigned managed
identity. Do not configure Google service-account JSON, storage keys or a
connection string.

For full Blob Storage validation read
[`README_AZURE_BLOB_STORAGE.md`](README_AZURE_BLOB_STORAGE.md).
