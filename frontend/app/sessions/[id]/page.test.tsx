// @vitest-environment jsdom
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AnchorHTMLAttributes } from "react";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { StreamHandlers } from "@/lib/api";
import type { Scorecard, SessionDetail, Turn } from "@/lib/schemas";

/*
  The interview room: the page that consumes the SSE stream.

  The transport has its own tests (lib/sse.test.ts, 20 of them). These check
  what the person sees while it runs — the question typing itself in, the
  composer locked until it has finished, and what happens when the stream or
  the API fails halfway.

  `streamQuestion` is replaced by a stream the test drives by hand, one token at
  a time, so "while it is still arriving" is a state the test can stop in.
*/

const ID = "5d1e9a5e-0c55-4a64-9a3f-3e1b8a2f2c11";

const mocks = vi.hoisted(() => ({
  // Next returns the same router object on every render. A fresh object per
  // call would re-run the page's load effect, and its cleanup aborts the
  // stream, so the stub has to be stable too.
  router: { push: vi.fn() },
  api: {
    getSession: vi.fn(),
    cost: vi.fn(),
    submitAnswer: vi.fn(),
    complete: vi.fn(),
  },
  auth: { get: vi.fn(), clear: vi.fn() },
  streamQuestion: vi.fn(),
}));

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: ID }),
  useRouter: () => mocks.router,
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: mocks.api,
    auth: mocks.auth,
    streamQuestion: mocks.streamQuestion,
  };
});

import { ApiError } from "@/lib/api";
import InterviewRoom from "./page";

const OPENED = "2026-10-08T09:00:00Z";

const QUESTION: Turn = {
  id: "t0",
  index: 0,
  speaker: "interviewer",
  content: "Walk me through a system you've shipped that had real users on it.",
  created_at: "2026-10-08T09:00:05Z",
};

const ANSWER: Turn = {
  id: "t1",
  index: 1,
  speaker: "candidate",
  content: "The Rock in Rio Lisboa site: 1.9 million users and no downtime.",
  created_at: "2026-10-08T09:01:10Z",
};

const CARD: Scorecard = {
  overall: 72,
  summary: "You attach numbers to your claims and you explain failure modes first.",
  dimensions: [
    { name: "Technical depth", score: 4, note: "Comfortable below the framework layer." },
    { name: "Specificity", score: 2, note: "Scale arrived without the numbers behind it." },
  ],
  strengths: ["Described the failure mode before the happy path."],
  gaps: ["Close each answer with the outcome."],
  created_at: "2026-10-08T09:05:00Z",
};

function session(turns: Turn[], extra: Partial<SessionDetail> = {}): SessionDetail {
  return {
    id: ID,
    role_title: "Frontend Engineer",
    seniority: "mid",
    focus_areas: ["React"],
    status: "active",
    cost_usd: 0,
    created_at: OPENED,
    completed_at: null,
    turns,
    scorecard: null,
    ...extra,
  };
}

/** The stream the page opened, held open until the test says otherwise. */
let live: { handlers: StreamHandlers; signal?: AbortSignal } | null = null;

beforeAll(() => {
  // jsdom has no layout, so it has no scrollIntoView either. The page calls it
  // to keep the newest turn in view.
  Element.prototype.scrollIntoView = vi.fn();
});

beforeEach(() => {
  vi.clearAllMocks();
  live = null;
  mocks.auth.get.mockReturnValue("access-token");
  mocks.api.cost.mockResolvedValue({ spent_usd: 0, ceiling_usd: 0.5, remaining_usd: 0.5 });
  mocks.streamQuestion.mockImplementation(
    async (_id: string, handlers: StreamHandlers, signal?: AbortSignal) => {
      live = { handlers, signal };
    },
  );
});

const composer = () => screen.getByLabelText("Your answer");

async function opened() {
  await waitFor(() => expect(live).not.toBeNull());
  return live!;
}

