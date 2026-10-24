"""Builders for the social-post tests: consenting observers, shared sightings
of both upload shapes, and the areas a caption is named from.

Rows are written directly rather than through the upload routes, because what
these tests pin is who and what may be posted -- the routes that create the
rows have their own tests.
"""

from app.ids import uuid7

BLR = (12.9716, 77.5946)  # lat, lng


async def observer(conn, *, handle="a_handle", consent=True, tier=None, name="Akash"):
    oid = uuid7()
    await conn.execute(
        "INSERT INTO observers (id, display_name, created_via, x_handle, social_consent, "
        "trust_tier) VALUES ($1,$2,'test',$3,$4,$5)", oid, name, handle, consent, tier)
    return oid


def _geog(at):
    return None if at is None else f"SRID=4326;POINT({at[1]} {at[0]})"


async def legacy_sighting(conn, oid, *, shared=True, review="valid", source=None,
                          species="dog", at=BLR, photos=1):
    """A /sighting upload: its hashed photos hang off the sighting itself."""
    sid = uuid7()
    attrs = '{"source": "%s"}' % source if source else "{}"
    await conn.execute(
        "INSERT INTO sightings (id, observer_id, captured_at, geo_source, geog, species, "
        "review_status, attrs, shared_with_team_at) VALUES ($1,$2,now(),'device_gps',"
        "$3::geography,$4,$5,$6::jsonb, CASE WHEN $7 THEN now() END)",
        sid, oid, _geog(at), species, review, attrs, shared)
    pids = []
    for n in range(photos):
        pid = uuid7()
        await conn.execute(
            "INSERT INTO photos (id, sighting_id, s3_key, content_sha256, created_at) "
            "VALUES ($1,$2,$3,$4, now() + make_interval(secs => $5))",
            pid, sid, f"sightings/{sid}/{pid}.webp", f"sha-{pid}", n)
        pids.append(pid)
    return sid, pids


async def capture_sighting(conn, oid, *, kind="photo", shared=True, at=BLR):
    """A /capture upload: the sighting holds only a crop; the postable image is
    the capture's source photo, reached through its animal instance."""
    cid, src, sid, crop, iid = uuid7(), uuid7(), uuid7(), uuid7(), uuid7()
    await conn.execute(
        "INSERT INTO captures (id, observer_id, kind, captured_at, processing_state) "
        "VALUES ($1,$2,$3,now(),'ready')", cid, oid, kind)
    await conn.execute(
        "INSERT INTO photos (id, capture_id, s3_key, content_sha256) VALUES ($1,$2,$3,$4)",
        src, cid, f"captures/{cid}/sources/{src}.webp", f"sha-{src}")
    await conn.execute(
        "INSERT INTO sightings (id, capture_id, observer_id, captured_at, geo_source, geog, "
        "species, shared_with_team_at) VALUES ($1,$2,$3,now(),'device_gps',$4::geography,"
        "'dog', CASE WHEN $5 THEN now() END)", sid, cid, oid, _geog(at), shared)
    await conn.execute(
        "INSERT INTO photos (id, capture_id, sighting_id, s3_key) VALUES ($1,$2,$3,$4)",
        crop, cid, sid, f"captures/{cid}/evidence/{crop}.webp")
    await conn.execute(
        """INSERT INTO animal_instances (id, capture_id, source_photo_id, evidence_photo_id,
           sighting_id, track_id, species, confidence, bbox, crop_bbox)
           VALUES ($1,$2,$3,$4,$5,'t','dog',0.9,'[0,0,1,1]','[0,0,1,1]')""",
        iid, cid, src, crop, sid)
    return sid, src, crop


async def ward(conn, name="Indiranagar", *, centre=BLR, half=0.01, kind="bbmp_ward"):
    lat, lng = centre
    x0, x1, y0, y1 = lng - half, lng + half, lat - half, lat + half
    await conn.execute(
        "INSERT INTO areas (id, name, kind, geog) VALUES ($1,$2,$3, ST_GeogFromText("
        f"'MULTIPOLYGON((({x0} {y0}, {x0} {y1}, {x1} {y1}, {x1} {y0}, {x0} {y0})))'))",
        uuid7(), name, kind)
