// @vitest-environment jsdom
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Typewriter } from "./Typewriter";

function prefersReducedMotion(reduce: boolean) {
  // jsdom implements no media queries at all; the component asks for exactly one.
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: reduce && query === "(prefers-reduced-motion: reduce)",
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("Typewriter", () => {
  it("types the line one character per tick", () => {
    prefersReducedMotion(false);
    const { container } = render(<Typewriter lines={["Hello"]} speed={10} hold={100} />);
    const caret = () => container.querySelector(".caret")?.textContent;

    expect(caret()).toBe("");
    act(() => vi.advanceTimersByTime(10));
    expect(caret()).toBe("H");
    act(() => vi.advanceTimersByTime(40));
    expect(caret()).toBe("Hello");
  });

  it("shows the whole line at once when the visitor asks for reduced motion", () => {
    prefersReducedMotion(true);
    const { container } = render(<Typewriter lines={["Hello"]} speed={10} hold={100} />);

    expect(container.querySelector(".caret")?.textContent).toBe("Hello");
  });
});
