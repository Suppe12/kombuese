// API documentation of Kombüse: Swagger UI on /api/openapi.json, everything served by this server.
// "Try it out" uses the browser's session cookie; the X-Kombuese header (CSRF protection) is added here.
// A token from "Authorize" goes as Authorization: Bearer and wins over the session.
(function () {
  const de = (navigator.language || '').toLowerCase().startsWith('de');
  const T = de ? {
    hint: '„Try it out“ nutzt deine Anmeldung (die Kopfzeile X-Kombuese: 1 setzt diese Seite selbst). Für ein API-Token oben „Authorize“. Anfragen ändern echte Daten.',
    as: 'Angemeldet als ', none: 'Nicht angemeldet – ', login: 'Anmelden', token: 'API-Token'
  } : {
    hint: '“Try it out” uses your sign-in (this page adds the X-Kombuese: 1 header itself). For an API token use “Authorize”. Requests change real data.',
    as: 'Signed in as ', none: 'Not signed in – ', login: 'Sign in', token: 'API token'
  };
  document.documentElement.lang = de ? 'de' : 'en';
  document.getElementById('kmb-hint').textContent = T.hint;

  const who = document.getElementById('kmb-who');
  fetch('/api/me', { credentials: 'same-origin' }).then(r => r.ok ? r.json() : null).then(me => {
    if (me) {
      who.textContent = T.as + (me.display_name || me.username) + (me.role === 'admin' ? ' (admin)' : '');
    } else {
      who.textContent = T.none;
      const a = document.createElement('a');
      a.href = '/login';
      a.textContent = T.login;
      who.appendChild(a);
    }
  }).catch(() => {});

  window.ui = SwaggerUIBundle({
    url: '/api/openapi.json',
    dom_id: '#swagger-ui',
    deepLinking: true,
    docExpansion: 'none',
    filter: true,
    displayRequestDuration: true,
    persistAuthorization: false,  // a token stays in this tab only
    validatorUrl: null,           // never ask an outside validator
    withCredentials: true,
    requestInterceptor: (req) => {
      const u = new URL(req.url, location.href);
      if (u.origin === location.origin) {
        req.headers['X-Kombuese'] = '1';
        req.credentials = 'same-origin';
      }
      return req;
    },
  });
})();
