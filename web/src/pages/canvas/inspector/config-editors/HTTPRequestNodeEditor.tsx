/**
 * HTTPRequestNode config editor.
 *
 * Engine schema: `{ method, url, headers?, body?, auth?, timeout }`.
 *
 * `url` and header values accept `{{var}}` interpolation slots — the
 * engine's `_interpolate` substitutes them from `inputs`.
 *
 * `body` is the engine's `{ format, content }` envelope (only sent for
 * POST/PUT): format 'json' sends `content` as the JSON body, format 'form'
 * sends it as form-encoded fields. `auth` is the engine's quick-auth config
 * (bearer / api_key / basic); the engine turns it into the right header and
 * merges it under any explicit headers.
 */
import { useTranslation } from 'react-i18next';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { CommitOnBlurInput, CommitOnBlurNumber } from '@/pages/canvas/inspector/CommitOnBlur';
import { JsonField } from './JsonField';
import { KeyValueTable } from './KeyValueTable';
import type { NodeConfigEditorProps } from './types';

const METHODS = ['GET', 'POST', 'PUT', 'DELETE'];
const BODY_METHODS = new Set(['POST', 'PUT']);

// Radix Select items cannot use an empty-string value, so "none" is a
// sentinel that maps to "no auth" / "no body" (the key is deleted from config).
const NONE = 'none';
const AUTH_TYPES = ['bearer', 'api_key', 'basic'];
const BODY_FORMATS = ['json', 'form'];

type Dict = Record<string, unknown>;

