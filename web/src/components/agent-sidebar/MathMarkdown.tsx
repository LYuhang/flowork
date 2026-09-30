import ReactMarkdown, { type Options } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';

/** Loaded only for messages with math; raw HTML remains disabled. */
export default function MathMarkdown(props: Options) {
  return <ReactMarkdown {...props} remarkPlugins={[remarkGfm, remarkMath]}
    rehypePlugins={[[rehypeKatex, { trust: false, strict: false, maxExpand: 1000, maxSize: 20 }]]} />;
}
