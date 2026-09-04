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

## Second round: file/audio upload, lawyer directory, consent & email

**Contract/photo/audio upload in chat** — you can now attach a photo, PDF,
or audio file to a message (paperclip icon in the composer) and Gemini
reads it natively as multimodal input — no separate OCR/transcription
step. When a document is attached, Itegeko is instructed to summarize it,
assess viability/enforceability under Rwandan law, flag risky or
one-sided clauses, and cite relevant law — a direct "upload a contract,
ask if it's viable" flow. Free and guest accounts get **3 uploads total,
for the life of the account** (not daily/weekly like messages) before
being asked to upgrade; Pro is effectively unlimited. Limits are enforced
server-side (`plans.attachments_used`/`attachment_limit_for`), not just
hidden in the UI. One real constraint worth knowing: the original file
bytes aren't kept anywhere after that turn (only filename/type, so the
allowance can be tracked and the chip can be redrawn on reload) — if the
in-memory chat session gets rebuilt later (e.g. after a restart), Gemini
can no longer "see" that specific file again, only a text note that one
was attached. If you want attachments to stay fully re-viewable later,
that's a reasonable follow-up (store the bytes in GridFS like gazette
PDFs already do) but wasn't built here to keep scope matched to what was
asked.

**Law firm directory** — new "Find a lawyer" tab in the sidebar (public,
no login needed) links to `/lawyers`, a directory of real firms with
phone/address/specialty. Admin-managed only: `/admin/firms` lets an admin
add a firm (name, phone, and address required; email/website/specialty/
description optional) or remove one — no public submission path, no edit
yet (delete and re-add to correct an entry, matching what was asked).

**Password show/hide** — every password field (login, signup, reset
password, change password in Settings) now has an eye icon that toggles
between hidden and plain text while typing. One shared `static/js/auth.js`
rather than four copies of the same code.

**Terms of Service + Privacy Policy pages** — these didn't exist before,
so a "you must agree to our Terms" checkbox had nothing real to link to.
Added `/terms` and `/privacy` with real, substantive content (not just
placeholders) — the Privacy Policy specifically covers marketing email
and the future-AI-training consent described below. Worth having an
actual advocate review both before a real launch, same as any ToS/Privacy
page.

**Signup consent** — three checkboxes now on signup: agreeing to the
Terms/Privacy (required to create an account), an opt-in for occasional
marketing email, and a separate opt-in for conversations potentially
being used to help train Itegeko's models in the future. All three are
stored on the user record (`terms_accepted_at`, `marketing_consent`,
`training_consent`); the two opt-ins can be changed any time afterward
from Settings → "Email & data preferences" (new). Kept as two *separate*
checkboxes rather than one combined one — email communication preference
and data-usage rights are different kinds of consent, and bundling them
would make it impossible to opt into one without the other.

**Welcome email** — the existing verification email (sent at signup, only
when SMTP is configured) now doubles as a welcome email rather than
sending two separate emails back-to-back: warmer copy, a short "here's
what you can do" note, then the verification link. Verifying stays
optional/non-blocking as before — this only changes the email's content,
not when or whether it's sent.

## Files added (second round)

`firms.py` (law firm directory data layer), `templates/admin_firms.html`,
`templates/lawyers.html`, `templates/terms.html`, `templates/privacy.html`,
`static/js/auth.js`.

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
- Chat attachment validation was exercised directly: a real (tiny, valid)
  PNG passes through parsing/limit-checks to the AI-not-configured stage;
  an unsupported file type and an oversized file are both rejected with
  a clear 400 before any AI call is attempted.
- Signup's Terms/consent validation was verified with a mocked `db.users`
  (the offline sandbox has no real database to test against) confirming
  the "must agree to Terms" rejection actually fires, and that a valid
  signup stores `terms_accepted_at`, `marketing_consent`, and
  `training_consent` correctly before redirecting to onboarding.

What to double check on your end: an actual Gemini API round trip
(streaming + non-streaming, including a real file attachment), a real
Mongo-backed signup → onboarding → chat → history → rename/delete flow,
the full attachment-limit flow against real usage data (upload 3, confirm
the 4th is blocked, confirm Pro bypasses it), and — if you configure
SMTP — actual email delivery for the welcome/verification email and
password reset.
