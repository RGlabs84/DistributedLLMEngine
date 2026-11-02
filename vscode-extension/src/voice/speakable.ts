/**
 * Conversation mode (2.8.2, docs/voice-contract.md): what is read aloud. Pure text work, no process and no VS Code:
 * `speakable` turns the agent's Markdown into plain speech lines, `splitSentences` cuts them, and `TurnReader` hands out
 * the sentences of ONE reply as it streams (new complete sentences only; the end flushes the rest), with the contract's
 * 1,500-characters-a-turn limit. The shared test vectors of the contract are in test/voice-speakable.test.js.
 */

export const CODE_SENTENCE = "There's code in the chat.";
export const TABLE_SENTENCE = "There's a table in the chat.";
export const PICTURE_SENTENCE = "I made a picture. It's in the chat.";
export const REST_SENTENCE = 'The rest is in the chat.';
/** The most characters read aloud in one turn. */
export const MAX_SPOKEN_CHARS = 1500;

const ABBREVIATIONS = new Set(['e.g', 'i.e', 'vs', 'mr', 'mrs', 'ms', 'dr', 'prof', 'st', 'cf', 'approx', 'fig', 'no']);

/**
 * Markdown to speech lines, one per line of the Markdown that has words. Fenced code becomes one "There's code in the
 * chat." per block, a table one "There's a table in the chat."; inline code keeps its text; links keep their text and
 * bare URLs go; heading, list, quote and emphasis marks and citation marks like [1] go; spaces collapse.
 */
export function speakable(markdown: string): string {
  const out: string[] = [];
  const lines = String(markdown).replace(/\r\n?/g, '\n').split('\n');
  let fence: string | null = null;
  let inTable = false;
  for (const raw of lines) {
    const fenceMatch = /^\s{0,3}(`{3,}|~{3,})/.exec(raw);
    if (fence !== null) {
      if (fenceMatch && fenceMatch[1][0] === fence[0] && fenceMatch[1].length >= fence.length && /^\s{0,3}[`~]+\s*$/.test(raw)) fence = null;
      continue;
    }
    if (fenceMatch) {
      fence = fenceMatch[1];
      inTable = false;
      out.push(CODE_SENTENCE);
      continue;
    }
    if (/^\s*\|.*\|?\s*$/.test(raw) && raw.includes('|')) {
      if (!inTable) out.push(TABLE_SENTENCE);
      inTable = true;
      continue;
    }
    inTable = false;
    let line = raw
      .replace(/^\s{0,3}#{1,6}\s+/, '')
      .replace(/^\s*>+\s?/, '')
      .replace(/^\s*(?:[-*+]|\d+[.)])\s+/, '')
      .replace(/^\s*([-*_])(?:\s*\1){2,}\s*$/, '')
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/\[\d+(?:[,\s-]+\d+)*\]/g, '')
      .replace(/https?:\/\/[^\s)]*[^\s).,;:!?]/g, '')
      .replace(/`+([^`]*)`+/g, '$1')
      .replace(/(\*\*|__)(.+?)\1/g, '$2')
      .replace(/(\*|_)(?=\S)(.+?)(?<=\S)\1/g, '$2')
      .replace(/~~(.+?)~~/g, '$1')
      .replace(/<[^>\n]+>/g, '')
      .replace(/\s+/g, ' ')
      .replace(/\s+([.,!?;:])/g, '$1')
      .trim();
    // Marks left over from a half-streamed line.
    line = line.replace(/[*_`#]+(?=\s|$)/g, '').replace(/^[*_`]+/, '').trim();
    if (line) out.push(line);
  }
  return out.join('\n');
}

/** Cuts speech lines into sentences: after . ! ? (and any closing quote or bracket) followed by space, and at every newline. Never inside 3.5 or after "e.g.". */
export function splitSentences(text: string): string[] {
  const sentences: string[] = [];
  for (const line of String(text).split('\n')) {
    let start = 0;
    const re = /[.!?]+["')\]]*(?=\s|$)/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(line)) !== null) {
      const end = m.index + m[0].length;
      const word = (/(\S+)$/.exec(line.slice(start, m.index)) ?? [, ''])[1].replace(/^[("'[]+/, '').toLowerCase();
      if (m[0][0] === '.' && m[0].length === 1 && ABBREVIATIONS.has(word)) continue;
      const piece = line.slice(start, end).trim();
      if (piece) sentences.push(piece);
      start = end;
    }
    const rest = line.slice(start).trim();
    if (rest) sentences.push(rest);
  }
  return sentences;
}

/** The agent's prose only: tool-call blocks (fenced JSON, <tool_call> tags), finished or still arriving, are cut out. */
export function proseOnly(text: string): string {
  return String(text)
    .replace(/```(?:json|tool_call)?\s*\{\s*"name"\s*:[\s\S]*?```/g, '')
    .replace(/<tool_call[\s\S]*?<\/tool_call>/g, '')
    .replace(/```(?:json|tool_call)?\s*\{\s*"name"\s*:[\s\S]*$/, '')
    .replace(/<tool_call[\s\S]*$/, '')
    .replace(/<\/?t(?:o(?:o(?:l(?:_(?:c(?:a(?:ll?)?)?)?)?)?)?)?$/, '');
}

/**
 * The sentences of `soFar` that are complete and beyond the first `spoken` ones. While the reply streams the last
 * sentence is complete only when what follows it has arrived (a space or a newline), because "3." may become "3.5";
 * `final` says no more is coming, so the rest is complete too.
 */
export function newSentences(soFar: string, spoken: number, final: boolean): string[] {
  const prose = proseOnly(soFar);
  const all = splitSentences(speakable(prose));
  let complete = all.length;
  if (!final && all.length > 0) {
    const endsOpen = !/\s$/.test(prose) || !(/[.!?]["')\]]*\s*$/.test(prose) || /\n\s*$/.test(prose));
    if (endsOpen) complete -= 1;
  }
  return all.slice(Math.max(0, spoken), Math.max(complete, 0));
}

/** One spoken turn: feed it the reply so far, get back what to say next, within the turn's character limit. */
export class TurnReader {
  private spoken = 0;
  private chars = 0;
  private capped = false;
  private pictureSaid = false;

  constructor(private readonly maxChars: number = MAX_SPOKEN_CHARS) {}

  private take(sentences: string[]): string[] {
    const say: string[] = [];
    for (const s of sentences) {
      this.spoken++;
      if (this.capped) continue;
      if (this.chars + s.length > this.maxChars) {
        this.capped = true;
        say.push(REST_SENTENCE);
        continue;
      }
      this.chars += s.length;
      say.push(s);
    }
    return say;
  }

  /** The reply text so far (all of it, not just the new part); `final` once the reply is over. */
  feed(soFar: string, final = false): string[] {
    return this.take(newSentences(soFar, this.spoken, final));
  }

  /** A picture was made (the reply says so; the picture itself is in the chat). */
  picture(): string[] {
    if (this.pictureSaid) return [];
    this.pictureSaid = true;
    return [PICTURE_SENTENCE];
  }
}
