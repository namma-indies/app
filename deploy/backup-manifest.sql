-- Row counts and schema head recorded beside each nightly dump (see
-- deploy/BACKUPS.md). The restore drill compares a restored copy against them.
-- Taken immediately before pg_dump, so a busy minute can differ by a few rows;
-- the drill allows a small tolerance. Counts only: no identifiers or content.
SET statement_timeout = '120s';
SELECT json_build_object(
    'schema_head', (SELECT min(version_num) FROM alembic_version),
    'counts', json_build_object(
        'observers', (SELECT count(*) FROM observers),
        'sightings', (SELECT count(*) FROM sightings),
        'captures', (SELECT count(*) FROM captures),
        'photos', (SELECT count(*) FROM photos),
        'individuals', (SELECT count(*) FROM individuals),
        'confirmations', (SELECT count(*) FROM confirmations),
        'embeddings', (SELECT count(*) FROM embeddings WHERE vec_miew IS NOT NULL)
    )
);
