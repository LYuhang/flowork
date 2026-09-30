/** Normalize TeX delimiters outside fenced and inline code. Incomplete streamed
 * expressions remain literal until their closing delimiter arrives. */
export function normalizeMathDelimiters(source: string): string {
  const convert = (text: string) => text
    .replace(/(?<!\\)\\\[([\s\S]*?)(?<!\\)\\\]/g, (_match, value: string) => `\n$$\n${value.trim()}\n$$\n`)
    .replace(/(?<!\\)\\\(([\s\S]*?)(?<!\\)\\\)/g, (_match, value: string) => `$${value}$`);
  const outsideCode = (text: string) => {
    let result = '', start = 0;
    const ticks = /`+/g;
    let match: RegExpExecArray | null;
    while ((match = ticks.exec(text))) {
      const close = new RegExp(`(?<!\x60)${match[0]}(?!\x60)`, 'g');
      close.lastIndex = ticks.lastIndex;
      const end = close.exec(text);
      if (!end) continue;
      result += convert(text.slice(start, match.index)) + text.slice(match.index, close.lastIndex);
      start = ticks.lastIndex = close.lastIndex;
    }
    return result + convert(text.slice(start));
  };
  let fence: {char: string; length: number} | null = null, prose = '', output = '';
  for (const line of source.split(/(?<=\n)/)) {
    const marker = /^ {0,3}(`{3,}|~{3,})/.exec(line)?.[1];
    if (!fence && marker) {
      output += outsideCode(prose); prose = ''; fence = {char:marker[0],length:marker.length}; output += line;
    } else if (fence) {
      output += line;
      if (marker?.[0] === fence.char && marker.length >= fence.length && /^ {0,3}(?:`+|~+)\s*$/.test(line)) fence = null;
    } else prose += line;
  }
  return output + outsideCode(prose);
}
export const hasMarkdownMath = (source: string) => /\$|\\[([]|^\s*```(?:math|latex)\b/m.test(source);
