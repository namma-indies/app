# Policy links and store support

The legal documents are maintained on the public website. This app contains
links, without document text. Drafts must stay unpublished until their owners
approve them.

Aswin must configure these backend settings privately before store submission:

| Setting | Value to supply |
| --- | --- |
| `TERMS_URL` | Final published Terms of Service URL |
| `PRIVACY_URL` | Final published Privacy Policy URL |
| `COMMUNITY_GUIDELINES_URL` | Final published Community Guidelines URL |
| `GRIEVANCE_URL` | Published grievance page; use `/grievances` once that route is included |
| `SUPPORT_CONTACT_NAME` | Published support team/contact name |
| `SUPPORT_CONTACT_EMAIL` | Published support inbox |
| `SUPPORT_CONTACT_DETAILS` | Optional other contact details, including line breaks |

Policy URLs accept HTTPS or a path on this server. The stable app routes are
`/policies/terms`, `/policies/privacy`, `/policies/community-guidelines` and
`/policies/grievance`. Each redirects using the current configuration, without
caching the destination. An unset URL returns a page saying it is unconfigured
(503), rather than serving a draft or silently sending someone elsewhere.

The store support URL is the public app URL followed by `/support`. It works
without an account, links to all policies and the web deletion request, and
publishes the configured contact details. Empty contact settings show a visible
placeholder: the endpoint existing does not make the contact ready for a store
submission. No production configuration is changed by this work.

The app keeps these links on sign-in, the inbox confirmation, the account row,
deletion and suspended-account screens. Server-rendered sign-in and every
deletion result use the same navigation. The PWA leaves support and policy
routes to the server. The grievance pages from #215 are on a separate launch
branch; their navigation must use `app.legal_links.legal_links_html()` and
`LEGAL_LINKS_STYLE` when that branch is synchronized.

Product questions for Akash: who owns the published support inbox and grievance
response, and which final document URLs may be submitted to the stores? Legal
wording remains in #182, store submission in #185/#186, and age policy in #194.
