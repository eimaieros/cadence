"use client";

import { useEffect, useRef, useState } from "react";

/*
  The landing page hero types a question out character by character.

  This is deliberately a visual echo of the real product rather than a
  decoration: the live interview arrives over SSE one token at a time, and the
  hero shows you that before you sign up. Same caret, same rhythm.
*/
export function Typewriter({
  lines,
  speed = 34,
  hold = 2400,
}: {
  lines: string[];
  speed?: number;
  hold?: number;
}) {
  const [text, setText] = useState("");
  const [lineIndex, setLineIndex] = useState(0);
  const reduced = useRef(false);

  useEffect(() => {
    reduced.current =
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }, []);

  useEffect(() => {
    const line = lines[lineIndex] ?? "";

    if (reduced.current) {
      setText(line);
      const hold2 = setTimeout(() => setLineIndex((i) => (i + 1) % lines.length), hold * 2);
      return () => clearTimeout(hold2);
    }

    let char = 0;
    setText("");
    const tick = setInterval(() => {
      char += 1;
      setText(line.slice(0, char));
      if (char >= line.length) {
        clearInterval(tick);
        setTimeout(() => setLineIndex((i) => (i + 1) % lines.length), hold);
      }
    }, speed);

    return () => clearInterval(tick);
  }, [lineIndex, lines, speed, hold]);

  return (
    <span className="caret">
      {text}
    </span>
  );
}