describe("the question arriving", () => {
  it("types the opening question in live and keeps the composer locked until it ends", async () => {
    mocks.api.getSession.mockResolvedValueOnce(session([]));
    render(<InterviewRoom />);
    const stream = await opened();

    act(() => stream.handlers.onToken("Walk me "));
    act(() => stream.handlers.onToken("through"));

    expect(screen.getByText("Walk me through")).toBeInTheDocument();
    expect(screen.getByText("--:--")).toBeInTheDocument();
    expect(composer()).toBeDisabled();
    expect(composer()).toHaveAttribute("placeholder", "Wait for the question…");

    mocks.api.getSession.mockResolvedValueOnce(session([QUESTION]));
    mocks.api.cost.mockResolvedValueOnce({ spent_usd: 0.0004, ceiling_usd: 0.5, remaining_usd: 0.4996 });
    await act(async () => {
      stream.handlers.onDone({ index: 0, content: QUESTION.content, cost_usd: 0.0004 });
    });

    expect(await screen.findByText(QUESTION.content)).toBeInTheDocument();
    expect(screen.queryByText("--:--")).not.toBeInTheDocument();
    expect(screen.getByText("00:05")).toBeInTheDocument();
    expect(composer()).toBeEnabled();
    expect(await screen.findByText("$0.0004 / $0.50")).toBeInTheDocument();
  });

  it("shows why the stream stopped and gives the composer back", async () => {
    mocks.api.getSession.mockResolvedValueOnce(session([]));
    render(<InterviewRoom />);
    const stream = await opened();

    act(() => stream.handlers.onToken("Walk me "));
    act(() =>
      stream.handlers.onError(
        "This session has reached its spend guardrail. End it to see your scorecard.",
      ),
    );

    expect(screen.getByRole("alert")).toHaveTextContent("spend guardrail");
    expect(screen.queryByText("Walk me")).not.toBeInTheDocument();
    expect(composer()).toBeEnabled();
  });

  it("cancels the stream when you leave the room", async () => {
    mocks.api.getSession.mockResolvedValueOnce(session([]));
    const { unmount } = render(<InterviewRoom />);
    const stream = await opened();
    expect(stream.signal?.aborted).toBe(false);

    unmount();

    expect(stream.signal?.aborted).toBe(true);
  });
});

describe("answering", () => {
  it("sends the trimmed answer with Ctrl+Enter, then asks the next question", async () => {
    mocks.api.getSession.mockResolvedValueOnce(session([QUESTION]));
    render(<InterviewRoom />);
    const user = userEvent.setup();
    await screen.findByText(QUESTION.content);
    expect(mocks.streamQuestion).not.toHaveBeenCalled();

    mocks.api.submitAnswer.mockResolvedValueOnce(ANSWER);
    mocks.api.getSession.mockResolvedValueOnce(session([QUESTION, ANSWER]));
    await user.type(composer(), `  ${ANSWER.content}  `);
    await user.keyboard("{Control>}{Enter}{/Control}");

    expect(mocks.api.submitAnswer).toHaveBeenCalledWith(ID, ANSWER.content);
    expect(await screen.findByText(ANSWER.content)).toBeInTheDocument();
    await waitFor(() => expect(mocks.streamQuestion).toHaveBeenCalledOnce());
    expect(composer()).toHaveValue("");
  });

  it("keeps what you wrote, and says why, when the answer is rejected", async () => {
    mocks.api.getSession.mockResolvedValueOnce(session([QUESTION]));
    render(<InterviewRoom />);
    const user = userEvent.setup();
    await screen.findByText(QUESTION.content);

    mocks.api.submitAnswer.mockRejectedValueOnce(
      new ApiError("This session is already closed", 409),
    );
    await user.type(composer(), ANSWER.content);
    await user.click(screen.getByRole("button", { name: "Send" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("This session is already closed");
    expect(composer()).toHaveValue(ANSWER.content);
    expect(mocks.streamQuestion).not.toHaveBeenCalled();
  });

  it("ends the interview and shows the scorecard in place of the composer", async () => {
    mocks.api.getSession.mockResolvedValueOnce(session([QUESTION, ANSWER]));
    render(<InterviewRoom />);
    const user = userEvent.setup();
    await screen.findByText(ANSWER.content);

    mocks.api.complete.mockResolvedValueOnce(CARD);
    mocks.api.getSession.mockResolvedValueOnce(
      session([QUESTION, ANSWER], { status: "completed", scorecard: CARD }),
    );
    await user.click(screen.getByRole("button", { name: "End & score" }));

    expect(await screen.findByText(CARD.summary)).toBeInTheDocument();
    expect(screen.getByText("72")).toBeInTheDocument();
    expect(screen.getByLabelText("2 out of 5")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByLabelText("Your answer")).not.toBeInTheDocument());
  });
});

describe("getting in", () => {
  it("sends you to sign in when there is no token, without calling the API", async () => {
    mocks.auth.get.mockReturnValue(null);
    render(<InterviewRoom />);

    await waitFor(() => expect(mocks.router.push).toHaveBeenCalledWith("/login"));
    expect(mocks.api.getSession).not.toHaveBeenCalled();
  });

  it("does not say whether a session it won't show you exists", async () => {
    mocks.api.getSession.mockRejectedValueOnce(new ApiError("Not found", 404));
    render(<InterviewRoom />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That session doesn't exist, or it isn't yours.",
    );
    expect(mocks.streamQuestion).not.toHaveBeenCalled();
  });

  it("signs you out when the API no longer accepts the session", async () => {
    mocks.api.getSession.mockRejectedValueOnce(new ApiError("Token expired", 401));
    render(<InterviewRoom />);

    await waitFor(() => expect(mocks.router.push).toHaveBeenCalledWith("/login"));
    expect(mocks.auth.clear).toHaveBeenCalledOnce();
  });
});
