import { describe, expect, it } from "vitest";
import { SseParser } from "./sse";

/**
 * The README calls the partial-event case "the single most common SSE bug
 * there is". These are the tests that make that a statement about this code
 * rather than about SSE in general.
 */

const feed = (chunks: string[]) => {
  const p = new SseParser();
  const out = chunks.flatMap((c) => p.push(c));
  return [...out, ...p.flush()];
};

/** The single event a test expected, or a failure that says so. */
const um = (chunks: string[]) => {
  const eventos = feed(chunks);
  expect(eventos).toHaveLength(1);
  return eventos[0]!;
};

describe("frames", () => {
  it("reads one complete event", () => {
    expect(feed(['event: token\ndata: {"t":"hi"}\n\n'])).toEqual([
      { event: "token", data: '{"t":"hi"}' },
    ]);
  });

  it("reads several events out of one chunk", () => {
    const eventos = feed([
      'event: token\ndata: {"t":"a"}\n\nevent: token\ndata: {"t":"b"}\n\n',
    ]);
    expect(eventos.map((e) => e.data)).toEqual(['{"t":"a"}', '{"t":"b"}']);
  });

  it("defaults to `message` when the frame omits event:", () => {
    expect(feed(["data: plain\n\n"])).toEqual([{ event: "message", data: "plain" }]);
  });

  it("joins repeated data: lines with newlines, per spec", () => {
    expect(feed(["data: one\ndata: two\n\n"])).toEqual([
      { event: "message", data: "one\ntwo" },
    ]);
  });

  it("ignores comment lines", () => {
    // Proxies send `: keep-alive` to stop idle connections being reaped.
    // Treating one as data hands JSON.parse a string never meant for it.
    expect(feed([": keep-alive\n\n", 'data: {"t":"x"}\n\n'])).toEqual([
      { event: "message", data: '{"t":"x"}' },
    ]);
  });

  it("emits nothing for a frame with no data", () => {
    expect(feed(["event: token\n\n"])).toEqual([]);
  });
});

describe("TCP does not respect message boundaries", () => {
  it("holds a partial event until the rest arrives", () => {
    const p = new SseParser();
    expect(p.push('event: token\ndata: {"t":"wal')).toEqual([]);
    expect(p.push('k"}\n\n')).toEqual([{ event: "token", data: '{"t":"walk"}' }]);
  });

  it("survives a split inside the separator itself", () => {
    // The nastiest place for a chunk boundary: between the two newlines that
    // mark the end of the event.
    const p = new SseParser();
    expect(p.push('data: {"t":"a"}\n')).toEqual([]);
    expect(p.push('\ndata: {"t":"b"}\n\n')).toEqual([
      { event: "message", data: '{"t":"a"}' },
      { event: "message", data: '{"t":"b"}' },
    ]);
  });

  it("survives one byte at a time", () => {
    const fonte = 'event: token\ndata: {"t":"slow"}\n\nevent: done\ndata: {}\n\n';
    const p = new SseParser();
    const out = [...fonte].flatMap((c) => p.push(c));
    expect(out).toEqual([
      { event: "token", data: '{"t":"slow"}' },
      { event: "done", data: "{}" },
    ]);
  });

  it("does not lose an event the server never terminated", () => {
    // A server that closes right after its last data: line leaves a complete
    // event in the buffer. Dropping it silently loses the end of an answer.
    const p = new SseParser();
    expect(p.push('event: done\ndata: {"ok":true}')).toEqual([]);
    expect(p.flush()).toEqual([{ event: "done", data: '{"ok":true}' }]);
  });

  it("flushes nothing when the stream ended cleanly", () => {
    const p = new SseParser();
    p.push('data: {"t":"x"}\n\n');
    expect(p.flush()).toEqual([]);
  });
});

describe("line endings", () => {
  it("reads CRLF", () => {
    /**
     * THE BUG THIS FILE WAS WRITTEN TO CATCH.
     *
     * The previous parser split on "\n\n" and nothing else. A CRLF server
     * sends "\r\n\r\n", which does not contain "\n\n" — there is a \r in the
     * middle of it. So nothing ever matched, no event was ever emitted, the
     * buffer grew for the whole response, and the UI showed an empty answer
     * while the server streamed a complete one.
     */
    expect(feed(['event: token\r\ndata: {"t":"hi"}\r\n\r\n'])).toEqual([
      { event: "token", data: '{"t":"hi"}' },
    ]);
  });

  it("reads bare CR", () => {
    expect(feed(['event: token\rdata: {"t":"hi"}\r\r'])).toEqual([
      { event: "token", data: '{"t":"hi"}' },
    ]);
  });

  it("reads a stream that mixes them", () => {
    // An app server and a proxy in front of it need not agree.
    const eventos = feed(['data: a\r\n\r\ndata: b\n\n']);
    expect(eventos.map((e) => e.data)).toEqual(["a", "b"]);
  });

  it("keeps a \\r from ending up inside a value", () => {
    const e = um(['data: {"t":"x"}\r\n\r\n']);
    expect(e.data).toBe('{"t":"x"}');
    expect(JSON.parse(e.data).t).toBe("x");
  });
});

describe("field parsing", () => {
  it("strips exactly one leading space, not the rest", () => {
    // The interviewer streams words with their spaces attached: a token of
    // " me" is not the same as "me". Over-trimming runs every word together.
    const e = um(['data: {"t":"  two"}\n\n']);
    expect(JSON.parse(e.data).t).toBe("  two");
  });

  it("accepts a field with no space after the colon", () => {
    expect(feed(["data:tight\n\n"])).toEqual([{ event: "message", data: "tight" }]);
  });

  it("keeps colons inside the value", () => {
    const e = um(['data: {"url":"https://x.test/a"}\n\n']);
    expect(JSON.parse(e.data).url).toBe("https://x.test/a");
  });

  it("ignores fields it does not know", () => {
    // `id:` and `retry:` are spec fields this app has no use for. Unknown
    // fields must be skipped, not treated as data.
    expect(feed(["id: 7\nretry: 3000\ndata: x\n\n"])).toEqual([
      { event: "message", data: "x" },
    ]);
  });
});
