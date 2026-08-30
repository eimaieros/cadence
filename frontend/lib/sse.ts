/**
 * Server-sent events, parsed as a pure function of the bytes that arrived.
 *
 * WHY THIS IS ITS OWN FILE.
 *
 * This logic used to live inside `streamQuestion`, welded to a `fetch` call and
 * a `ReadableStream`. That made it untestable without mocking the network, so
 * it was never tested — while the README singled it out as "the single most
 * common SSE bug there is". A claim about correctness, with nothing checking it.
 *
 * Pulled out here it is thirty lines that take strings and return events, and
 * the test file can feed it every way a stream can arrive badly.
 *
 * THE BUG THE EXTRACTION IMMEDIATELY EXPOSED.
 *
 * The old code split on `"\n\n"` and nothing else. The SSE specification allows
 * `\n`, `\r\n` or `\r` as a line terminator, so a server or a proxy emitting
 * CRLF sends `\r\n\r\n` between events — which does not contain `\n\n`,
 * because there is a `\r` in the middle of it.
 *
 * The failure mode is not a dropped event. It is that *no* event is ever
 * emitted: every chunk gets appended to the buffer, the buffer grows for the
 * whole response, and the UI sits there showing nothing while the server
 * cheerfully streams a complete answer. It works perfectly against uvicorn,
 * which sends `\n\n`, and would break behind an intermediary that normalises
 * line endings — the kind of thing you discover in production, from someone
 * else's infrastructure.
 */

export interface SseEvent {
  /** The `event:` field, or `"message"` when the frame omits it. */
  event: string;
  /** The `data:` field. Multiple `data:` lines are joined with newlines, per spec. */
  data: string;
}

export class SseParser {
  private buffer = "";
  private pendingCR = false;

  /**
   * Feed one chunk. Returns the events that are now complete.
   *
   * Whatever follows the last separator is a partial event and stays in the
   * buffer for the next chunk. TCP does not respect message boundaries, and
   * assuming it does is what this class exists to not do.
   */
  push(chunk: string): SseEvent[] {
    // A CRLF pair can itself be split across network chunks. Hold a trailing
    // CR until the next push so it cannot become one newline here plus a
    // second newline at the start of the next chunk (which would manufacture
    // a blank line and dispatch an event too early).
    let normalised = "";
    if (this.pendingCR) {
      normalised = "\n";
      if (chunk.startsWith("\n")) chunk = chunk.slice(1);
      this.pendingCR = false;
    }
    if (chunk.endsWith("\r")) {
      this.pendingCR = true;
      chunk = chunk.slice(0, -1);
    }
    normalised += chunk.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    this.buffer += normalised;

    const parts = this.buffer.split("\n\n");
    this.buffer = parts.pop() ?? "";

    const out: SseEvent[] = [];
    for (const part of parts) {
      const e = parseFrame(part);
      if (e) out.push(e);
    }
    return out;
  }

  /**
   * What is left when the stream ends.
   *
   * A well-behaved server terminates the last event with a blank line, so this
   * is usually empty. It is here because "usually" is not "always": a server
   * that closes the connection immediately after its final `data:` line leaves
   * a complete event sitting in the buffer, and silently dropping the last
   * answer of a conversation is a bad way to end one.
   */
  flush(): SseEvent[] {
    if (this.pendingCR) {
      this.buffer += "\n";
      this.pendingCR = false;
    }
    const resto = this.buffer;
    this.buffer = "";
    const e = resto.trim() ? parseFrame(resto) : null;
    return e ? [e] : [];
  }
}

/** One frame — the text between two blank lines — into an event, or null. */
function parseFrame(frame: string): SseEvent | null {
  let event = "message";
  const dados: string[] = [];

  for (const line of frame.split("\n")) {
    // A line starting with a colon is a comment. Servers and proxies send them
    // as keep-alives, and treating one as data would hand JSON.parse a string
    // that was never meant for it.
    if (line.startsWith(":")) continue;

    const i = line.indexOf(":");
    const campo = i === -1 ? line : line.slice(0, i);
    // Exactly one leading space is stripped, per spec — the rest belongs to
    // the value. `data: {"t":"  two spaces"}` must keep both of them.
    let valor = i === -1 ? "" : line.slice(i + 1);
    if (valor.startsWith(" ")) valor = valor.slice(1);

    if (campo === "event") event = valor.trim();
    else if (campo === "data") dados.push(valor);
  }

  if (!dados.length) return null;
  return { event, data: dados.join("\n") };
}
