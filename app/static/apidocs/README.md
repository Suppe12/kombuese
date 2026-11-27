# API documentation page

Served by `server.py` at `/api/docs` (only the files listed in `H.DOCS_FILES`), description from `app/openapi.py`.

- `index.html`, `init.js`, `docs.css`: Kombüse's own page (AGPL-3.0-or-later like the app). No inline script or
  style, the page's CSP forbids them.
- `swagger-ui-bundle.js`, `swagger-ui.css`, `swagger-ui-bundle.js.LICENSE.txt`, `LICENSE`, `NOTICE`: unchanged from
  the npm package [swagger-ui-dist](https://www.npmjs.com/package/swagger-ui-dist) **5.36.0** (Apache-2.0,
  © SmartBear Software).

Update: `npm pack swagger-ui-dist@<version>`, copy those five files over, change the version above, check
`/api/docs` in the browser (console free of CSP errors, *Try it out* with a session and with a token).
