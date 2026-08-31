import { useRef, useState } from "react";
import {
  readPhotoMetadata,
  UnauthorizedError,
  type Condition,
  type EarNotch,
  type GeoSource,
  type PhotoMetadata,
  type Sex,
} from "../api";
import { enqueue, flush } from "../offline/queue";
import DogSprite from "../components/DogSprite";
import ImportOriginPrompt from "../components/ImportOriginPrompt";
import { chooseFromGalleryIfNative, isNative, takePhotoIfNative } from "../capture/takePhoto";
import {
  originFromExif,
  originFromPerson,
  resolveCapturedAt,
  type ImportOrigin,
} from "../capture/importOrigin";

const MAX_PHOTOS = 5;

const SEX_OPTIONS: { value: Sex; label: string }[] = [
  { value: "male", label: "♂ male" },
  { value: "female", label: "♀ female" },
  { value: "unsure", label: "unsure" },
];

const EAR_NOTCH_OPTIONS: { value: EarNotch; label: string }[] = [
  { value: "none", label: "none" },
  { value: "left", label: "left" },
  { value: "right", label: "right" },
  { value: "unsure", label: "unsure" },
];

const CONDITION_OPTIONS: { value: Condition; label: string }[] = [
  { value: "healthy", label: "healthy" },
  { value: "injured", label: "injured" },
  { value: "unsure", label: "unsure" },
];

