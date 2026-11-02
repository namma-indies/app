/**
 * Pages the server renders itself, which the PWA's service worker must let
 * through to the network rather than answer with the cached app shell.
 *
 * Without this the SW's navigateFallback serves index.html for the magic-link
 * consume redirect and the /join gate, swallowing the Set-Cookie (login
 * silently fails) and hiding the server-rendered /join page -- and, for
 * account deletion (#179), the web request page and the emailed confirmation
 * link would show the app instead of the form and the button.
 */
export const SERVER_ROUTES: RegExp[] = [/^\/auth\//, /^\/join/, /^\/account\/delete/];
