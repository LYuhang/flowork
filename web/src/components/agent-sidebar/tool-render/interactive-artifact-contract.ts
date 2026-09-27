import type { MergedToolCall } from '@/components/agent-sidebar/types';
import { parseEnvelope } from '@/components/agent-sidebar/tool-render/parseEnvelope';
import viewSchema from './interactive-view-schema.generated.json';

export type ComponentType =
  | 'approval'
  | 'html_preview'
  | 'file_preview'
  | 'url_preview'
  | 'workflow_preview'
  | 'user_input';

export type CompletionMode = 'render_only' | 'wait_for_submit';

export interface InteractiveArtifact {
  kind: 'interactive_artifact';
  schema_version?: number;
  artifact_id?: string;
  title?: string;
  component_type?: ComponentType;
  props?: Record<string, unknown>;
  interaction_schema?: Record<string, unknown>;
  completion_mode?: CompletionMode;
  height?: number;
  preview?: { mode?: 'none' | 'optional' | 'preferred' };
  widget_state?: Record<string, unknown>;
  hitl_request_id?: string | null;
  interaction_state?: {
    is_interacted?: boolean;
    status?: string;
    result?: Record<string, unknown>;
  };
}

export interface ReadInteractiveArtifactResult {
  artifact: InteractiveArtifact | null;
  ref?: string;
  previewOnly?: boolean;
}

function isObject(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x);
}

interface ViewValidationError {
  instancePath: string;
  message: string;
}

interface ViewPropertySchema {
  pattern?: string;
  const?: unknown;
  minLength?: number;
  type?: string;
}

interface ViewObjectSchema {
  additionalProperties?: boolean;
  properties?: Record<string, ViewPropertySchema>;
  required?: string[];
  type?: string;
}

interface ViewSchemaDocument {
  $defs?: Record<string, ViewObjectSchema>;
  discriminator?: { propertyName?: string };
  oneOf?: Array<{ $ref?: string }>;
}

const rendererSchema = viewSchema as ViewSchemaDocument;

function resolveLocalDefinition(ref: string | undefined): ViewObjectSchema | null {
  const prefix = '#/$defs/';
  if (!ref?.startsWith(prefix)) return null;
  return rendererSchema.$defs?.[ref.slice(prefix.length)] ?? null;
}

/**
 * Validate the generated renderer contract without runtime code generation.
 *
 * Ajv's default browser compiler relies on `new Function`, which requires the
 * broad CSP `unsafe-eval` permission. The generated view contract is a small,
 * flat discriminated union, so this fail-closed interpreter implements only
 * the schema vocabulary emitted for that boundary: local `$ref`, `oneOf`,
 * discriminator, object/string types, required fields, const, minLength and
 * `additionalProperties: false`.
 */
function validateView(value: unknown): ViewValidationError[] {
  if (!isObject(value)) {
    return [{ instancePath: '/view', message: 'must be an object' }];
  }

  const discriminator = rendererSchema.discriminator?.propertyName;
  if (!discriminator) {
    return [{ instancePath: '/view', message: 'has no discriminator contract' }];
  }
  const discriminatorValue = value[discriminator];
  const candidates = (rendererSchema.oneOf ?? [])
    .map((entry) => resolveLocalDefinition(entry.$ref))
    .filter((entry): entry is ViewObjectSchema => entry !== null);
  const schema = candidates.find(
    (entry) => entry.properties?.[discriminator]?.const === discriminatorValue,
  );
  if (!schema) {
    return [{ instancePath: `/${discriminator}`, message: 'must select a supported renderer' }];
  }
  if (schema.type !== 'object' || !schema.properties) {
    return [{ instancePath: '/view', message: 'uses an unsupported schema shape' }];
  }

  const errors: ViewValidationError[] = [];
  for (const field of schema.required ?? []) {
    if (!(field in value)) {
      errors.push({ instancePath: `/${field}`, message: 'is required' });
    }
  }
  for (const [field, fieldValue] of Object.entries(value)) {
    const property = schema.properties[field];
    if (!property) {
      if (schema.additionalProperties === false) {
        errors.push({ instancePath: `/${field}`, message: 'is not allowed' });
      }
      continue;
    }
    if (property.type === 'string' && typeof fieldValue !== 'string') {
      errors.push({ instancePath: `/${field}`, message: 'must be a string' });
      continue;
    }
    if ('const' in property && fieldValue !== property.const) {
      errors.push({ instancePath: `/${field}`, message: `must equal ${String(property.const)}` });
    }
    if (typeof fieldValue === 'string' && property.pattern && !new RegExp(property.pattern).test(fieldValue)) {
      errors.push({ instancePath: `/${field}`, message: 'has an invalid format' });
    }
    if (
      typeof fieldValue === 'string'
      && typeof property.minLength === 'number'
      && fieldValue.length < property.minLength
    ) {
      errors.push({ instancePath: `/${field}`, message: `must contain at least ${property.minLength} character` });
    }
  }
  return errors;
}

