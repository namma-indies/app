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
