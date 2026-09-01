import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, auth } from "./api";
import type { TokenPair } from "./schemas";

const ORIGINAL: TokenPair = {
  access_token: "access-old",
  refresh_token: "refresh-old",
  token_type: "bearer",
  expires_in: 1800,
};

const ROTATED: TokenPair = {
  access_token: "access-new",
  refresh_token: "refresh-new",
  token_type: "bearer",
  expires_in: 1800,
};

const USER = {
  id: "1c08679e-a87e-482f-b205-a2b9ced4fbd7",
  email: "rodrigo@example.com",
  display_name: "Rodrigo",
  created_at: "2026-08-31T00:00:00Z",
};

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

describe("rotating browser session", () => {
  beforeEach(() => auth.clear());
  afterEach(() => vi.unstubAllGlobals());

  it("keeps both halves of the token pair", () => {
    auth.set(ORIGINAL);
    expect(auth.get()).toBe("access-old");
    expect(auth.getRefresh()).toBe("refresh-old");
  });

  it("refreshes once after a 401 and retries with the rotated access token", async () => {
    auth.set(ORIGINAL);
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json({ detail: "expired" }, 401))
      .mockResolvedValueOnce(json(ROTATED))
      .mockResolvedValueOnce(json(USER));
    vi.stubGlobal("fetch", fetchMock);

    await expect(api.me()).resolves.toEqual(USER);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(String(fetchMock.mock.calls[1]?.[0]).endsWith("/auth/refresh")).toBe(true);
    expect(auth.get()).toBe("access-new");
    expect(auth.getRefresh()).toBe("refresh-new");
  });

  it("collapses concurrent 401s into one refresh request", async () => {
    auth.set(ORIGINAL);
    let release!: (response: Response) => void;
    const pendingRefresh = new Promise<Response>((resolve) => {
      release = resolve;
    });
    let refreshCalls = 0;

    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/auth/refresh")) {
        refreshCalls += 1;
        return pendingRefresh;
      }
      const supplied = new Headers(init?.headers).get("Authorization");
      return supplied === "Bearer access-new" ? json(USER) : json({ detail: "expired" }, 401);
    });
    vi.stubGlobal("fetch", fetchMock);

    const first = api.me();
    const second = api.me();
    await vi.waitFor(() => expect(refreshCalls).toBe(1));
    release(json(ROTATED));

    await expect(Promise.all([first, second])).resolves.toEqual([USER, USER]);
    expect(refreshCalls).toBe(1);
  });

  it("clears the session when rotation is rejected", async () => {
    auth.set(ORIGINAL);
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(json({ detail: "expired" }, 401))
        .mockResolvedValueOnce(json({ detail: "replayed" }, 401)),
    );

    await expect(api.me()).rejects.toBeInstanceOf(ApiError);
    expect(auth.get()).toBeNull();
    expect(auth.getRefresh()).toBeNull();
  });
});
