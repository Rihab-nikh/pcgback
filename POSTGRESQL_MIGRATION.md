# PostgreSQL migration

The backend now uses PostgreSQL as its default/runtime database. SQLite remains only as an explicit deterministic test/legacy-migration backend and is rejected when `APP_ENV=production`.

## Local PostgreSQL

```bash
docker compose -f docker-compose.postgres.yml up -d
cp .env.example .env
# DATABASE_URL=postgresql://pcg:pcg@localhost:5432/pcg_maroc_ai
pip install -r requirements.txt
uvicorn app.main:app --reload
```

At startup the current v16 PostgreSQL baseline schema is created idempotently and platform accounting/tax reference rows are seeded.

## Neon / managed PostgreSQL

Set the provider URL directly, including TLS requirements when supplied:

```env
DATABASE_URL=postgresql://USER:PASSWORD@HOST/DB?sslmode=require
```

Production refuses SQLite.

## Copy an existing SQLite database

No SQLite database is bundled in this repository. If an older deployment has one, copy it before switching traffic:

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --sqlite /path/to/pcg.db \
  --database-url 'postgresql://USER:PASSWORD@HOST/DB?sslmode=require'
```

The migration creates the PostgreSQL schema first, copies known columns in foreign-key-safe schema order, defers PostgreSQL FK checks for the copy transaction, and keeps existing target rows on conflicts.

After migration, point the application only at `DATABASE_URL` and keep the old SQLite file as a read-only backup until the pilot data has been reconciled.
