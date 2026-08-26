# Docker + Cloudflare R2 object storage

The FastAPI backend is stateless in production:

- PostgreSQL stores relational/accounting metadata.
- Cloudflare R2 (S3-compatible) stores invoice originals, GED documents and versions, expense attachments, and OD supporting pieces.
- The API container does **not** persist uploaded files on its filesystem.

## Local Docker

For a local throwaway environment (uploads kept only in process memory):

```bash
docker compose up --build
```

API: `http://localhost:8000`  
Health: `http://localhost:8000/health`

To exercise real R2 locally, export `STORAGE_BACKEND=s3` and the four `R2_*` credentials before starting Compose.

## Cloudflare R2

Create a private R2 bucket and an API token with object read/write access. Configure:

```env
STORAGE_BACKEND=s3
R2_ACCOUNT_ID=<cloudflare-account-id>
R2_BUCKET=<private-bucket-name>
R2_ACCESS_KEY_ID=<r2-access-key>
R2_SECRET_ACCESS_KEY=<r2-secret-key>
OBJECT_STORAGE_PREFIX=pcg
```

The endpoint is derived automatically as:
`https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`.

The bucket does not need to be public. Downloads remain authorization-checked by FastAPI and are proxied from the private object store.

## Production requirements

`APP_ENV=production` fails fast unless:

- `DATABASE_URL` is PostgreSQL;
- `STORAGE_BACKEND=s3`;
- R2/S3 endpoint, bucket and credentials are configured;
- `JWT_SECRET` is strong;
- bootstrap super-admin credentials are explicitly configured.

The Docker image runs as a non-root user and has no application data volume.

## Legacy filesystem uploads in this package

Three pre-existing legacy invoice image files were found under `pcg_engine/data/files`.
They were moved out of the runtime backend tree to `../legacy_uploads_backup/` so the
Docker context no longer carries uploaded documents.

Once R2 credentials are configured, inspect the mapping first:

```bash
python scripts/migrate_legacy_files_to_object_storage.py ../legacy_uploads_backup --dry-run
```

Then upload:

```bash
python scripts/migrate_legacy_files_to_object_storage.py ../legacy_uploads_backup
```

The migration command does not delete the backup.
