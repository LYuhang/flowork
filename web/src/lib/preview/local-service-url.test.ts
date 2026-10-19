import { describe, expect, it } from 'vitest';
import { isLocalServiceUrl } from './local-service-url';

describe('sandbox service URLs', () => {
  it.each(['http://localhost:3000/a?q=1#part', 'https://127.0.0.1:8443', 'http://[::1]:8080'])('recognizes %s', (url) => expect(isLocalServiceUrl(url)).toBe(true));
  it.each(['http://128.0.0.1', 'https://localhost.example.com', 'http://user:pass@localhost', 'file:///tmp/x', 'http://localhost:99999', 'hello'])('leaves %s alone', (url) => expect(isLocalServiceUrl(url)).toBe(false));
});