function formatErrors(errors: ViewValidationError[]): string {
  return errors
    .slice(0, 4)
    .map((error) => `${error.instancePath || '/view'} ${error.message || 'is invalid'}`)
    .join('; ');
}

/**
 * The single frontend render preflight. Inline rendering and Preview-pane
 * discovery both call this function; grouping deliberately does not, because
 * backend-successful artifacts stay standalone even when this check fails.
 */
export function interactiveArtifactRenderError(
  artifact: InteractiveArtifact | null,
  previewOnly = false,
): string | null {
  if (!artifact) return 'artifact data is missing';
  if (artifact.kind !== 'interactive_artifact') return 'artifact kind is unsupported';
  if (previewOnly) {
    const preview = artifact.props?.preview;
    return typeof artifact.artifact_id === 'string'
      && typeof artifact.component_type === 'string'
      && typeof preview === 'string'
      ? null
      : 'offloaded artifact preview is incomplete';
  }
  if (typeof artifact.component_type !== 'string' || !isObject(artifact.props)) {
    return 'component type or props are missing';
  }
  if (artifact.component_type === 'approval') {
    return Array.isArray(artifact.props.fields) ? null : 'approval fields are missing';
  }
  if (artifact.component_type === 'user_input') {
    if (artifact.props.mode === 'choices') {
      const questions = artifact.props.questions;
      if (!Array.isArray(questions) || questions.length !== 1 || !isObject(questions[0])) return 'choice question is missing';
      const options = questions[0].options;
      if (!Array.isArray(options) || !options.length || options.some((option) => !isObject(option)
        || typeof option.value !== 'string' || !option.value || typeof option.label !== 'string' || !option.label)) return 'choice options are invalid';
      if (new Set(options.map((option) => option.value)).size !== options.length) return 'choice option IDs must be unique';
    }
    const hasQuestions = Array.isArray(artifact.props.questions);
    const hasUrl = typeof artifact.props.url === 'string' && artifact.props.url.length > 0;
    return hasQuestions || hasUrl ? null : 'user input questions are missing';
  }
  if (artifact.component_type === 'file_preview') {
    // A file card is deliberately a non-executing summary. Persisted
    // artifacts may contain additive metadata (for example an older `mime`
    // hint), but the Preview resolver remains authoritative for type
    // detection, authorization and content validation after the user opens
    // the file. Do not turn a valid path into a heavy inline error card merely
    // because the producer and the current tool schema differ on metadata.
    const allowedFields = new Set([
      'path',
      'file_type',
      'description',
      // Older persisted artifacts used these content hints. They remain
      // non-authoritative; the Preview resolver detects the actual type.
      'mime',
      'content_type',
    ]);
    const unsupportedField = Object.keys(artifact.props)
      .find((field) => !allowedFields.has(field));
    if (unsupportedField) return `/${unsupportedField} is not allowed`;

    const path = artifact.props.path;
    if (
      typeof path !== 'string'
      || path.length === 0
      || !path.startsWith('/')
      || path.split('/').includes('..')
    ) {
      return 'file preview path is missing or invalid';
    }
    const fileType = artifact.props.file_type;
    if (fileType !== undefined && (typeof fileType !== 'string' || fileType.length === 0)) {
      return 'file preview type must be a non-empty string';
    }
    for (const field of ['description', 'mime', 'content_type'] as const) {
      const value = artifact.props[field];
      if (value !== undefined && typeof value !== 'string') {
        return `/${field} must be a string`;
      }
    }
    return null;
  }
  const view = { ...artifact.props, type: artifact.component_type };
  const errors = validateView(view);
  return errors.length === 0
    ? null
    : formatErrors(errors) || 'view does not match the renderer contract';
}

export function readInteractiveArtifact(call: MergedToolCall): ReadInteractiveArtifactResult {
  const payload = isObject(call.artifact?.payload) ? call.artifact.payload : null;
  const direct = payload?.artifact;
  if (isObject(direct) && direct.kind === 'interactive_artifact') {
    return { artifact: direct as unknown as InteractiveArtifact };
  }

  const envelope = parseEnvelope(call.result);
  const data = envelope?.output?.data;
  if (isObject(data) && data.kind === 'interactive_artifact') {
    return { artifact: data as unknown as InteractiveArtifact, ref: envelope?.output?.path };
  }

  const preview = payload?.artifact_preview ?? data;
  if (isObject(preview)) {
    return {
      artifact: {
        kind: 'interactive_artifact',
        artifact_id: typeof preview.artifact_id === 'string' ? preview.artifact_id : undefined,
        title: typeof preview.title === 'string' ? preview.title : undefined,
        component_type:
          typeof preview.component_type === 'string'
            ? (preview.component_type as ComponentType)
            : undefined,
        props: { preview: preview.preview },
      },
      ref:
        typeof payload?.artifact_ref === 'string'
          ? payload.artifact_ref
          : envelope?.output?.path,
      previewOnly: true,
    };
  }

  return { artifact: null };
}
