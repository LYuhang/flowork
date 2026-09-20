/**
 * Unit tests for the shared `errorMessage` helper.
 *
 * Covers standard and structured API errors:
 *   1. `Error` instance         → `.message`.
 *   2. `{ detail: string }`     → that string.
 *   3. `{ detail: [{msg}, …] }` → `msg` strings joined with `; `.
 *   4. Structured detail codes → localized guidance or the stable code.
 *   5. Unknown objects         → a safe generic message.
 *
 * This is the toast-text path for every mutation in the UI, so any
 * regression here surfaces in every error banner the user sees.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { errorMessage, isAuthorizationChangedError } from '@/lib/api/mutations/error-message';
import i18n from '@/lib/i18n';

beforeEach(async () => { await i18n.changeLanguage('en'); });

describe('errorMessage', () => {
  it('returns `Error.message` for Error instances', () => {
    expect(errorMessage(new Error('boom'))).toBe('boom');
  });

  it('returns the `detail` string from a FastAPI body', () => {
    expect(errorMessage({ detail: 'workflow not found' })).toBe(
      'workflow not found',
    );
  });

  it('joins a FastAPI validation-error `detail` array via `; `', () => {
    const body = {
      detail: [
        { msg: 'field required', loc: ['body', 'name'] },
        { msg: 'must be a string', loc: ['body', 'tags', 0] },
      ],
    };
    expect(errorMessage(body)).toBe('field required; must be a string');
  });

  it('falls back to String(e) for primitives', () => {
    expect(errorMessage('plain string')).toBe('plain string');
    expect(errorMessage(42)).toBe('42');
    expect(errorMessage(true)).toBe('true');
  });

  it.each(['en', 'zh'])('explains origin and CSRF failures in %s', async (locale) => {
    await i18n.changeLanguage(locale);
    expect(errorMessage({ detail: { code: 'invalid_request_origin' } })).toBe(i18n.t('api.error.invalidRequestOrigin'));
    expect(errorMessage({ detail: { code: 'csrf_validation_failed' } })).toBe(i18n.t('api.error.csrfValidationFailed'));
  });

  it('handles structured messages and unknown codes without dumping fields', () => {
    expect(errorMessage({ detail: { message: 'Too many requests', code: 'limit' } })).toBe('Too many requests');
    expect(errorMessage({ detail: { msg: 'Invalid node' } })).toBe('Invalid node');
    expect(errorMessage({ detail: { code: 'version_conflict', private: 'secret' } })).toBe('version_conflict');
    expect(errorMessage({ message: 'Service unavailable' })).toBe('Service unavailable');
    expect(errorMessage({ detail: [{ msg: 'Required' }, { private: 'secret' }] })).toBe('Required');
  });

  it.each([null, undefined, {}, { detail: {} }, { private: 'secret' }, new Error('')])('uses a safe fallback for %j', (value) => {
    expect(errorMessage(value)).toBe(i18n.t('api.error.unexpected'));
  });

  it('bounds circular errors', () => {
    const value: { detail?: unknown } = {};
    value.detail = value;
    expect(errorMessage(value)).toBe(i18n.t('api.error.unexpected'));
  });

  it('preserves authorization-change detection for structured codes', () => {
    expect(isAuthorizationChangedError({ detail: { code: 'resource_not_found' } })).toBe(true);
    expect(isAuthorizationChangedError({ detail: { code: 'permission_denied' } })).toBe(true);
    expect(isAuthorizationChangedError({ detail: { code: 'invalid_request_origin' } })).toBe(false);
  });
});
