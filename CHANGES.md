# Itegeko — finalization changelog

Your Flask backend and architecture are unchanged: same app structure,
same MongoDB collections, same Gemini integration, same Render deployment
model. Nothing below removes or replaces existing functionality — this is
bug fixes plus the features from your spec layered onto what was already
there. Full route list, template renders, and a fresh Flask test client
run were used to verify every page still returns successfully before this
was packaged (see "How this was verified" at the bottom).

## Bugs fixed (these were breaking things before this pass)

1. **Every plain HTML form was missing its CSRF token** — login, signup,
   onboarding, logout, admin document upload/delete, and upgrade all had
   `CSRFProtect(app)` enabled globally but no `csrf_token()` hidden input
   in the form. Submitting any of them returned a hard 400 "CSRF token
   missing." Confirmed with a test client before fixing, confirmed fixed
   after.
2. **`/forgot-password` 500'd** — the route existed in `auth_email.py` but
   `templates/forgot_password.html` and `templates/reset_password.html`
   were never created.
3. **`load_dotenv()` ran after `import db`** — so a local `.env` file's
   `MONGODB_URI` (and `SMTP_*`, etc.) wasn't visible yet when `db.py` read
   `os.environ` at import time. Invisible in production (Render sets real
   env vars before the process starts), but broke local development from
   `.env`. Moved `load_dotenv()` to the top of `app.py`, before any local
   module import.
4. **A MongoDB outage at boot crashed the whole app** — `client.admin
   .command("ping")` in `db.py` had no error handling, so if Mongo was
   briefly unreachable when the process started, the entire app failed to
   boot instead of degrading gracefully (the way it already does for a
   *missing* `MONGODB_URI`). Now wrapped in try/except: a failed
   connection logs an error and disables accounts/history/gazette for
   that process, exactly like the "not configured" case, instead of
   taking the app down.

## New features (mapped to your spec)

**Authentication / top nav** — Login and Sign Up buttons now always show
in the top bar for guests. Authenticated users get a profile menu
(avatar + name) with Profile, Settings, and Logout, plus an Admin
documents link if applicable.

**Guest experience** — the signup-nudge modal (backend already flagged
`show_signup_prompt` after 3 prompts, but nothing in the UI read it) now
actually opens, with Sign Up / Login / Continue as Guest, dismissible
without blocking the conversation.

**Prompt limits** — unchanged backend logic (guest popup/cap, free
daily+weekly, Pro high-ceiling), but limit-reached responses now render
as a proper card with the exact required copy and an Upgrade/Sign-up CTA
instead of a plain error bubble.

**User dashboard** — new `/profile` page: current plan, daily usage bar,
weekly usage bar, Upgrade to Pro button. `/settings` holds account
management (name, password, email verification) — this split keeps
"Profile" purely the usage dashboard your spec described, matching a
typical Settings/Profile separation.

**Chat experience** — the frontend now actually calls your existing
`/chat/stream` SSE endpoint (it was built but unused; the UI called
non-streaming `/chat`), with a frame-throttled renderer so long answers
stay smooth. Markdown rendering extended: fenced code blocks with a Copy
button, links, blockquotes, `---` rules. Added a "Clear conversation"
button distinct from "New conversation." Starter-prompt chips on the
empty state. Copy/regenerate/typing-indicator/auto-grow were already
built and are preserved as-is.

**Chat history sidebar** — search, rename, delete, and continuing a past
conversation are now wired into the sidebar for authenticated users (the
backend endpoints already existed; nothing in the UI called them).
Scoped to logged-in users only, matching "For authenticated users" in
your spec.

**Legal sources** — gazette matches were already used to ground the
prompt, but never surfaced to the user directly. Answers now return a
structured `sources` list (title + act number) shown as chips under the
response, in addition to the model being instructed to cite them in
prose.

**Email verification** — was entirely missing (only password reset
existed). Added the full flow: token generation, verification email,
`/verify-email/<token>`, and a resend action in Settings. Non-blocking by
design — an unverified account can still log in and chat; this only
confirms the address and shows a badge, since gating access on it is a
bigger behavior change than "support" implies and risks lockouts if SMTP
is misconfigured.

**Security** — `session.permanent = True` now set at login/signup so
`PERMANENT_SESSION_LIFETIME` (7 days) actually governs expiration, rather
than defaulting to a browser-session cookie. Rate limits added to
login/signup/password-change/password-reset/resend-verification.
CSRF/rate-limiter moved into `extensions.py` so blueprints can use them
without a circular import.

