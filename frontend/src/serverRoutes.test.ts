import { describe, expect, it } from "vitest";
import { SERVER_ROUTES } from "./serverRoutes";

const served = (path: string) => SERVER_ROUTES.some((r) => r.test(path));

describe("pages the service worker leaves to the server", () => {
  it("keeps the public grievance form and receipts on the network", () => {
    expect(served("/grievances")).toBe(true);
    expect(served("/grievances/anything")).toBe(true);
    expect(served("/grievances-other")).toBe(false);
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
