import { describe, expect, it } from "vitest";
import { SERVER_ROUTES } from "./serverRoutes";

const served = (path: string) => SERVER_ROUTES.some((r) => r.test(path));

describe("pages the service worker leaves to the server", () => {
  it("leaves published policy redirects and support to the server", () => {
    for (const path of ["/support", "/support?from=store", "/policies/terms", "/policies/privacy",
      "/policies/community-guidelines", "/policies/grievance"]) {
      // Workbox matches pathname + search, including store attribution queries.
      expect(served(path)).toBe(true);
    }
    expect(served("/supporting")).toBe(false);
  });
  it("keeps the public grievance form and receipts on the network", () => {
    expect(served("/grievances")).toBe(true);
    expect(served("/grievances/anything")).toBe(true);
    expect(served("/grievances?sighting=abc")).toBe(true);
    expect(served("/grievances-other")).toBe(false);
  });
  it("leaves every server page to the network when it carries a query string", () => {
    // Workbox tests pathname + search, so an anchored route must allow "?".
    for (const path of ["/auth/email/consume?token=x", "/join?signup=1", "/account/delete?source=web",
      "/policies/terms?from=store", "/policies?x=1", "/support?from=store", "/grievances?x=1",
      "/grievances/receipt?id=1"]) {
      expect(served(path)).toBe(true);
    }
  });
  it("includes the account deletion pages (#179)", () => {
    expect(served("/account/delete")).toBe(true);
    expect(served("/account/delete/confirm?token=abc")).toBe(true);
  });

  it("still includes sign-in, and not the app itself", () => {
    expect(served("/auth/email/consume?token=x")).toBe(true);
    expect(served("/join")).toBe(true);
    expect(served("/")).toBe(false);
    expect(served("/index.html")).toBe(false);
  });
});
