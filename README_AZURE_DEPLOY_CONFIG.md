# Azure deployment configuration

Use `datarisInfra` as the source of truth for Dataris deployment configuration.
Terraform injects PostgreSQL, Key Vault, CORS, Azure Blob Storage containers and
the managed identity into Azure Container Apps.

Never place Azure Storage keys, connection strings or secret values in this
repository. Configure optional runtime secrets through Azure Key Vault.

For the Blob Storage migration, see
[`README_AZURE_BLOB_STORAGE.md`](README_AZURE_BLOB_STORAGE.md).
