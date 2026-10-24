import { useState } from "react";
import Moderation from "./Moderation";
import Social from "./Social";
import Stats from "./Stats";

/** Everything only a moderator sees, in one tab: the corpus in numbers, the
 * reports waiting on someone, and what goes out on @nammaindies. The tab only
 * renders for a moderator; every endpoint behind it checks the tier itself. */
export default function Admin({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [view, setView] = useState<"numbers" | "flags" | "posts">("numbers");
  return (
    <div className="admin">
      <div className="dex-toggle">
        <button className={view === "numbers" ? "active" : ""} onClick={() => setView("numbers")}>NUMBERS</button>
        <button className={view === "flags" ? "active" : ""} onClick={() => setView("flags")}>FLAGS</button>
        <button className={view === "posts" ? "active" : ""} onClick={() => setView("posts")}>POSTS</button>
      </div>
      {view === "numbers" ? <Stats onUnauthorized={onUnauthorized} />
        : view === "flags" ? <Moderation onUnauthorized={onUnauthorized} />
        : <Social onUnauthorized={onUnauthorized} />}
    </div>
  );
}
