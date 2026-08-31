// CloudFront Function (viewer-request), attached ONLY to the default
// (S3) cache behavior. Rewrites extension-less paths to /index.html so
// the browser can deep-link / refresh on client-side routes
// (/servers, /pnl, ...). API behaviors (/api/*, /strategies, /health)
// have no function attached, so their status codes pass through intact
// and the auth boundary (401 from the backend) is never masked.
function handler(event) {
  var request = event.request;
  var uri = request.uri;

  // Real files (index.html, /assets/app.abc123.js, favicon.svg, ...)
  // contain a dot in the last path segment — leave them untouched.
  var lastSegment = uri.substring(uri.lastIndexOf('/') + 1);
  if (lastSegment.indexOf('.') !== -1) {
    return request;
  }

  // Everything else is an SPA route.
  request.uri = '/index.html';
  return request;
}
