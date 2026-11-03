export function packageDocumentURL(name: string, url: URL): string {
  const state = url.searchParams.get('commit_state');
  return '/api/ui/packages/' + encodeURIComponent(name) + (state ? '?' + new URLSearchParams({commit_state: state}) : '');
}

export function backendURL(path: string): URL {
  // Host is operator configuration, never a query parameter or request Host header.
  const base = process.env.TRACKER_API_URL || 'http://127.0.0.1:18731';
  return new URL(path, base);
}
export async function api<T>(path: string, timeoutMs = 8000): Promise<{status: number; data: T | null}> {
  try {
    const response = await fetch(backendURL(path), {signal: AbortSignal.timeout(timeoutMs), redirect: 'error'});
    if (!response.ok) {
      await response.body?.cancel();
      return {status: response.status, data: null};
    }
    return {status: response.status, data: await response.json() as T};
  } catch { return {status: 503, data: null}; }
}
