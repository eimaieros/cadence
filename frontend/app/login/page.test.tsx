// @vitest-environment jsdom
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AnchorHTMLAttributes } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

/*
  The sign-in form, tested the way a person uses it: by label, by role and by
  what appears on screen — never by component state or class names.

  The API module is mocked, but `ApiError` stays the real class. The page tells
  a wrong password from a network failure with `instanceof`, and a test that
  replaced the class would be testing its own stub.
*/

const mocks = vi.hoisted(() => ({
  router: { push: vi.fn() },
  api: { login: vi.fn(), register: vi.fn() },
  auth: { set: vi.fn() },
}));

vi.mock("next/navigation", () => ({ useRouter: () => mocks.router }));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: mocks.api, auth: mocks.auth };
});

import { ApiError } from "@/lib/api";
import LoginPage from "./page";

const TOKENS = {
  access_token: "access",
  refresh_token: "refresh",
  token_type: "bearer" as const,
  expires_in: 1800,
};

beforeEach(() => {
  vi.clearAllMocks();
});

async function fill(email: string, password: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Email"), email);
  await user.type(screen.getByLabelText("Password"), password);
  return user;
}

describe("signing in", () => {
  it("rejects a malformed email without calling the API", async () => {
    render(<LoginPage />);
    const user = await fill("not-an-email", "long enough password");

    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(screen.getByRole("alert")).toHaveTextContent("That doesn't look like an email address.");
    expect(mocks.api.login).not.toHaveBeenCalled();
  });

  it("rejects a password under 10 characters", async () => {
    render(<LoginPage />);
    const user = await fill("rodrigo@example.com", "short");

    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Passwords need at least 10 characters.");
    expect(mocks.api.login).not.toHaveBeenCalled();
  });

  it("counts the password limit in UTF-8 bytes, as bcrypt does, not in characters", async () => {
    render(<LoginPage />);
    // 40 characters, 80 bytes: under the character limit, over the byte limit.
    const user = await fill("rodrigo@example.com", "é".repeat(40));

    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Passwords can be at most 72 UTF-8 bytes.");
    expect(mocks.api.login).not.toHaveBeenCalled();
  });

  it("stores the tokens and goes to the sessions list", async () => {
    mocks.api.login.mockResolvedValue(TOKENS);
    render(<LoginPage />);
    const user = await fill("rodrigo@example.com", "long enough password");

    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(mocks.api.login).toHaveBeenCalledWith("rodrigo@example.com", "long enough password");
    expect(mocks.auth.set).toHaveBeenCalledWith(TOKENS);
    expect(mocks.router.push).toHaveBeenCalledWith("/sessions");
  });

  it("submits with Enter from the password field", async () => {
    mocks.api.login.mockResolvedValue(TOKENS);
    render(<LoginPage />);
    const user = await fill("rodrigo@example.com", "long enough password");

    await user.keyboard("{Enter}");

    expect(mocks.api.login).toHaveBeenCalledOnce();
  });

  it("says plainly when the email and password don't match", async () => {
    mocks.api.login.mockRejectedValue(new ApiError("Invalid credentials", 401));
    render(<LoginPage />);
    const user = await fill("rodrigo@example.com", "long enough password");

    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That email and password don't match an account.",
    );
    expect(mocks.router.push).not.toHaveBeenCalled();
  });

  it("tells a network failure apart from a rejected login", async () => {
    mocks.api.login.mockRejectedValue(new TypeError("fetch failed"));
    render(<LoginPage />);
    const user = await fill("rodrigo@example.com", "long enough password");

    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Couldn't reach the server. Is the API running on port 8000?",
    );
  });

  it("locks the button while the request is in flight, so a double click sends once", async () => {
    let finish: (value: typeof TOKENS) => void = () => {};
    mocks.api.login.mockReturnValue(new Promise((resolve) => (finish = resolve)));
    render(<LoginPage />);
    const user = await fill("rodrigo@example.com", "long enough password");

    await user.click(screen.getByRole("button", { name: "Sign in" }));
    const busy = screen.getByRole("button", { name: "Working…" });
    expect(busy).toBeDisabled();
    await user.click(busy);

    expect(mocks.api.login).toHaveBeenCalledOnce();
    finish(TOKENS);
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeEnabled();
  });
});

describe("registering", () => {
  it("asks for a name and explains an account that already exists", async () => {
    mocks.api.register.mockRejectedValue(new ApiError("Conflict", 409));
    render(<LoginPage />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Need an account? Create one" }));
    await user.type(screen.getByLabelText("Name"), "Rodrigo");
    await user.type(screen.getByLabelText("Email"), "rodrigo@example.com");
    await user.type(screen.getByLabelText("Password"), "long enough password");
    await user.click(screen.getByRole("button", { name: "Create account" }));

    expect(mocks.api.register).toHaveBeenCalledWith(
      "rodrigo@example.com",
      "long enough password",
      "Rodrigo",
    );
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "An account with those details already exists. Sign in instead.",
    );
  });

  it("clears the previous error when you switch between signing in and registering", async () => {
    render(<LoginPage />);
    const user = await fill("not-an-email", "long enough password");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByRole("alert")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Need an account? Create one" }));

    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Name")).toBeInTheDocument();
  });
});