function Chips<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[];
  value: T | null;
  onChange: (v: T | null) => void;
}) {
  return (
    <div className="chip-row">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          className={"chip" + (value === o.value ? " active" : "")}
          onClick={() => onChange(value === o.value ? null : o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function getLocation(): Promise<GeolocationPosition | null> {
  return new Promise((resolve) => {
    // Checks the value, not just the key. Some webviews expose the property as
    // undefined, where an `in` test passes and the call below then throws --
    // which would reject out of submit() and lose the sighting.
    if (!navigator.geolocation) return resolve(null);
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve(pos),
      () => resolve(null),
      { timeout: 8000, enableHighAccuracy: true },
    );
  });
}

export default function Capture() {
  const fileRef = useRef<HTMLInputElement>(null);
  const videoRef = useRef<HTMLInputElement>(null);
  // Separate from fileRef because this one must NOT carry `capture`, which
  // forces the camera and hides the gallery.
  const importRef = useRef<HTMLInputElement>(null);
  const [photos, setPhotos] = useState<File[]>([]);
  const [previewUrls, setPreviewUrls] = useState<string[]>([]);
  // A sighting is photos or a clip, never both: the server extracts frames from
  // the clip and stores those as the sighting's photos, so mixing the two would
  // just be two ways of saying the same thing.
  const [video, setVideo] = useState<File | null>(null);
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [sex, setSex] = useState<Sex | null>(null);
  const [earNotch, setEarNotch] = useState<EarNotch | null>(null);
  const [condition, setCondition] = useState<Condition | null>(null);
  const [moreOpen, setMoreOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  // Set only for a camera-roll import: where and when the photo says it was
  // taken, which overrides the live clock and GPS fix in submit().
  const [origin, setOrigin] = useState<ImportOrigin | null>(null);
  // Non-null while we're asking the person for what the file didn't say.
  const [asking, setAsking] = useState<{ file: File; md: PhotoMetadata } | null>(null);

  function showToast(msg: string) {
    setToast(msg);
    setTimeout(() => setToast(null), 2600);
  }

  function addPhoto(f: File) {
    setPhotos((prev) => (prev.length >= MAX_PHOTOS ? prev : [...prev, f]));
    setPreviewUrls((prev) =>
      prev.length >= MAX_PHOTOS ? prev : [...prev, URL.createObjectURL(f)],
    );
  }

  function onFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    // Reset now so choosing the same file again still fires a change event.
    if (fileRef.current) fileRef.current.value = "";
    if (!f) return;
    addPhoto(f);
  }

  function onVideoChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (videoRef.current) videoRef.current.value = "";
    if (!f) return;
    previewUrls.forEach((url) => URL.revokeObjectURL(url));
    setPhotos([]);
    setPreviewUrls([]);
    if (videoUrl) URL.revokeObjectURL(videoUrl);
    setVideo(f);
    setVideoUrl(URL.createObjectURL(f));
    // A clip is a live capture; it must not inherit an import's date and place.
    setOrigin(null);
  }

  function removeVideo() {
    if (videoUrl) URL.revokeObjectURL(videoUrl);
    setVideo(null);
    setVideoUrl(null);
    if (videoRef.current) videoRef.current.value = "";
  }

  /** Replace whatever is staged with a single imported photo.
   *
   * One photo per import, not a multi-select: each carries its own capture date
   * and place, and photos chosen together aren't necessarily from the same
   * evening or street. Batching them would have to ask per photo or cluster by
   * EXIF -- deliberately left to the follow-up issue.
   */
  function stageImport(f: File, o: ImportOrigin) {
    previewUrls.forEach((url) => URL.revokeObjectURL(url));
    if (videoUrl) URL.revokeObjectURL(videoUrl);
    setVideo(null);
    setVideoUrl(null);
    setPhotos([f]);
    setPreviewUrls([URL.createObjectURL(f)]);
    setOrigin(o);
  }

  async function onImportFile(f: File) {
    let md: PhotoMetadata;
    try {
      md = await readPhotoMetadata(f);
    } catch (err) {
      if (err instanceof UnauthorizedError) {
        showToast("Session expired. Sign in again.");
        return;
      }
      throw err;
    }

    const capturedAt = resolveCapturedAt(md.captured_at_local, md.utc_offset_minutes);
    if (capturedAt && md.has_location && md.lat != null && md.lng != null) {
      // The file knew both. Nothing to ask.
      stageImport(f, originFromExif(capturedAt, md.lat, md.lng));
      return;
    }
    // Stripped of one or both -- the common case for forwards and screenshots,
    // and also what an offline preflight looks like. Ask rather than default to
    // here-and-now, which would poison the 1km prior re-ID matches against.
    setAsking({ file: f, md });
  }

  async function onImportPress() {
    if (isNative()) {
      try {
        const picked = await chooseFromGalleryIfNative();
        // null here means the user dismissed the picker. Falling through to the
        // web file input would reopen a chooser the instant they backed out.
        if (picked) await onImportFile(picked);
      } catch {
        showToast("Couldn't open your photos. Try again.");
      }
      return;
    }
    // Web: a file input with no `capture` attribute, so the OS itself offers
    // the gallery alongside the camera.
    importRef.current?.click();
  }

  function onImportChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (importRef.current) importRef.current.value = "";
    if (!f) return;
    void onImportFile(f);
  }

  async function onShutterPress() {
    try {
      const native = await takePhotoIfNative();
      if (native) {
        addPhoto(native);
        return;
      }
    } catch {
      showToast("Couldn't open camera. Try again.");
    }
    fileRef.current?.click();
  }

  function removePhoto(index: number) {
    setPreviewUrls((prev) => {
      URL.revokeObjectURL(prev[index]);
      return prev.filter((_, i) => i !== index);
    });
    setPhotos((prev) => prev.filter((_, i) => i !== index));
  }

  function reset() {
    previewUrls.forEach((url) => URL.revokeObjectURL(url));
    setPhotos([]);
    setPreviewUrls([]);
    if (videoUrl) URL.revokeObjectURL(videoUrl);
    setVideo(null);
    setVideoUrl(null);
    if (videoRef.current) videoRef.current.value = "";
    if (importRef.current) importRef.current.value = "";
    setOrigin(null);
    setAsking(null);
    setNote("");
    setSex(null);
    setEarNotch(null);
    setCondition(null);
    setMoreOpen(false);
    if (fileRef.current) fileRef.current.value = "";
  }

  async function submit() {
    if (photos.length === 0 && !video) return;
    setSubmitting(true);

    // An imported photo brought its own when and where. Asking the device again
    // would overwrite them with here-and-now -- and `captured_at`/`geog` are
    // the two inputs to the 1km candidate search, so that inserts a phantom
    // into the spatial prior for wherever this phone is standing.
    let capturedAt: string;
    let lat: number | undefined;
    let lng: number | undefined;
    let accuracy: number | undefined;
    let geoSource: GeoSource;
    if (origin) {
      ({ captured_at: capturedAt, lat, lng, geo_accuracy_m: accuracy } = origin);
      geoSource = origin.geo_source;
    } else {
      capturedAt = new Date().toISOString();
      const position = await getLocation();
      lat = position?.coords.latitude;
      lng = position?.coords.longitude;
      accuracy = position?.coords.accuracy;
      geoSource = position ? "device_gps" : "none";
    }

    const input = {
      photos: video ? undefined : photos,
      video: video ?? undefined,
      lat,
      lng,
      geo_accuracy_m: accuracy,
      geo_source: geoSource,
      captured_at: capturedAt,
      note: note || undefined,
      sex: sex || undefined,
      ear_notch: earNotch || undefined,
      condition: condition || undefined,
    };

    try {
      await enqueue(input);
      showToast("Sighting logged 🐾");
      reset();
      flush().catch(() => {});
    } catch {
      showToast("Couldn't save. Try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="capture-stage">
      <div className="preview-frame">
        {videoUrl ? (
          <video src={videoUrl} controls muted playsInline />
        ) : previewUrls.length > 0 ? (
          <img src={previewUrls[previewUrls.length - 1]} alt="captured dog" />
        ) : (
          <div className="placeholder">
            <DogSprite coat="tan" scale={7} />
            <p>Spot an indie? Snap a photo.</p>
          </div>
        )}
        <div className="vf-chrome" aria-hidden="true">
          <span className="vf-corner vf-c1" />
          <span className="vf-corner vf-c2" />
          <span className="vf-corner vf-c3" />
          <span className="vf-corner vf-c4" />
          <span className="vf-rec">
            <i className="vf-dot" />
            REC
          </span>
          <span className="vf-batt">▮▮▮▯</span>
          <span className="vf-stamp">SP · {new Date().toLocaleString()} · GPS</span>
        </div>
      </div>

      <input
        ref={fileRef}
        type="file"
        accept="image/*"
        capture="environment"
        aria-label="capture photo"
        style={{ display: "none" }}
        onChange={onFileChosen}
      />
      <input
        ref={videoRef}
        type="file"
        accept="video/*"
        capture="environment"
        aria-label="record clip"
        style={{ display: "none" }}
        onChange={onVideoChosen}
      />
      {/* No `capture` attribute, unlike the two above: that attribute forces
          the camera and hides the gallery, which is the whole point here. */}
      <input
        ref={importRef}
        type="file"
        accept="image/*"
        aria-label="choose from photos"
        style={{ display: "none" }}
        onChange={onImportChosen}
      />

      {photos.length === 0 && !video ? (
        <div className="shutter-wrap">
          <span className="spot-label">SPOT AN INDIE</span>
          <button className="shutter" onClick={onShutterPress} aria-label="Spot a sighting">
            📷
          </button>
          <p className="hint">Tap to open camera</p>
          {/* A few seconds of video identifies a dog far better than one frame:
              measured 37% top-1 from a single photo against 83% from eight. */}
          <button
            type="button"
            className="link-btn"
            onClick={() => videoRef.current?.click()}
          >
            or record a short clip
          </button>
          {/* For a dog you photographed before you had the app. The photo keeps
              its own date and place rather than being logged as here-and-now. */}
          <button type="button" className="link-btn" onClick={onImportPress}>
            or add one from your photos
          </button>
        </div>
      ) : (
        <>
          {/* A clip and photos are alternatives, but both are "evidence in
              hand" and share everything below -- the note field and the save
              button. Keeping the clip inside this branch is the whole point:
              its own branch had no way to submit. */}
          {video ? (
            <div className="clip-ready">
              <span className="spot-label">CLIP READY</span>
              {/* This used to promise the clip was never stored, which is no
                  longer true: it is kept so a better model can re-read the
                  original footage later. Saying so plainly matters more than
                  the reassurance did -- a clip records more of a street than a
                  still does. */}
              <p className="hint">
                We'll keep the clearest frames, and the clip itself, so we can
                re-check it as our matching improves.
              </p>
              <button type="button" className="link-btn" onClick={removeVideo}>
                Remove clip
              </button>
            </div>
          ) : (
            <>
              <div className="filmstrip">
                {previewUrls.map((url, i) => (
                  <div className="filmstrip-thumb" key={url}>
                    <img src={url} alt={`captured dog ${i + 1}`} />
                    <button
                      type="button"
                      className="thumb-remove"
                      onClick={() => removePhoto(i)}
                      aria-label={`Remove photo ${i + 1}`}
                    >
                      ×
                    </button>
                  </div>
                ))}
                {/* Not offered for an import: the staged photo carries its own
                    date and place, and a live camera photo added beside it
                    would be from a different time and street. One import is
                    one sighting. */}
                {photos.length < MAX_PHOTOS && !origin && (
                  <button
                    type="button"
                    className="filmstrip-add"
                    onClick={onShutterPress}
                    aria-label="Add another photo"
                  >
                    +
                  </button>
                )}
              </div>
              {photos.length >= MAX_PHOTOS && (
                <p className="hint">
                  {MAX_PHOTOS}/{MAX_PHOTOS} photos added
                </p>
              )}
            </>
          )}

          {origin && (
            <p className="hint import-badge">
              from your photos ·{" "}
              {new Date(origin.captured_at).toLocaleString()}
              {origin.geo_source === "none" ? " · no place" : ""}
            </p>
          )}

          <div className="note-field">
            <textarea
              rows={2}
              placeholder="Add a note (optional) — e.g. friendly, limping, near the tea stall…"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
          </div>

          <button
            type="button"
            className="more-toggle"
            onClick={() => setMoreOpen((v) => !v)}
          >
            {moreOpen ? "▾" : "▸"} tell us more (optional)
          </button>

          {moreOpen && (
            <div className="more-fields">
              <div className="field-group">
                <label>sex</label>
                <Chips options={SEX_OPTIONS} value={sex} onChange={setSex} />
              </div>
              <div className="field-group">
                <label>ear-notch (sterilized?)</label>
                <Chips options={EAR_NOTCH_OPTIONS} value={earNotch} onChange={setEarNotch} />
              </div>
              <div className="field-group">
                <label>condition</label>
                <Chips options={CONDITION_OPTIONS} value={condition} onChange={setCondition} />
              </div>
            </div>
          )}

          <div className="actions-row">
            <button className="btn btn-secondary" onClick={reset} disabled={submitting}>
              CLEAR ALL
            </button>
            <button
              className="btn btn-primary"
              onClick={() => submit()}
              disabled={submitting}
            >
              {submitting ? <span className="spinner" /> : "LOG IT"}
            </button>
          </div>
        </>
      )}

      {asking && (
        <ImportOriginPrompt
          md={asking.md}
          getPosition={async () => {
            const pos = await getLocation();
            return pos
              ? { lat: pos.coords.latitude, lng: pos.coords.longitude }
              : null;
          }}
          onConfirm={(o) => {
            stageImport(asking.file, o);
            setAsking(null);
          }}
          onCancel={() => setAsking(null)}
        />
      )}

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
