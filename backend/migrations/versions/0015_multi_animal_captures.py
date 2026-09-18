"""Capture-owned sources and independently published animal evidence."""
from alembic import op

revision = "0015_multi_animal_captures"
down_revision = "0014_merge_detections_media_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE captures (
            id uuid PRIMARY KEY,
            observer_id uuid REFERENCES observers(id) ON DELETE SET NULL,
            client_token text,
            kind text NOT NULL CHECK (kind IN ('photo','video')),
            processing_state text NOT NULL DEFAULT 'queued'
                CHECK (processing_state IN ('legacy','queued','processing','needs_review','ready','no_animal','failed')),
            captured_at timestamptz NOT NULL,
            reported_at timestamptz,
            geog geography(Point,4326),
            geo_source text,
            geo_accuracy_m double precision,
            attrs jsonb NOT NULL DEFAULT '{}',
            clip_s3_key text,
            revision int NOT NULL DEFAULT 0,
            review_groups jsonb NOT NULL DEFAULT '[]',
            published_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE(observer_id,client_token)
        );
        CREATE INDEX ix_captures_owner_created ON captures(observer_id,created_at DESC,id);
        ALTER TABLE sightings ADD COLUMN capture_id uuid REFERENCES captures(id),
            ADD COLUMN species text CHECK (species IN ('dog','cat'));
        CREATE INDEX ix_sightings_capture ON sightings(capture_id);
        INSERT INTO captures (id,observer_id,client_token,kind,processing_state,captured_at,
            reported_at,geog,geo_source,geo_accuracy_m,attrs,clip_s3_key,published_at,created_at)
        SELECT id,observer_id,client_token,CASE WHEN clip_s3_key IS NULL THEN 'photo' ELSE 'video' END,
            'legacy',captured_at,reported_at,geog,geo_source,geo_accuracy_m,attrs,clip_s3_key,created_at,created_at
        FROM sightings;
        UPDATE sightings SET capture_id=id;
        CREATE FUNCTION attach_legacy_capture() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.capture_id IS NULL THEN
                INSERT INTO captures(id,observer_id,client_token,kind,processing_state,captured_at,
                    reported_at,geog,geo_source,geo_accuracy_m,attrs,published_at)
                VALUES(NEW.id,NEW.observer_id,NEW.client_token,'photo','legacy',NEW.captured_at,
                    NEW.reported_at,NEW.geog,NEW.geo_source,NEW.geo_accuracy_m,NEW.attrs,now());
                NEW.capture_id := NEW.id;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER sighting_capture BEFORE INSERT ON sightings
            FOR EACH ROW EXECUTE FUNCTION attach_legacy_capture();
        ALTER TABLE photos ALTER COLUMN sighting_id DROP NOT NULL,
            ADD COLUMN capture_id uuid REFERENCES captures(id) ON DELETE CASCADE;
        CREATE INDEX ix_photos_capture ON photos(capture_id);
        ALTER TABLE photos ADD CONSTRAINT photo_owner CHECK (sighting_id IS NOT NULL OR capture_id IS NOT NULL);
        CREATE TABLE animal_instances (
            id uuid PRIMARY KEY,
            capture_id uuid NOT NULL REFERENCES captures(id) ON DELETE CASCADE,
            source_photo_id uuid NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            evidence_photo_id uuid NOT NULL UNIQUE REFERENCES photos(id) ON DELETE CASCADE,
            sighting_id uuid REFERENCES sightings(id) ON DELETE SET NULL,
            track_id text NOT NULL,
            species text NOT NULL CHECK (species IN ('dog','cat')),
            confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
            bbox jsonb NOT NULL,
            crop_bbox jsonb NOT NULL,
            timestamp_ms int,
            details jsonb NOT NULL DEFAULT '{}',
            UNIQUE(capture_id,track_id,source_photo_id)
        );
        CREATE INDEX ix_instances_capture ON animal_instances(capture_id);
        ALTER TABLE embeddings ADD COLUMN instance_id uuid REFERENCES animal_instances(id) ON DELETE CASCADE;
        CREATE UNIQUE INDEX ix_embeddings_instance_model ON embeddings(instance_id,model) WHERE instance_id IS NOT NULL;
        ALTER TABLE jobs ADD COLUMN capture_id uuid REFERENCES captures(id) ON DELETE CASCADE;
        CREATE UNIQUE INDEX ix_jobs_capture_pipeline ON jobs(capture_id,pipeline_version) WHERE capture_id IS NOT NULL;
        ALTER TABLE jobs DROP CONSTRAINT jobs_terminal_outcome_check;
        ALTER TABLE jobs ADD CONSTRAINT jobs_terminal_outcome_check
            CHECK (terminal_outcome IN ('ready','no_animal','failed','needs_review'));
        GRANT SELECT,INSERT,UPDATE,DELETE ON captures,animal_instances TO app_rw;
    """)


def downgrade():
    # Dropping instance ownership would reinterpret multiple animals as one.
    raise RuntimeError("Disable MULTI_ANIMAL_ENABLED to roll back intake; preserve capture data")
