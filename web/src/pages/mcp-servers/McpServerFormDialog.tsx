/**
 * `McpServerFormDialog` — create / edit an MCP server (MCP SP1-T7).
 *
 * One `Dialog` serves both modes — `target == null` => create, otherwise edit
 * the given server. Mirrors `CredentialFormDialog`: Label + Input + Select via
 * `@/components/ui/*`, a single short panel.
 *
 * Auth handling
 * -------------
 * The stored bearer `token` is scrubbed to `"***"` on read, so on edit the
 * token field starts EMPTY with a "leave blank to keep current" placeholder —
 * the backend keeps the stored token when omitted/blank.
 *
 * Test connection
 * ---------------
 * A "Test connection" button calls `useTestMcpServer().mutateAsync(input)` (a
 * dry-run probe — persists nothing) and shows the `{status, tool_count}`
 * result inline so the user can validate the endpoint before saving.
 */
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { toast } from 'sonner';

import { Button } from '@/components/ui/button';
import { SectionBlock } from '@/components/layout/section-block';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import {
  useCreateMcpServer,
  useTestMcpServer,
  useUpdateMcpServer,
} from '@/lib/api/queries/mcp-servers';
import type {
  McpServer,
  McpServerInput,
  McpTestResult,
} from '@/lib/api/mcp-servers';

interface McpServerFormDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** null => create mode; a row => edit that server. */
  target: McpServer | null;
}

type Transport = 'stdio' | 'sse' | 'streamable_http' | 'streamable-http' | 'http';
const NO_AUTH = '__none__';

interface FormState {
  name: string;
  tool_prefix: string;
  transport: Transport;
  endpoint: string;
  argsJson: string;
  cwd: string;
  envJson: string;
  /** Auth type: NO_AUTH or e.g. 'bearer'. */
  authType: string;
  /** Bearer token (blank on edit => keep stored). */
  token: string;
}

function emptyForm(): FormState {
  return {
    name: '',
    tool_prefix: '',
    transport: 'sse',
    endpoint: '',
    argsJson: '[]',
    cwd: '',
    envJson: '{}',
    authType: NO_AUTH,
    token: '',
  };
}

