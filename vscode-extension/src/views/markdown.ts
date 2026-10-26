/**
 * Markdown for the agent's answers in the sidebar, safe by construction.
 *
 * An answer is model output (from the coordinator's engine or from a volunteer's coding member), so it is not
 * trusted. Everything in it is HTML-escaped before any Markdown is read, and the only markup in the result is the
 * fixed set of tags made here (p, br, h3-h6, ul, ol, li, pre, code, strong, em, del, blockquote, hr), none of
 * which carries an attribute: no links, no images, no classes or styles from the text. A link is shown as its
 * text followed by the address in brackets.
 *
 * The sidebar page runs this very function: sidebarProvider puts its source into the page's script. So it must
 * use nothing from outside its own body (no imports, no module-level helpers).
 */
export function renderMarkdown(source: string): string {
  const escapeHtml = (text: string): string =>
    text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  // Code spans are set aside while the rest of a line is read, so that what is inside them is not Markdown.
  const held: string[] = [];
  const hold = (html: string): string => '\u0000' + (held.push(html) - 1) + '\u0000';

  const inline = (text: string): string => {
    let out = escapeHtml(text);
    out = out.replace(/`([^`\n]+)`/g, (_match, code: string) => hold('<code>' + code + '</code>'));
    out = out.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, '$1 ($2)');
    out = out.replace(/\*\*(?=\S)([^*\n]*?\S)\*\*/g, '<strong>$1</strong>');
    out = out.replace(/__(?=\S)([^_\n]*?\S)__/g, '<strong>$1</strong>');
    // Single * or _ only between word boundaries, so snake_case names and 2*3*4 stay as they are.
    out = out.replace(/(^|[^*\w])\*(?=\S)([^*\n]*?\S)\*(?![*\w])/g, '$1<em>$2</em>');
    out = out.replace(/(^|[^_\w])_(?=\S)([^_\n]*?\S)_(?![_\w])/g, '$1<em>$2</em>');
    out = out.replace(/~~(?=\S)([^~\n]*?\S)~~/g, '<del>$1</del>');
    return out;
  };

  const lines = String(source ?? '').replace(/\u0000/g, '').replace(/\r\n?/g, '\n').split('\n');
  const blocks: string[] = [];
  let paragraph: string[] = [];
  let list: { tag: 'ul' | 'ol'; items: string[] } | null = null;

  const flushParagraph = (): void => {
    if (paragraph.length > 0) {
      blocks.push('<p>' + paragraph.map(inline).join('<br>') + '</p>');
      paragraph = [];
    }
  };
  const flushList = (): void => {
    if (list) {
      blocks.push('<' + list.tag + '>' + list.items.map((item) => '<li>' + item + '</li>').join('') + '</' + list.tag + '>');
      list = null;
    }
  };
  const flush = (): void => {
    flushParagraph();
    flushList();
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // A fenced code block. One that is not closed yet (the answer is still arriving) runs to the end.
    const fence = /^\s*(`{3,}|~{3,})/.exec(line);
    if (fence) {
      flush();
      const code: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trimStart().startsWith(fence[1])) {
        code.push(lines[i]);
        i++;
      }
      blocks.push('<pre><code>' + escapeHtml(code.join('\n')) + '</code></pre>');
      continue;
    }

    if (!line.trim()) {
      flush();
      continue;
    }

    // Headings start at h3: the sidebar is narrow, and an answer's "#" is a section, not a page title.
    const heading = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (heading) {
      flush();
      const level = Math.min(6, heading[1].length + 2);
      blocks.push('<h' + level + '>' + inline(heading[2]) + '</h' + level + '>');
      continue;
    }

    if (/^\s{0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush();
      blocks.push('<hr>');
      continue;
    }

    const item = /^\s*([-*+]|\d{1,9}[.)])\s+(.*)$/.exec(line);
    if (item) {
      flushParagraph();
      const tag = /\d/.test(item[1]) ? 'ol' : 'ul';
      if (!list || list.tag !== tag) {
        flushList();
        list = { tag, items: [] };
      }
      list.items.push(inline(item[2]));
      continue;
    }

    const quote = /^\s{0,3}>\s?(.*)$/.exec(line);
    if (quote) {
      flush();
      blocks.push('<blockquote>' + inline(quote[1]) + '</blockquote>');
      continue;
    }

    // An indented line right after a list item continues that item.
    if (list && /^\s{2,}\S/.test(line)) {
      list.items[list.items.length - 1] += '<br>' + inline(line.trim());
      continue;
    }

    flushList();
    paragraph.push(line);
  }
  flush();

  return blocks.join('').replace(/\u0000(\d+)\u0000/g, (_match, index: string) => held[Number(index)] ?? '');
}
