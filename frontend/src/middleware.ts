import {defineMiddleware} from 'astro:middleware';
import {createHash} from 'node:crypto';
import {api, packageDocumentURL} from './lib/api';
import type {DetailDocument, ListingDocument} from './lib/document';
import {RenderCache, notModified} from './lib/render-cache.mjs';

// Byte and entry bounds cover both large pages and many tiny search results.
const rendered = new RenderCache();
export const onRequest = defineMiddleware(async (context, next) => {
  let key: string | undefined;
  if (context.request.method === 'GET') {
    if (context.url.pathname === '/') {
      context.locals.listing = await api<ListingDocument>('/api/ui/packages' + context.url.search);
    } else if (context.params.name && /^\/packages\/[^/]+\/?$/.test(context.url.pathname)) {
      context.locals.detail = await api<DetailDocument>(packageDocumentURL(context.params.name, context.url));
    }
    const document = context.locals.listing || context.locals.detail;
    // Hash the actual document, not generation: freshness and notices can change
    // without a database write. URL and cookies also affect the rendered page.
    if (document?.status === 200 && document.data) {
      key = createHash('sha256').update(JSON.stringify([
        context.url.href, context.request.headers.get('Cookie'), document.data,
      ])).digest('hex');
    }
  }
  const validator = context.request.headers.get('If-None-Match');
  const produce = async () => {
    const response = await next();
    const scripts = context.url.pathname === '/' || context.url.pathname.startsWith('/packages/')
      ? "'self'" : "'none'";
    response.headers.set('Content-Security-Policy', `default-src 'self'; script-src ${scripts}; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`);
    response.headers.set('X-Content-Type-Options', 'nosniff');
    response.headers.set('Referrer-Policy', 'no-referrer');
    response.headers.set('X-Frame-Options', 'DENY');
    response.headers.set('Cache-Control', 'no-store');
    // Revalidate actual rendered HTML, including query and theme, before reuse.
    // Never cache errors, redirects, cookie-setting responses or raw observations.
    if (context.request.method === 'GET' && response.status === 200
        && (response.headers.get('Content-Type')?.startsWith('text/html')
            || context.url.pathname === '/presentation.css')
        && !response.headers.has('Set-Cookie')) {
      const body = await response.arrayBuffer();
      const etag = `W/"${createHash('sha256').update(new Uint8Array(body)).digest('hex')}"`;
      const headers = new Headers(response.headers);
      headers.set('Cache-Control', 'private, no-cache');
      headers.set('ETag', etag);
      if (!headers.get('Vary')?.split(',').some(value => value.trim().toLowerCase() === 'cookie')) headers.append('Vary', 'Cookie');
      if (key) rendered.set(key, new Response(null, {headers}), body);
      const unchanged = notModified(headers, validator);
      if (unchanged) return unchanged;
      return new Response(body, {status: response.status, headers});
    }
    return response;
  };
  return key ? rendered.resolve(key, produce, validator) : produce();
});
