import { describe, expect, it } from "vitest";
import { esc, popupHtml } from "./DogMap";

// The popup is built as an HTML string and handed to setHTML(), so every
// interpolated value is an injection site. The note is typed by whoever
// logged the sighting; the photo URL and tags are server-supplied but still
// land inside attributes. The previous version escaped only "<" in the note,
// which stops a tag but not an attribute break-out.
describe("popup escaping", () => {
  it("escapes the characters that break out of text and attributes", () => {
    expect(esc(`<b>&"'`)).toBe("&lt;b&gt;&amp;&quot;&#39;");
  });

  it("renders a note containing markup as visible text, not markup", () => {
    const html = popupHtml({
      thumb: "",
      time: "1 Aug",
      note: '<img src=x onerror=alert(1)> a "quoted" note',
      tags: "",
    });
    expect(html).not.toContain("<img src=x");
    expect(html).toContain("&lt;img src=x onerror=alert(1)&gt;");
    expect(html).toContain("&quot;quoted&quot;");
  });

  it("does not let a photo URL escape its attribute", () => {
    const html = popupHtml({
      thumb: '" onerror="alert(1)',
      time: "1 Aug",
      note: "",
      tags: "",
    });
    expect(html).not.toContain('onerror="alert(1)');
    expect(html).toContain("&quot; onerror=&quot;alert(1)");
  });

  it("omits the note and tag blocks entirely when there is nothing to show", () => {
    const html = popupHtml({ thumb: "", time: "1 Aug", note: "", tags: "" });
    expect(html).not.toContain("popup-note");
    expect(html).not.toContain("popup-tags");
  });
});

// Attribution: Akash's call is that who logged a sighting appears on tapping
// it, never on the pin. So the name lives in the popup only.
describe("popup attribution", () => {
  it("names who logged someone else's sighting", () => {
    const html = popupHtml({
      thumb: "",
      time: "1 Aug",
      note: "",
      tags: "",
      observer: "Aswin",
    });
    expect(html).toContain("logged by Aswin");
  });

  it("says nothing about the observer on your own sightings", () => {
    const html = popupHtml({ thumb: "", time: "1 Aug", note: "", tags: "", observer: "" });
    expect(html).not.toContain("popup-by");
  });

  it("escapes a display name — it is user-supplied at /join", () => {
    // Observers type their own name into the passcode form, so this string is
    // no more trustworthy than the note.
    const html = popupHtml({
      thumb: "",
      time: "1 Aug",
      note: "",
      tags: "",
      observer: '<img src=x onerror=alert(1)>',
    });
    expect(html).not.toContain("<img src=x");
    expect(html).toContain("&lt;img src=x");
  });
})

// --- coarsened sightings ----------------------------------------------------
// Another observer's sighting comes back snapped to the centre of a grid cell.
// The popup has to say so: someone reading this map is deciding whether they
// can go and find this dog, and for a sighting that is not theirs the honest
// answer is "not from here".
describe("precision", () => {
  it("says the position is approximate when the server coarsened it", () => {
    const html = popupHtml({
      thumb: "",
      time: "1 Aug",
      note: "",
      tags: "",
      precision: "area",
      approx_km: "1",
    });
    expect(html).toContain("popup-approx");
    expect(html).toContain("somewhere in this ~1 km area");
  });

  it("claims nothing extra about your own sightings", () => {
    const html = popupHtml({
      thumb: "",
      time: "1 Aug",
      note: "",
      tags: "",
      precision: "exact",
      approx_km: "",
    });
    expect(html).not.toContain("popup-approx");
  });

  it("reports the radius the server actually used, not a hardcoded one", () => {
    const html = popupHtml({
      thumb: "",
      time: "1 Aug",
      note: "",
      tags: "",
      precision: "area",
      approx_km: "2.5",
    });
    expect(html).toContain("~2.5 km");
  });

  it("still escapes the radius, which reaches the DOM as text", () => {
    const html = popupHtml({
      thumb: "",
      time: "1 Aug",
      note: "",
      tags: "",
      precision: "area",
      approx_km: "<img src=x>",
    });
    expect(html).not.toContain("<img src=x>");
  });
});

// --- reporting --------------------------------------------------------------
// The report affordance lives in the popup because that is where a sighting
// that is not yours is actually looked at. It carries the id it would report,
// and one delegated listener on the map container reads it back -- popups are
// created and destroyed as markers scroll in and out of view, so per-popup
// listeners would leak with them.
describe("report button", () => {
  const base = { thumb: "", time: "1 Aug", note: "", tags: "" };

  it("offers reporting on someone else's sighting", () => {
    const html = popupHtml({ ...base, observer: "Priya", id: "abc-123" });
    expect(html).toContain('data-report="abc-123"');
  });

  it("does not offer it on your own", () => {
    const html = popupHtml({ ...base, observer: "", id: "abc-123" });
    expect(html).not.toContain("popup-report");
  });

  it("omits it when there is no id to report", () => {
    const html = popupHtml({ ...base, observer: "Priya" });
    expect(html).not.toContain("popup-report");
  });

  it("does not let a sighting id break out of the attribute", () => {
    const html = popupHtml({ ...base, observer: "Priya", id: '" onclick="alert(1)' });
    expect(html).not.toContain('onclick="alert(1)"');
    expect(html).toContain("&quot; onclick=&quot;alert(1)");
  });
});