**Performance** — added a Mongo index on `conversation_id` (used by
load/rename/delete, previously unindexed for that lookup). Static assets
now cache for a year with a `?v=<mtime>` cache-busting query string, so
deploys can't serve a stale `style.css`.

**Accessibility** — skip-to-content link, `aria-label`s on icon-only
buttons, `role="log" aria-live="polite"` on the message thread, visible
`:focus-visible` outlines, `role="dialog"`/`aria-modal` on the guest
modal, `role="menu"` on the profile dropdown.

## New optional environment variables

Everything below is optional and degrades gracefully if unset, same
philosophy as the existing `GEMINI_API_KEY`/`MONGODB_URI` handling. See
the updated `.env.example` for the full, current list — it previously
documented `FREE_PLAN_DAILY_LIMIT=30` and `GUEST_MESSAGE_LIMIT=15` as the
defaults, which no longer matched the actual code (10 and 10); corrected.

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@example.com
SMTP_PASS=your-smtp-password-or-app-password
SMTP_FROM=noreply@itegeko.rw
```

Without these, password reset and email verification both show a plain
"email isn't configured yet" message instead of erroring.

## Follow-up fixes (after the initial pass)

**Document upload, beyond just the CSRF fix** — checked back through the
actual upload pipeline, not just the form wrapper:
- No server-side cap existed on upload size at all (`file.read()` loaded
  the whole thing into memory first, with nothing stopping an
  arbitrarily large file). Added `MAX_CONTENT_LENGTH` (30MB) plus a
  friendly "file too large" flash instead of Flask's default error page.
- An invalid/corrupted PDF previously fell through to a generic "check
  the server logs" message. Now raises a clear "that doesn't look like a
  valid PDF" error instead.
- `/admin/documents/download/<file_id>` crashed with a 500 on a
  malformed ID instead of a clean 404.

**Mobile sidebar** — on phone widths the sidebar used to switch to a
permanent 52px icon strip fixed over the left edge of the chat — it
couldn't fully go away. Rebuilt as an off-canvas drawer: fully hidden by
default, opened with one tap via a new hamburger button in the top bar,
with a backdrop that closes it on tap and on Escape. Picking a
conversation, a legal area, or "New conversation" from inside the drawer
now closes it automatically on phone-sized screens.

**Conversation sidebar pagination** — matching how Claude.ai's sidebar
behaves: `/api/conversations` now takes `limit`/`offset` and returns
`has_more`, defaulting to 5 per page instead of dumping up to 50 into one
scrollable box. The sidebar shows the 5 most recent conversations with a
"Show more" link underneath that loads the next 5 without reloading the
list you already have open. Renaming updates the item in place; deleting
removes just that item, both without collapsing the list back down.
Search still returns its full match set directly (typically small,
doesn't need paging).

## Files added

`extensions.py`, `auth_utils.py`, `account.py` (Profile/Settings routes),
`templates/forgot_password.html`, `templates/reset_password.html`,
`templates/verify_email.html`, `templates/profile.html`,
`templates/settings.html`.

## How this was verified

No live Gemini/MongoDB access exists in the environment this was built
in, so nothing here was tested against your real data — you'll still
want to click through it yourself after deploying. What *was* verified
locally, in a mode that mirrors your app's own graceful-degradation path
for a missing database:

- Every `.py` file compiles (`py_compile`) and the full app imports
  cleanly with no circular imports.
- Every route resolves and returns a non-5xx status from a real Flask
  test client, including the ones that previously 400'd or 500'd.
- Every template renders through the actual Jinja environment (not just
  read for syntax) with representative context, both as a guest and as a
  logged-in user, including the two conditional branches of `index.html`.
- The inline JavaScript in `index.html` was extracted post-Jinja-render
  (both guest and authenticated variants) and passed `node --check`.
- A CSRF regression check: a form POST without a token still correctly
  gets rejected (protection intact), and the same POST with the token
  from the rendered page now passes CSRF (bug fixed) instead of the
  previous hard 400.
- The new Mongo-unreachable-at-boot handling was verified against a
  real (unroutable) address to confirm it logs and degrades in ~2s
  instead of hanging 30s or crashing the import.

What to double check on your end: an actual Gemini API round trip
(streaming + non-streaming), a real Mongo-backed signup → onboarding →
chat → history → rename/delete flow, and — if you configure SMTP — an
actual email arriving for password reset and verification.
