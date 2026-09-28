// Boots Swagger UI on /api/docs. A static file rather than an inline script so
// the page needs no CSP exception; see templates/api_docs.html.
(function () {
  var el = document.getElementById('swagger-ui');
  if (!el) return;

  if (typeof window.SwaggerUIBundle !== 'function') {
    // The bundle is downloaded when the image is built. Running from a plain
    // checkout it is simply not there, and a blank page would look like a bug.
    el.textContent = 'Swagger UI is not installed. It is added when the Docker image is built; ' +
      'the schema itself is at ' + el.dataset.openapiUrl + '.';
    return;
  }

  window.SwaggerUIBundle({
    url: el.dataset.openapiUrl,
    domNode: el,
    // The default sends the schema to validator.swagger.io for a badge — an
    // outside request the CSP would block anyway, and not one to make at all.
    validatorUrl: null,
    // Keep the API key in memory only. Persisting it would leave the admin
    // credential in localStorage for any later script on this origin to read.
    persistAuthorization: false,
    deepLinking: false,
  });
})();
