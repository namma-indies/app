-- Entirely synthetic corpus for CI's real pg_dump/pg_restore rehearsal.
INSERT INTO observers(id, display_name) VALUES ('00000000-0000-4000-8000-000000000001', 'Synthetic');
INSERT INTO individuals(id, created_by) VALUES ('00000000-0000-4000-8000-000000000002', 'manual');
INSERT INTO sightings(id, observer_id, individual_id, captured_at, geo_source)
VALUES ('00000000-0000-4000-8000-000000000003', '00000000-0000-4000-8000-000000000001',
        '00000000-0000-4000-8000-000000000002', now(), 'none');
INSERT INTO photos(id, sighting_id, s3_key, width, height)
VALUES ('00000000-0000-4000-8000-000000000004', '00000000-0000-4000-8000-000000000003',
        'sightings/synthetic/photo.webp', 32, 32);
INSERT INTO embeddings(id, photo_id, model, dim, vec_miew)
VALUES ('00000000-0000-4000-8000-000000000005', '00000000-0000-4000-8000-000000000004',
        'miewid-msv3', 2152, ('[1,' || array_to_string(array_fill(0, ARRAY[2151]), ',') || ']')::vector);
INSERT INTO confirmations(id, sighting_id, individual_id, observer_id, verdict)
VALUES ('00000000-0000-4000-8000-000000000006', '00000000-0000-4000-8000-000000000003',
        '00000000-0000-4000-8000-000000000002', '00000000-0000-4000-8000-000000000001', 'same');