export function HTTPRequestNodeEditor({
  config,
  readOnly,
  onChange,
  workflowSettings,
}: NodeConfigEditorProps) {
  const { t } = useTranslation();
  const method =
    typeof config.method === 'string' ? (config.method as string) : 'GET';
  const url = typeof config.url === 'string' ? (config.url as string) : '';
  const timeout =
    typeof config.timeout === 'number' ? (config.timeout as number) : undefined;

  const auth = (config.auth as Dict | undefined) ?? {};
  const authType = typeof auth.type === 'string' ? (auth.type as string) : NONE;
  const authStr = (k: string) => (typeof auth[k] === 'string' ? (auth[k] as string) : '');

  const body = (config.body as Dict | undefined) ?? {};
  const bodyFormat =
    typeof body.format === 'string' ? (body.format as string) : config.body ? 'json' : NONE;

  const setAuthType = (next: string) => {
    const cfg = { ...config };
    if (next === NONE) delete cfg.auth;
    else cfg.auth = { ...auth, type: next };
    onChange(cfg);
  };
  const patchAuth = (k: string, v: string) =>
    onChange({ ...config, auth: { ...auth, [k]: v } });

  const setBodyFormat = (next: string) => {
    const cfg = { ...config };
    if (next === NONE) delete cfg.body;
    else cfg.body = { ...body, format: next, content: body.content ?? {} };
    onChange(cfg);
  };
  const setBodyContent = (content: unknown) =>
    onChange({
      ...config,
      body: { ...body, format: bodyFormat === NONE ? 'json' : bodyFormat, content: content ?? {} },
    });

  const showBody = BODY_METHODS.has(method);

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-[110px_1fr] items-center gap-2">
        <div className="space-y-1">
          <Label className="text-xs">method</Label>
          <Select
            value={method}
            onValueChange={(next) => {
              const updated: Dict = { ...config, method: next };
              if (!BODY_METHODS.has(next)) delete updated.body;
              onChange(updated);
            }}
            disabled={readOnly}
          >
            <SelectTrigger className="h-8 text-xs">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {METHODS.map((m) => (
                <SelectItem key={m} value={m} className="text-xs">
                  {m}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1">
          <Label className="text-xs">url</Label>
          <CommitOnBlurInput
            value={url}
            onCommit={(next) => onChange({ ...config, url: next })}
            disabled={readOnly}
            placeholder="https://example.com/{{var}}"
            className="h-8 text-xs"
          />
        </div>
      </div>

      <KeyValueTable
        label={t('inspector.config.http.headersLabel', 'headers (optional)')}
        value={config.headers}
        readOnly={readOnly}
        data-testid="cfg-http-headers"
        onChange={(next) => {
          const cfg = { ...config };
          if (next === undefined) delete cfg.headers;
          else cfg.headers = next;
          onChange(cfg);
        }}
      />

      {/* Auth — the engine builds the matching header (bearer / api_key / basic). */}
      <div className="space-y-1.5">
        <Label className="text-xs">
          {t('inspector.config.http.authLabel', 'auth (optional)')}
        </Label>
        <Select value={authType} onValueChange={setAuthType} disabled={readOnly}>
          <SelectTrigger className="h-8 text-xs" data-testid="cfg-http-auth-type">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={NONE} className="text-xs">
              {t('inspector.config.http.authNone', 'none')}
            </SelectItem>
            {AUTH_TYPES.map((a) => (
              <SelectItem key={a} value={a} className="text-xs">
                {a}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {authType === 'bearer' && (
          <CommitOnBlurInput
            value={authStr('token')}
            onCommit={(next) => patchAuth('token', next)}
            disabled={readOnly}
            placeholder="token  (supports {{var}})"
            className="h-8 text-xs"
            data-testid="cfg-http-auth-token"
          />
        )}
        {authType === 'api_key' && (
          <div className="grid grid-cols-2 gap-2">
            <CommitOnBlurInput
              value={authStr('header_name')}
              onCommit={(next) => patchAuth('header_name', next)}
              disabled={readOnly}
              placeholder={t('canvas.http.headerNamePlaceholder', 'Header name (for example, X-API-Key)')}
              className="h-8 text-xs"
              data-testid="cfg-http-auth-header"
            />
            <CommitOnBlurInput
              value={authStr('key')}
              onCommit={(next) => patchAuth('key', next)}
              disabled={readOnly}
              placeholder="key  (supports {{var}})"
              className="h-8 text-xs"
              data-testid="cfg-http-auth-key"
            />
          </div>
        )}
        {authType === 'basic' && (
          <div className="grid grid-cols-2 gap-2">
            <CommitOnBlurInput
              value={authStr('username')}
              onCommit={(next) => patchAuth('username', next)}
              disabled={readOnly}
              placeholder="username  (supports {{var}})"
              className="h-8 text-xs"
              data-testid="cfg-http-auth-user"
            />
            <CommitOnBlurInput
              value={authStr('password')}
              onCommit={(next) => patchAuth('password', next)}
              disabled={readOnly}
              placeholder="password  (supports {{var}})"
              className="h-8 text-xs"
              data-testid="cfg-http-auth-pass"
            />
          </div>
        )}
      </div>

      {/* Body — only sent for POST/PUT, as a { format, content } envelope. */}
      {showBody && (
        <div className="space-y-1.5">
          <Label className="text-xs">
            {t('inspector.config.http.bodyLabel', 'body (optional)')}
          </Label>
          <Select value={bodyFormat} onValueChange={setBodyFormat} disabled={readOnly}>
            <SelectTrigger className="h-8 text-xs" data-testid="cfg-http-body-format">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={NONE} className="text-xs">
                {t('inspector.config.http.bodyNone', 'none')}
              </SelectItem>
              {BODY_FORMATS.map((f) => (
                <SelectItem key={f} value={f} className="text-xs">
                  {f}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {bodyFormat === 'json' && (
            <JsonField
              label=""
              value={body.content}
              readOnly={readOnly}
              rows={4}
              onCommit={(next) => setBodyContent(next)}
              placeholder='{"key": "value"}'
            />
          )}
          {bodyFormat === 'form' && (
            <KeyValueTable
              label=""
              value={body.content}
              readOnly={readOnly}
              data-testid="cfg-http-body-form"
              onChange={(next) => setBodyContent(next ?? {})}
            />
          )}
        </div>
      )}

      <div className="space-y-1">
        <Label className="text-xs">
          {t('inspector.config.http.timeoutLabel', 'timeout (seconds)')}
        </Label>
        <CommitOnBlurNumber
          kind="float"
          step="any"
          min={1}
          value={timeout}
          placeholder={t('inspector.config.http.inheritTimeout', 'Inherit workflow settings')}
          data-testid="cfg-http-timeout"
          onClear={() => {
            const updated = { ...config };
            delete updated.timeout;
            onChange(updated);
          }}
          onCommit={(next) => onChange({ ...config, timeout: next })}
          disabled={readOnly}
          className="h-8 text-xs"
        />
        <p className="text-xs text-muted-foreground">
          {timeout !== undefined
            ? t('inspector.config.http.timeoutOverride', 'Node override: {{seconds}} s. Clear to inherit.', { seconds: timeout })
            : t('inspector.config.http.timeoutInherited', 'Inherited timeout: {{seconds}} s ({{source}}).', {
                seconds: workflowSettings?.timeouts?.http && workflowSettings.timeouts.http > 0 ? workflowSettings.timeouts.http : 30,
                source: workflowSettings?.timeouts?.http && workflowSettings.timeouts.http > 0
                  ? t('settings.workflow.title', 'Workflow settings') : t('inspector.config.engineDefault', 'Engine default'),
              })}
        </p>
      </div>
    </div>
  );
}