export function McpServerFormDialog({
  open,
  onOpenChange,
  target,
}: McpServerFormDialogProps) {
  const { t } = useTranslation();
  const isEdit = target != null;

  const createMutation = useCreateMcpServer();
  const updateMutation = useUpdateMcpServer();
  const testMutation = useTestMcpServer();
  const pending = createMutation.isPending || updateMutation.isPending;

  const [form, setForm] = useState<FormState>(emptyForm);
  const [testResult, setTestResult] = useState<McpTestResult | null>(null);

  // (Re)seed the form whenever the dialog opens. Create mode resets to blanks;
  // edit mode hydrates from the target (but NEVER the token — that field always
  // starts empty since the stored one is scrubbed to "***").
  useEffect(() => {
    if (!open) return;
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      setTestResult(null);
      setForm(!isEdit ? emptyForm() : {
        name: target.name,
        tool_prefix: target.tool_prefix,
        transport: target.transport,
        endpoint: target.endpoint,
        argsJson: JSON.stringify(target.connection_config?.args ?? [], null, 2),
        cwd: String(target.connection_config?.cwd ?? ''),
        envJson: JSON.stringify(target.connection_config?.env ?? {}, null, 2),
        authType:
          !target.auth_config?.type || target.auth_config.type === 'none'
            ? NO_AUTH
            : target.auth_config.type,
        token: '',
      });
    });
    return () => {
      active = false;
    };
  }, [open, isEdit, target]);

  const buildInput = (): McpServerInput => {
    const connectionConfig: Record<string, unknown> = (() => {
      if (form.transport !== 'stdio') {
        return {
          ...(target?.connection_config ?? {}),
          url: form.endpoint.trim(),
        };
      }
      let args: unknown;
      let env: unknown;
      try {
        args = JSON.parse(form.argsJson || '[]');
      } catch {
        throw new Error(t('mcp.args_invalid', 'Args must be valid JSON array.'));
      }
      try {
        env = JSON.parse(form.envJson || '{}');
      } catch {
        throw new Error(t('mcp.env_invalid', 'Environment must be valid JSON object.'));
      }
      if (!Array.isArray(args) || args.some((item) => typeof item !== 'string')) {
        throw new Error(t('mcp.args_invalid', 'Args must be valid JSON array.'));
      }
      if (
        env === null ||
        typeof env !== 'object' ||
        Array.isArray(env) ||
        Object.entries(env as Record<string, unknown>).some(
          ([key, value]) => typeof key !== 'string' || typeof value !== 'string',
        )
      ) {
        throw new Error(t('mcp.env_invalid', 'Environment must be valid JSON object.'));
      }
      return {
        command: form.endpoint.trim(),
        args,
        ...(form.cwd.trim() ? { cwd: form.cwd.trim() } : {}),
        ...(Object.keys(env as Record<string, unknown>).length > 0 ? { env } : {}),
      };
    })();
    const base: McpServerInput = {
      name: form.name.trim(),
      tool_prefix: form.tool_prefix.trim(),
      transport: form.transport,
      endpoint: form.endpoint.trim(),
      connection_config: connectionConfig,
      auth_config: { type: 'none' },
    };
    if (form.authType !== NO_AUTH && form.token) {
      base.auth_config = { type: form.authType, token: form.token };
    }
    return base;
  };

  const canSubmit = useMemo(() => {
    if (!form.name.trim()) return false;
    if (!form.tool_prefix.trim()) return false;
    if (!form.endpoint.trim()) return false;
    return true;
  }, [form]);

  const handleTest = async () => {
    setTestResult(null);
    try {
      const result = await testMutation.mutateAsync(buildInput());
      setTestResult(result);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const handleSubmit = async () => {
    try {
      const input = buildInput();
      if (isEdit) {
        // On edit, omit auth_config entirely when the token is blank so the
        // backend keeps the stored one.
        const patch: Partial<McpServerInput> = {
          name: input.name,
          endpoint: input.endpoint,
          connection_config: input.connection_config,
          ...(form.authType === NO_AUTH ? { auth_config: { type: 'none' } } : {}),
          ...(form.authType !== NO_AUTH && form.token
            ? { auth_config: input.auth_config }
            : {}),
        };
        await updateMutation.mutateAsync({ id: target.id, patch });
        toast.success(t('mcp.updated', 'MCP server updated'));
      } else {
        await createMutation.mutateAsync(input);
        toast.success(t('mcp.created', 'MCP server saved'));
      }
      onOpenChange(false);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {isEdit
              ? t('mcp.edit_title', 'Edit MCP server')
              : t('mcp.add_title', 'Add MCP server')}
          </DialogTitle>
          <DialogDescription>
            {t(
              'mcp.form_desc',
              'Connect a Model Context Protocol server so its tools become available to the agent.',
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-3">
          <SectionBlock
            title={t('mcp.form.identity', 'Identity')}
            description={t('mcp.form.identityHelp', 'Name the server and define the prefix agents use for its tools.')}
            contentClassName="space-y-4"
          >
          <div className="flex flex-col gap-1">
            <Label htmlFor="mcp-name">{t('mcp.name', 'Name')}</Label>
            <Input
              id="mcp-name"
              data-testid="mcp-name"
              required
              value={form.name}
              placeholder={t('mcp.name_ph', 'e.g. Github tools')}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
            />
          </div>

          <div className="flex flex-col gap-1">
            <Label htmlFor="mcp-prefix">
              {t('mcp.tool_prefix', 'Tool prefix')}
            </Label>
            <Input
              id="mcp-prefix"
              data-testid="mcp-prefix"
              required
              value={form.tool_prefix}
              placeholder={t('mcp.tool_prefix_ph', 'e.g. gh')}
              onChange={(e) =>
                setForm((f) => ({ ...f, tool_prefix: e.target.value }))
              }
            />
            <span className="text-xs text-muted-foreground">
              {t(
                'mcp.tool_prefix_helper',
                'Prepended to every tool name so tools from different servers never collide.',
              )}
            </span>
          </div>
          </SectionBlock>

          <SectionBlock
            title={t('mcp.form.connection', 'Connection')}
            description={t('mcp.form.connectionHelp', 'Choose how Flowork reaches this server and provide its address or command.')}
            contentClassName="space-y-4"
          >
          <div className="flex flex-col gap-1">
            <Label htmlFor="mcp-transport">
              {t('mcp.transport', 'Transport')}
            </Label>
            <Select
              value={form.transport}
              onValueChange={(v) =>
                setForm((f) => ({ ...f, transport: v as Transport }))
              }
            >
              <SelectTrigger id="mcp-transport" data-testid="mcp-transport">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="sse">
                  {t('mcp.transport_sse', 'SSE')}
                </SelectItem>
                <SelectItem value="streamable_http">
                  {t('mcp.transport_http', 'Streamable HTTP')}
                </SelectItem>
                <SelectItem value="http">
                  {t('mcp.transport_http_short', 'HTTP')}
                </SelectItem>
                <SelectItem value="stdio">
                  {t('mcp.transport_stdio', 'Stdio command')}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="flex flex-col gap-1">
            <Label htmlFor="mcp-endpoint">
              {form.transport === 'stdio'
                ? t('mcp.command', 'Command')
                : t('mcp.endpoint', 'Endpoint')}
            </Label>
            <Input
              id="mcp-endpoint"
              data-testid="mcp-endpoint"
              required
              value={form.endpoint}
              placeholder={
                form.transport === 'stdio'
                  ? t('mcp.command_ph', 'npx')
                  : t('mcp.endpoint_ph', 'https://host/mcp')
              }
              onChange={(e) =>
                setForm((f) => ({ ...f, endpoint: e.target.value }))
              }
            />
          </div>

          {form.transport === 'stdio' && (
            <>
              <div className="flex flex-col gap-1">
                <Label htmlFor="mcp-args">{t('mcp.args', 'Args')}</Label>
                <Textarea
                  id="mcp-args"
                  data-testid="mcp-args"
                  value={form.argsJson}
                  rows={3}
                  className="font-mono text-xs"
                  placeholder={'["-y", "@modelcontextprotocol/server-filesystem", "/data"]'}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, argsJson: e.target.value }))
                  }
                />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="mcp-cwd">{t('mcp.cwd', 'Working directory')}</Label>
                <Input
                  id="mcp-cwd"
                  data-testid="mcp-cwd"
                  value={form.cwd}
                  placeholder={t('mcp.cwd_ph', 'Optional')}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, cwd: e.target.value }))
                  }
                />
              </div>
              <div className="flex flex-col gap-1">
                <Label htmlFor="mcp-env">{t('mcp.env', 'Environment')}</Label>
                <Textarea
                  id="mcp-env"
                  data-testid="mcp-env"
                  value={form.envJson}
                  rows={3}
                  className="font-mono text-xs"
                  placeholder={'{"API_KEY": "..."}'}
                  onChange={(e) =>
                    setForm((f) => ({ ...f, envJson: e.target.value }))
                  }
                />
              </div>
            </>
          )}
          </SectionBlock>

          {form.transport !== 'stdio' && (
          <SectionBlock
            title={t('mcp.form.authentication', 'Authentication')}
            description={t('mcp.form.authenticationHelp', 'Credentials are encrypted after saving and are not displayed again.')}
            contentClassName="space-y-4"
          >
          <div className="flex flex-col gap-1">
            <Label htmlFor="mcp-auth">{t('mcp.auth', 'Authentication')}</Label>
            <Select
              value={form.authType}
              onValueChange={(v) => setForm((f) => ({ ...f, authType: v }))}
            >
              <SelectTrigger id="mcp-auth" data-testid="mcp-auth">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NO_AUTH}>
                  {t('mcp.auth_none', 'None')}
                </SelectItem>
                <SelectItem value="bearer">
                  {t('mcp.auth_bearer', 'Bearer token')}
                </SelectItem>
              </SelectContent>
            </Select>
          </div>


          {form.authType !== NO_AUTH && (
            <div className="flex flex-col gap-1">
              <Label htmlFor="mcp-token">{t('mcp.token', 'Token')}</Label>
              <Input
                id="mcp-token"
                data-testid="mcp-token"
                type="password"
                autoComplete="off"
                value={form.token}
                placeholder={
                  isEdit
                    ? t('mcp.token_kept_hint', 'Leave blank to keep current')
                    : t('mcp.token_ph', 'Paste the bearer token')
                }
                onChange={(e) =>
                  setForm((f) => ({ ...f, token: e.target.value }))
                }
              />
            </div>
          )}
          </SectionBlock>
          )}

          <SectionBlock
            title={t('mcp.test_title', 'Test connection')}
            description={t('mcp.form.testHelp', 'Verify the endpoint and inspect how many tools it exposes before saving.')}
            actions={(
              <Button
                type="button"
                variant="outline"
                size="sm"
                data-testid="mcp-test"
                onClick={() => void handleTest()}
                disabled={!canSubmit || testMutation.isPending}
              >
                {testMutation.isPending
                  ? t('mcp.testing', 'Testing…')
                  : t('mcp.test', 'Test')}
              </Button>
            )}
          >
            {testResult && (
              <p
                data-testid="mcp-test-result"
                className={
                  testResult.status === 'ok'
                    ? 'text-xs text-state-success'
                    : 'text-xs text-destructive'
                }
              >
                {testResult.status === 'ok'
                  ? t('mcp.test_ok', {
                      count: testResult.tool_count ?? 0,
                      defaultValue: 'OK — {{count}} tools discovered',
                    })
                  : t('mcp.test_failed', {
                      error: testResult.status,
                      defaultValue: 'Failed: {{error}}',
                    })}
              </p>
            )}
            {!testResult ? (
              <p className="text-xs text-content-tertiary">
                {t('mcp.form.testPending', 'No connection test has been run for these values yet.')}
              </p>
            ) : null}
          </SectionBlock>
        </div>

        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => onOpenChange(false)}
            disabled={pending}
          >
            {t('mcp.cancel', 'Cancel')}
          </Button>
          <Button
            type="button"
            data-testid="mcp-save"
            onClick={() => void handleSubmit()}
            disabled={!canSubmit || pending}
          >
            {pending ? t('mcp.saving', 'Saving…') : t('mcp.save', 'Save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
