import { useState } from "react";

type Frame = { url: string; thumb_url: string };

/** The photos of one sighting. A clip can carry up to twelve frames, so they are
 * not stacked: the first (the one the map pin shows) leads at full width, and the
 * rest are same-size thumbnails in one scrolling strip. Tapping one leads with it. */
export default function SightingFrames({ photos, noun }: { photos: Frame[]; noun: string }) {
  const [lead, setLead] = useState(0);
  if (photos.length === 0) return null;
  const current = photos[Math.min(lead, photos.length - 1)];
  if (photos.length === 1) return <img className="viewer-photo" src={current.url} alt={`${noun} sighting`} />;
  return (
    <div className="frames">
      <div className="frames-lead">
        <img src={current.url} alt={`${noun} sighting, frame ${lead + 1} of ${photos.length}`} />
      </div>
      <div className="frames-strip" role="group" aria-label="Frames">
        {photos.map((photo, index) => (
          <button key={photo.url} type="button" className="frames-thumb" aria-pressed={index === lead}
            aria-label={`Show frame ${index + 1} of ${photos.length}`} onClick={() => setLead(index)}>
            <img src={photo.thumb_url} alt="" />
          </button>
        ))}
      </div>
    </div>
  );
}
