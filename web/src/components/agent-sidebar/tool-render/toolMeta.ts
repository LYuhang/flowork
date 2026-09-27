/**
 * Tool `name` → friendly presentation metadata (icon + i18n label key).
 *
 * The collapsed tool-call header shows a calm, human-language chip rather
 * than the raw machine tool name. This map provides the icon
 * and a translatable label per known tool; unknown tools fall back to a
 * generic wrench + the raw name (see `getToolMeta`).
 *
 * Keep this list aligned with tools that can actually be emitted. Unknown or
 * historical tools use the generic wrench and their raw name.
 */
import type { LucideIcon } from 'lucide-react';
import {
  Bot,
  Camera,
  Clock,
  Columns,
  CornerDownLeft,
  Eye,
  FilePen,
  FileText,
  FolderTree,
  Globe,
  Image,
  ListChecks,
  ListFilter,
  MousePointerClick,
  MoveVertical,
  PenLine,
  Play,
  Power,
  Search,
  Terminal,
  Type,
  Wrench,
  Zap,
} from 'lucide-react';

export interface ToolMeta {
  icon: LucideIcon;
  /** i18n key under the `tool.meta.*` namespace. */
  labelKey: string;
}

const TOOL_META: Record<string, ToolMeta> = {
  shell: { icon: Terminal, labelKey: 'tool.meta.shell' },
  run_command: { icon: Terminal, labelKey: 'tool.meta.shell' },
  read_file: { icon: FileText, labelKey: 'tool.meta.read_file' },
  write_file: { icon: FilePen, labelKey: 'tool.meta.write_file' },
  ls: { icon: FolderTree, labelKey: 'tool.meta.ls' },
  list_files: { icon: FolderTree, labelKey: 'tool.meta.ls' },
  grep: { icon: Search, labelKey: 'tool.meta.grep' },
  run_subagent: { icon: Bot, labelKey: 'tool.meta.run_subagent' },
  // ── Run / test the workflow ────────────────────────────────────────────
  run_workflow: { icon: Play, labelKey: 'tool.meta.run_workflow' },
  node_execute: { icon: Zap, labelKey: 'tool.meta.node_execute' },
  // ── Media (vision) ─────────────────────────────────────────────────────
  read_images: { icon: Image, labelKey: 'tool.meta.read_images' },
  // ── Conversation-native Preview artifacts ─────────────────────────────
  render_interactive: { icon: Eye, labelKey: 'tool.meta.render_interactive' },
  render_preview: { icon: Eye, labelKey: 'tool.meta.render_preview' },
  render_url_preview: { icon: Globe, labelKey: 'tool.meta.render_url_preview' },
  // ── Browser-automation tools ───────────────────────────────────────────
  browser_navigate: { icon: Globe, labelKey: 'tool.meta.browser_navigate' },
  browser_navigate_back: { icon: CornerDownLeft, labelKey: 'tool.meta.browser_navigate_back' },
  browser_snapshot: { icon: Eye, labelKey: 'tool.meta.browser_snapshot' },
  browser_find: { icon: Search, labelKey: 'tool.meta.browser_find' },
  browser_console_messages: { icon: Terminal, labelKey: 'tool.meta.browser_console_messages' },
  browser_network_requests: { icon: ListFilter, labelKey: 'tool.meta.browser_network_requests' },
  browser_network_request: { icon: Globe, labelKey: 'tool.meta.browser_network_request' },
  browser_take_screenshot: { icon: Camera, labelKey: 'tool.meta.browser_take_screenshot' },
  browser_wait_for: { icon: Clock, labelKey: 'tool.meta.browser_wait_for' },
  browser_tabs: { icon: Columns, labelKey: 'tool.meta.browser_tabs' },
  browser_close: { icon: Power, labelKey: 'tool.meta.browser_close' },
  browser_resize: { icon: Columns, labelKey: 'tool.meta.browser_resize' },
  browser_click: { icon: MousePointerClick, labelKey: 'tool.meta.browser_click' },
  browser_drag: { icon: MoveVertical, labelKey: 'tool.meta.browser_drag' },
  browser_drop: { icon: MoveVertical, labelKey: 'tool.meta.browser_drop' },
  browser_hover: { icon: MousePointerClick, labelKey: 'tool.meta.browser_hover' },
  browser_type: { icon: Type, labelKey: 'tool.meta.browser_type' },
  browser_fill_form: { icon: PenLine, labelKey: 'tool.meta.browser_fill_form' },
  browser_file_upload: { icon: FilePen, labelKey: 'tool.meta.browser_file_upload' },
  browser_handle_dialog: { icon: ListChecks, labelKey: 'tool.meta.browser_handle_dialog' },
  browser_select_option: { icon: ListFilter, labelKey: 'tool.meta.browser_select_option' },
  browser_press_key: { icon: CornerDownLeft, labelKey: 'tool.meta.browser_press_key' },
};

/**
 * Resolve presentation metadata for a tool name. Returns a known entry, or a
 * generic fallback whose `labelKey` is empty (signalling the caller to show
 * the raw tool name verbatim).
 */
export function getToolMeta(name: string): ToolMeta {
  return TOOL_META[name] ?? { icon: Wrench, labelKey: '' };
}
