"""Write and rebuild sighting means from the active per-photo recipe only."""

import numpy as np

from app.embed import MODEL_NAME
from app.reid_recipe import ACTIVE_RECIPE


async def save_mean(conn, sighting_id, vector):
    """A missing mean clears its tags as well as any stale cached vector."""
    await conn.execute("""UPDATE sightings SET vec_miew=$2::vector,
        vec_miew_model=$3, vec_miew_recipe=$4 WHERE id=$1""", sighting_id, vector,
        MODEL_NAME if vector is not None else None,
        ACTIVE_RECIPE if vector is not None else None)


async def rebuild_mean(conn, sighting_id):
    """No inference or media access: use persisted active frames, including v2 crops.

    Lock the sighting while taking the frame snapshot and replacing its mean.
    Returns whether the result is usable; no frames or a cancelling mean clears
    an old cache rather than relabelling it as the new recipe.
    """
    async with conn.transaction():
        await conn.execute("SELECT id FROM sightings WHERE id=$1 FOR UPDATE", sighting_id)
        rows = await conn.fetch("""SELECT e.vec_miew::text AS vec
            FROM photos p JOIN embeddings e ON e.photo_id=p.id
            WHERE p.sighting_id=$1 AND e.model=$2 AND e.recipe=$3
              AND e.vec_miew IS NOT NULL ORDER BY p.created_at,p.id""",
            sighting_id, MODEL_NAME, ACTIVE_RECIPE)
        vector = None
        if rows:
            mean = np.mean([[float(v) for v in r["vec"].strip("[]").split(",")]
                            for r in rows], axis=0)
            norm = float(np.linalg.norm(mean))
            if np.isfinite(norm) and norm > 1e-8:
                vector = "[" + ",".join(f"{float(v):.7g}" for v in mean / norm) + "]"
        await save_mean(conn, sighting_id, vector)
        return vector is not None
