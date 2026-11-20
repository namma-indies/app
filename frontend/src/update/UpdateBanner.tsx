import { useEffect, useRef, useState } from "react";
import { startServiceWorker } from "./registerServiceWorker";

/** Registers the service worker and shows "New version ready" when a reload would lose work. */
export default function UpdateBanner() {
  const [visible, setVisible] = useState(false);
  const reloadNow = useRef<() => void>(() => window.location.reload());

  useEffect(() => {
    const sw = startServiceWorker(setVisible);
    reloadNow.current = sw.reloadNow;
    return sw.stop;
  }, []);

  if (!visible) return null;
  return (
    <div className="update-banner" role="status">
      <span>New version ready</span>
      <button type="button" onClick={() => reloadNow.current()}>
        Reload
      </button>
    </div>
  );
}
