# Policy links and store support

The legal documents are maintained on the public website. This app contains
links, without document text. Drafts must stay unpublished until their owners
approve them.

Aswin must configure these backend settings privately before store submission:

| Setting | Value to supply |
| --- | --- |
| `TERMS_URL` | Final published Terms of Service URL |
| `PRIVACY_URL` | Final published Privacy Policy URL (default `https://nammaindies.org/privacy`, the link the app used before) |
| `COMMUNITY_GUIDELINES_URL` | Final published Community Guidelines URL |
| `GRIEVANCE_URL` | Grievance page (default `/grievances`, the form #215 serves from this app) |
| `SUPPORT_CONTACT_NAME` | Published support team/contact name |
| `SUPPORT_CONTACT_EMAIL` | Published support inbox (default `nammaindies@gmail.com`, the address the app used before) |
| `SUPPORT_CONTACT_DETAILS` | Optional other contact details, including line breaks |

Policy URLs accept HTTPS or a path on this server. The stable app routes are
`/policies/terms`, `/policies/privacy`, `/policies/community-guidelines` and
`/policies/grievance`. Each redirects using the current configuration, without
caching the destination. An unset URL returns a page saying it is unconfigured
(404, so uptime monitors and store reviewers don't see an outage), rather than serving a draft or silently sending someone elsewhere.

The store support URL is the public app URL followed by `/support`. It works
without an account, links to all policies and the web deletion request, and
publishes the configured contact details. Empty contact settings show a visible
placeholder: the endpoint existing does not make the contact ready for a store
submission. No production configuration is changed by this work.

The app keeps these links on sign-in, the inbox confirmation, the account row,
deletion and suspended-account screens. Server-rendered sign-in and every
deletion result use the same navigation. The PWA leaves support and policy
routes to the server. The grievance pages from #215 do not yet show this
navigation; they should use `app.legal_links.legal_links_html()` and
`LEGAL_LINKS_STYLE`.

Product questions for Akash: who owns the published support inbox and grievance
response, and which final document URLs may be submitted to the stores? Legal
wording remains in #182, store submission in #185/#186, and age policy in #194.
