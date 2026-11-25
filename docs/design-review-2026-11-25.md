# Design Review: colloquicrm (2026-11-25)

Reviewed at commit `94e1b03`. This is a review of design complexity, not a bug
hunt: where the next change is expensive, and what to do about it in small
steps. Nothing in the code was changed. No earlier design review exists in
`docs/`, so there is no trend to compare against.

## Status (updated 2026-11-25, same day)

All eight priorities below have since been carried out; each carries a
**Status** line saying what was done and where it departs from the plan. The
findings are left as written so the before-picture stays readable — file and
line references describe the code as it was at `94e1b03`.

Checks after the work: 137 backend tests pass (101 before; six new test
modules), `ruff` passes on the app with its default rules, the web app lints
and builds, `alembic check` reports no drift, and the container image builds
and starts.

Deliberate behaviour changes — everything else is meant to be identical:

- **Shared addresses have one owner.** When two records list the same email
  or number, a person beats a lead and the older record beats the newer. The
  sync and the feed used to pick differently, each arbitrarily.
- **Notification fallback is the same everywhere.** On an install with no
  chat, a chat-preferring user with a registered device now gets assignments
  and automation notices by push, as due reminders already did.
- **A record's timeline is paged on the server** (50 at a time, "Load
  older") instead of being capped per source, and "Attach existing note"
  offers the notes loaded so far.
- **PostgreSQL only.** A SQLite `DATABASE_URL` is refused at startup.

Not done, by choice: the optional later steps (a stored `contact_points`
table; generated client types), and most of "Optional / later" below.

## Architecture summary

- **One deployable.** A single image serves the FastAPI API (`/api/v1`), the
  built React SPA, and public lead forms (`/f/{slug}`). Postgres is the only
  other service; `alembic upgrade head` runs in `entrypoint.sh`.
- **Backend** (`backend/app`, ~13k lines): `config.py` (one settings object),
  `db.py`, `deps.py` (session auth), `models.py` (all ORM models),
  `schemas.py` (request bodies only), `routes/` (24 route modules, 103
  handlers), `services/` (shared logic and integrations).
- **Core records**: Company, Person, Lead, Opportunity — all built by one
  factory, `services/crud.py::register_crud`, which gives each list / search /
  filter / sort / CSV export / bulk actions / merge / trash and restore.
- **Satellites** hang off records through an untyped `(entity_type,
  entity_id)` pair with no foreign key: Note, Task, Activity, Attachment,
  EntityTag, CustomFieldValue, and manually logged PhoneEvents.
- **Synced history** is attached by *address*, not by record: EmailMessage /
  EmailParticipant (Gmail), CalendarEvent / attendees (Google Calendar),
  PhoneEvent (RingCentral). Which record an email or call belongs to is worked
  out at read time by normalizing the record's email and phone columns.
- **Background work**: six `asyncio` loops started in `main.py`'s lifespan
  (task reminders, Google sync, RingCentral sync, trash purge, attachment
  reconcile, automations), plus import jobs that resume after a restart.
- **Integrations**: Google (OAuth, contacts, calendar, Gmail backfill),
  RingCentral, Colloqui chat, and an APNs push relay for a companion iOS app
  that lives in another repository and uses the same API.
- **Frontend** (`frontend/src`, ~9k lines of plain JS/JSX, no TypeScript):
  one fetch client (`api.js`), generic `ListPage` / `DetailShell` /
  `ProfilePanel` driven by field lists in `constants/fields.js`, and
  per-feature pages.
- **Tests**: 18 backend test modules (~3.3k lines) against a real Postgres
  with every network call faked. No frontend tests, linter, or type check.
- **Hotspots** (large and frequently changed, last 6 months):
  `services/google.py` (1,648 lines, 30 commits), `pages/Settings.jsx`
  (1,759 / 27), `routes/google.py` (844 / 25), `models.py` (736 / 20),
  `services/crud.py` (793 / 18), `components/ListPage.jsx` (660 / 15),
  `services/importer.py` (1,214 / 11).

## Overall assessment

The CRUD core is strategic: `register_crud` is a deep module, and adding a
column to a core record is nearly free on the backend. The complexity has
gathered around it, in the features that grew one request at a time: synced
email, calls, the timeline, and notifications. The biggest theme is **the same
rule written out again wherever it was needed** — "which addresses belong to
this record", "what does a timeline item look like", "how does a notification
reach a user" each exist in three to ten places, and the copies have already
started to disagree. The second theme is an **implicit API contract**: two
clients depend on response shapes that no schema, type, or test pins down.
Individual functions are careful and well commented; what is missing is a
single home for each of these rules.

## Top priorities

Suggested order: 1 → 2 → 4 fall out of each other (each shrinks
`services/google.py`); 3, 5 and 6 are independent and can be done any time.

### 1. "Which addresses belong to this record" is re-derived in about ten places

- **Where:**
  - Org-wide lookup maps, built three times: `services/google.py:560`
    (`_crm_email_map`), `services/ringcentral.py:173` (`_crm_phone_map`),
    `routes/feed.py:43` (`_org_contact_maps`).
  - Per-record address lists, written inline: `routes/google.py:536`
    (calendar), `routes/google.py:706` (emails), `routes/ringcentral.py:193`
    (calls/texts), `routes/notes.py:24` (`_event_on_note_record`),
    `services/interactions.py:92` and `:200`, `services/common.py:348`
    (`company_people`), `routes/duplicates.py:144`, `services/socials.py:125`.
  - The field list itself: `routes/people.py:35`
    (`_CONTACT_ADDRESS_FIELDS`), the `merge_pool` groups in
    `routes/people.py` and `routes/leads.py`.
  - `normalize_email` lives in the Google service and `normalize_phone` in the
    RingCentral service; nine modules import one or both, and `routes/google.py`
    imports a private helper from `routes/feed.py` (`:68`, `:616`).
- **Symptom:** information leakage, change amplification, duplicated logic.
- **Why it matters:** adding a third email or phone slot to Person (or
  letting a Company carry its own addresses) touches roughly 14 files, and
  missing one produces no error — that record's mail simply doesn't show up
  in one view. The copies already disagree: the sync maps let the *last*
  person read win a shared address (`google.py:571`, `ringcentral.py:184`,
  from an unordered query), while the feed map lets the *first* win
  (`feed.py:62`), so the same email can be credited to different people in
  the sync and in the feed. Several of these also read every person in the
  org per call (`interactions.py:200`, `ringcentral.py:245`).
- **Fix:**
  1. Create `services/contacts.py`. Move `normalize_email` and
     `normalize_phone` there (re-export from the old locations so imports keep
     working).
  2. Add `record_emails(obj)` / `record_numbers(obj)` — the one place that
     knows which columns hold addresses for a person, lead, or company — and
     switch the per-record endpoints to them one at a time.
  3. Replace the three map builders with one `ContactIndex` (the feed's is
     already the superset: it carries labels), with one stated precedence
     rule and explicit handling of shared numbers.
  4. Later, optional: store normalized addresses in a small indexed
     `contact_points` table maintained on write, which removes the whole-org
     scans. Only worth it once steps 1–3 have made it a one-module change.
- **Pain / Frequency / Effort:** High / High / M
- **Status:** Done (steps 1–3). `services/contacts.py` owns normalization,
  the address columns, per-record contact points and the org-wide index;
  every site listed above now calls it. Step 4 (a stored table) not done.
  One known inconsistency is preserved rather than fixed: a company's
  calendar events match by email domain only, while its emails also match
  its people's addresses.

### 2. The timeline is built twice, and its rows are serialized by hand in five places

- **Where:** the org feed is one server-side `UNION ALL` with hydrators
  (`routes/feed.py:85–290`). The per-record timeline is assembled in the
  browser from four separate endpoints with different caps —
  `components/NotesTimeline.jsx:35–62` and `:82–99` (notes 500, activities
  50, emails 50, calls 50). The same email row is hand-built in
  `feed.py:_hydrate_emails`, `routes/google.py:search_emails` and
  `emails_for_entity`; the phone row in `feed.py:_hydrate_phone` and
  `routes/ringcentral.py:phone_events`. `pages/Feed.jsx:133–207` and
  `NotesTimeline.jsx:245–367` each render call / sms / email / note items
  separately.
- **Symptom:** duplicated logic, tangled responsibilities (ordering and
  merging business logic in a React component).
- **Why it matters:** a new timeline source (meetings, form submissions, a
  second phone provider) or a new field on an existing item means editing two
  backend serializers, a new per-record endpoint, both React renderers, and
  the iOS app's own merge. Showing email on an opportunity is currently a
  special case in the component (`NotesTimeline.jsx:42`). Because each source
  is capped separately, a busy record's timeline has gaps that depend on
  which source ran out first, and nothing on screen says so.
- **Fix:**
  1. Extract `services/timeline.py` from `routes/feed.py`: a registry of
     sources, each with a skinny `(kind, id, ts)` select and one serializer.
     The feed route becomes a thin caller. Covered by `tests/test_feed.py`.
  2. Point `search_emails`, `emails_for_entity` and `phone_events` at those
     serializers so each row shape exists once.
  3. Add `GET /timeline?entity_type=&entity_id=` that scopes the same sources
     to one record (using `record_emails` / `record_numbers` from item 1) and
     pages on the server. Keep the old endpoints until the iOS app moves.
  4. Switch `NotesTimeline` to the one call, and extract shared
     `TimelineItem` components used by it and `Feed.jsx`.
- **Pain / Frequency / Effort:** High / Med–High / M
- **Status:** Done. `services/timeline.py` holds the sources and the row
  shapes; the feed, `GET /timeline` (new, per record), email search and the
  per-record lists all use it, and the record page makes one call. The
  feed's item shape is unchanged. Departure from step 4: the feed and the
  record timeline keep their own layouts and share the wording rules
  (`components/timelineText.js`) rather than whole components — merging the
  markup would have been a visual change with nothing to test it against.

### 3. The API contract exists only in the heads of its two clients

- **Where:** 103 route handlers, zero `response_model`s. Core records are
  serialized by `services/common.py:32` (`row_to_dict`), which ships every
  column of the row; everything else is a hand-built dict. `schemas.py`
  covers requests only. The frontend is untyped JS with no linter — 14 files
  carry `eslint-disable` comments for a tool that is not installed
  (`frontend/package.json`). Rules the iOS app depends on are recorded as
  comments next to the code they constrain (`services/crud.py:237`,
  `routes/feed.py:37`, `routes/meta.py:26`, `main.py:144`,
  `schemas.py:73`).
- **Symptom:** information leakage (response shapes known only by reading
  handlers), missing seams for testing, unclear interface.
- **Why it matters:** renaming or dropping a field in a hand-built dict fails
  nowhere — not in CI, not in the web build — and surfaces as a blank cell or
  a crash in the iOS app. In the other direction, adding any column to
  Person, Lead, Company, Opportunity, Task, Note or a custom field exposes it
  to every client automatically, which is convenient until the column is
  internal. The dev-only `/docs` page cannot describe a single response.
- **Fix:**
  1. Cheapest first: contract tests that assert the exact key set of the
     responses both clients rely on (core record, list envelope, feed item,
     email, phone event, task). A few lines each in the existing suite.
  2. Give `row_to_dict` an explicit per-model exclude list instead of only
     `org_id`, so exposing a column is a decision.
  3. Add Pydantic response models for the hand-built shapes as item 2
     consolidates them (they are then written once anyway).
  4. Install ESLint with the react-hooks plugin — the code is already written
     to its rules — and add `npm run lint` and `npm run build` to the
     `checks` job in `.github/workflows`.
  5. Optional, later: generate client types from the OpenAPI schema once
     response models exist.
- **Pain / Frequency / Effort:** High / High / M (steps 1, 2, 4 are S)
- **Status:** Done (steps 1–4). `tests/test_contract.py` pins the key sets;
  a column is hidden from clients with `info={"private": True}`; the
  hand-built shapes have response models in `schemas.py`; ESLint (with the
  hooks rules) and a web build run in CI. Core records are still serialized
  from their columns — pinned by the contract tests, not by response
  models. Step 5 (generated client types) not done.

### 4. The Google integration is one 1,648-line module and one 844-line route file

- **Where:** `services/google.py` holds the OAuth state handling, the shared
  HTTP client, contacts import, calendar sync, Gmail parsing and storage, the
  retry queue, the backfill walk, sync claiming and orchestration, contact
  suggestions, and the periodic loop. `routes/google.py` holds three routers
  (integration admin, calendar events, emails) plus four diagnostic endpoints
  with their logic inline (`:289–500`). These are the #1 and #3 most-changed
  files in the repository.
- **Symptom:** god file, tangled responsibilities.
- **Why it matters:** every Gmail change is made in a file where calendar,
  OAuth and suggestion code are in scope and share module globals
  (`_client`, `_syncing`). `tests/test_google_sync.py` has to treat the whole
  module as one unit. The email *read* API (search, per-record list, body)
  has nothing to do with the Google connection but can only be found by
  opening the Google routes.
- **Fix:** mechanical, no behavior change, one move per commit:
  1. Turn `services/google.py` into a package: `client.py` (HTTP, tokens),
     `oauth.py`, `calendar.py`, `gmail.py` (parse/store/retry queue),
     `backfill.py`, `suggestions.py`, `sync.py` (claims, pass, loop).
     `__init__.py` re-exports the current public names so no caller changes.
  2. Move `calendar_router` and `emails_router` into `routes/calendar.py` and
     `routes/emails.py`.
  3. Move the diagnostic endpoints' bodies into the service package; the
     routes keep only auth and response shaping.
  Do this after items 1 and 2, which already pull the matching and
  serialization code out.
- **Pain / Frequency / Effort:** High / High / M
- **Status:** Done. `services/google/` is a package of nine modules (largest
  444 lines); `routes/google.py` is 368 lines, with the email and calendar
  read APIs in `routes/emails.py` and `routes/calendar.py` and the
  diagnostic probes in `services/google/diagnostics.py`. The diagnostic
  endpoints have no tests; they were moved, not rewritten.

### 5. Notification routing is written three times, inside the chat client module

- **Where:** "push if the user prefers it, else DM, else post to #tasks" is
  implemented in `services/colloqui.py:385` (`notify_task_event`),
  `services/colloqui.py:446` (`_send_due_reminder`), and
  `services/automations.py:279` (`_act_notify`), which reaches into
  `colloqui`'s private helpers (`_wants_push`, `_attention_count`,
  `_client_for`, `_mention`). `services/colloqui.py` is at once the chat HTTP
  client, workspace provisioning, task-notification policy, and the reminder
  loop.
- **Symptom:** duplicated logic, unclear naming (task reminders live in a
  module named for the chat product), leakage of private helpers.
- **Why it matters:** the three copies have drifted — a due reminder falls
  back to push when chat is not configured (`colloqui.py:456–461`); an
  assignment and an automation notice do not. Adding a channel (email, web
  push) or a per-user quiet-hours rule means finding and editing all three.
  Push-only installs have to import a chat module to send anything.
- **Fix:**
  1. Add `services/notifications.py` with one
     `deliver(db, user, title, body, link, data) -> "push" | "dm" | "channel" | None`
     that owns preference and fallback.
  2. Move the three call sites onto it, one per commit;
     `tests/test_push.py` and `tests/test_tasks.py` cover the behavior.
  3. Move `notify_task_event`, the due pass and `reminder_loop` out of
     `colloqui.py` (into `notifications.py` or a `tasks` service), leaving
     `colloqui.py` as the chat client only.
- **Pain / Frequency / Effort:** Med–High / Med / S–M
- **Status:** Done. `services/notifications.py` has the one `deliver()` and
  the task notifications; `services/colloqui.py` is the chat client only.
  Correction to the above: the existing tests did not cover routing at all,
  so `tests/test_notifications.py` was written against the old code first,
  then the code was changed. The fallback is now uniform (see Status at the
  top).

### 6. `Settings.jsx` is nine features in one file, each with its own loading code

- **Where:** `frontend/src/pages/Settings.jsx` — 1,759 lines, 48 `useState`,
  46 API calls, 7 `window.confirm`s; the most-changed source file in the
  frontend (27 commits). Sections are already separate functions
  (`TrashSection:23`, `ProfileSection:121`, `SecuritySection:298`,
  `SessionsSection:458`, `CustomFieldsSection:540`, `UsersSection:721`,
  `ColloquiSection:979`, `GoogleSection:1265`, `RingCentralSection:1525`).
  Across the frontend, every component writes its own load / busy / error
  state, and roughly 39 `catch` sites drop the error.
- **Symptom:** god file; duplicated logic (data loading).
- **Why it matters:** any settings change is a diff in a file where every
  other setting is in scope, and merge conflicts between unrelated features
  are likely. The hand-rolled loading means each new panel decides again
  whether a failed fetch looks like "empty" — `useRelated` in `hooks.js`
  solved this properly (`items: 'error'`) but only for one case.
- **Fix:**
  1. Move each section to `pages/settings/<Name>Section.jsx`;
     `Settings.jsx` keeps the tab shell (≈60 lines). Pure cut-and-paste, one
     section per commit. `AutomationsSection` and `FormsSection` already live
     this way.
  2. Add `useLoad(fn, deps)` to `hooks.js` returning
     `{ data, error, loading, reload }`, modelled on `useRelated`, and adopt
     it as sections are touched rather than in one sweep.
- **Pain / Frequency / Effort:** Med / High / S (split), M (hook adoption)
- **Status:** Done as scoped. `Settings.jsx` is an 86-line shell over
  `pages/settings/*`. `useLoad` is in `hooks.js` and used by three
  components (calendar panel, attachments panel, sessions); the rest adopt
  it as they are touched.

### 7. The list of record types, and what hangs off them, is kept in a dozen places

- **Where:** backend — `services/common.py:278` (`ENTITY_MODELS`,
  `LABEL_MODELS`), `routes/meta.py:23`, `services/maintenance.py:30`,
  `services/crud.py:51` (`PLURALS`), `services/automations.py:66`,
  `services/importer.py:66` and `:622`, and three identical
  `{"person", "lead", "company"}` maps (`routes/google.py:531`,
  `routes/ringcentral.py:190`, `routes/notes.py:21`). Frontend —
  `format.js:132`, `constants/options.js:51`, `Settings.jsx:16`,
  `DuplicatesBanner.jsx:8`, `AutomationsSection.jsx:12–27`,
  `NotesTimeline.jsx:38` and `:106`. Separately, the satellites are listed
  once for merge (`services/crud.py:713`) and once, differently, for
  permanent delete (`services/common.py:395`).
- **Symptom:** information leakage; missing constraints the app enforces in
  code (the `(entity_type, entity_id)` pairs have no foreign key).
- **Why it matters:** a new satellite table (say, a checklist on a record)
  must be added to both the merge list and the purge function; forgetting the
  first silently strands rows on a merged-away record, forgetting the second
  leaves orphans forever, and no constraint or test notices. Giving
  opportunities a call timeline means finding three separate maps.
- **Fix:**
  1. `services/entities.py`: one registry — type → model, plural, label,
     and capability flags (`trashable`, `has_contact_points`,
     `has_custom_fields`). Derive the existing maps from it one at a time.
  2. One `SATELLITES` table — model, what happens on merge, what happens on
     purge — read by both `merge_item` and `remove_entity_satellites`.
  3. Expose the registry's client-relevant part through the existing
     `/options` router so the frontend lists can shrink.
- **Pain / Frequency / Effort:** Med / Low–Med / S
- **Status:** Done. `services/entities.py` holds `RECORDS` and
  `SATELLITES`; merge and permanent delete both read it, and a test fails if
  a table with `(entity_type, entity_id)` is not registered. Merge had no
  tests; `tests/test_merge.py` was run against the old code and the new.
  Departure from step 3: the web app derives its lists from one module of
  its own (`constants/entities.js`) instead of fetching them — the server
  publishes the same list at `GET /options/record-types` for other clients.

### 8. Two databases in the code, one in the tests

- **Where:** twelve SQLite special cases in application code — `db.py:9`,
  `deps.py:14` (`as_utc`), `main.py:91` (`create_all`), `routes/auth.py:165`,
  `routes/duplicates.py:28`, `routes/automations.py:285`,
  `routes/meta.py:244`, `routes/reports.py:5` and `:50`,
  `services/google.py:384`, `services/common.py:433` — and
  `frontend/src/format.js` (`parseWhen`) on the client. The README states the
  suite needs Postgres because "the app uses Postgres-only SQL"; nothing
  runs the suite on SQLite.
- **Symptom:** change amplification; untested code path; a route for schema
  drift (`create_all` builds the dev schema from the models, bypassing the
  migrations production runs).
- **Why it matters:** every new foreign-key cascade has to be written twice
  ("the FK covers Postgres, but SQLite dev doesn't" appears four times), and
  the hand-written half is the half no test exercises. A feature can work in
  the SQLite quick start and fail under Postgres, or the reverse.
- **Fix:** this is a product decision, so two options:
  - *Drop SQLite for the app* (recommended): `docker compose up` already
    gives a Postgres dev stack on :8010 and the tests require one anyway.
    Then delete the special cases as their files are touched. CI's import
    smoke test moves to the Postgres job.
  - *Keep it*: turn on `PRAGMA foreign_keys=ON` for SQLite connections so the
    manual cascades can go, and run the suite on SQLite in CI so the path is
    real.
  Either way, add `alembic check` to CI so a model edit without a migration
  fails the build.
- **Pain / Frequency / Effort:** Med / Med / S–M
- **Status:** Done, taking the first option. SQLite support, its special
  cases, and the hand-written cascades are gone (the foreign keys that
  replace them are covered by `tests/test_cascades.py`); `aiosqlite` is out
  of the requirements and the lock. `alembic check` runs in CI — to get it
  clean, the seven indexes that existed only in migrations are now declared
  on the models. The README quick start uses the dev stack's Postgres,
  published on `localhost:5495`.

## Optional / later

- Six background loops repeat the same `while True / try / log / sleep`
  wrapper, and four services repeat `_get_client` / `aclose` / `_get_json` /
  `_release_db`. A `run_periodically(name, fn, interval)` helper and one small
  outbound-HTTP module would remove the copies. Low risk, low urgency.
- In-process state assumes a single worker: sync claims
  (`google.py:1390`, `ringcentral.py:407`), the feed's contact cache
  (`feed.py:34`), attachment quota reservations (`attachments.py:44`), the
  form daily counter (`public_forms.py:49`), and the login limiter. Fine for
  this deployment; write the assumption down once so nobody adds
  `--workers 2`.
- Route modules import private helpers from each other
  (`routes/public_forms.py:33`, `routes/users.py:11`). Move `_client_ip`,
  `_reserve` / `_release` into `services/throttle.py`. *(Still open: the
  login throttle's state is referenced by name in its tests.)*
- `register_crud` still inspects `after_create`'s signature for a "legacy"
  two-argument form (`crud.py:99–102`, `:521`); both remaining hooks take
  three arguments. Dead branch. *(Removed.)*
- Two commit conventions: `get_db` commits on the way out, and about sixty
  handlers also commit explicitly so the client's refetch sees the write.
  Both are reasoned; pick one sentence for the project rules.
- Vocabularies duplicated by hand between `schemas.py` / `routes/meta.py` and
  `frontend/src/constants/options.js` (lead statuses, priorities, custom
  field types, automation triggers and actions), and column widths mirrored
  between `models.py` and `schemas.py`.
- `org_id == …` is written inline 166 times. Correct today; the `Org`
  docstring's claim that multi-tenancy is "a config change" would need an
  audit of all of them.
- 23 function-level imports work around import cycles between `models`,
  `services` and `routes`; items 1, 4 and 5 should remove most of them.
- `main.py` carries the body-limit middleware (about 150 lines with its
  policy table, `:127–277`); it would read better
  in its own module. `styles.css` is a single 2,634-line file and the
  most-changed file in the repo — low pain so far.
- Thin test coverage relative to size: automations (none of their own —
  only the notify action is now covered), public forms (one module), saved
  filters (none), the importer (169 test lines for 1,214). Add characterization tests before refactoring any of these.
- Ruff runs on the app with only `E9,F821`; the tests get the full default
  set. Widening the app's rule set is cheap. *(Done: CI runs the default
  set on the app; it already passed.)*
- `colloquicrm-code-audit.md` (100 KB) sits untracked at the repository root;
  move it under `docs/` or out of the tree.

## What's already good

- **`register_crud` is a deep module.** One call per record type yields the
  whole list/export/bulk/merge/trash surface, with narrow hooks
  (`enrich`, `body_validator`, `after_update`, `merge_pool`) for what
  differs. Companies is 37 lines in total; people and opportunities add only
  their own rules on top. Keep extending it rather than writing endpoints
  beside it.
- **One settings object.** Every configuration value goes through
  `config.py`; the only direct environment read in the app is `GIT_SHA` in
  the health check.
- **`services/background.py`** — `spawn` and `after_commit` are small, do
  one thing, and remove a whole class of "notified before commit" bugs for
  every caller.
- **Permanent deletion has one path.** `remove_entity_satellites` states a
  decision for each satellite, and every hard delete goes through it.
- **Race-prone transitions are single conditional statements**
  (`UPDATE … WHERE … RETURNING` for lead conversion, restore, purge), with
  the reasoning written next to them.
- **Canonical values are defined as types** in `schemas.py` (`LeadStatus`,
  `Money`, `Currency`, `Priority`), so normalization happens once at the
  edge.
- **The frontend has one HTTP client** and config-driven generic pages
  (`ListPage`, `DetailShell`, `ProfilePanel` + `constants/fields.js`); a new
  list page is ~50 lines.
- **Tests run against real Postgres with real migrations**, and every
  outbound call is faked.
- **Comments explain why**, not what — most non-obvious lines say what went
  wrong before they were written that way.

## Suggested design principles

A draft `## Design principles` section for the project rules file. Written
before the work above; the version adopted in the rules file was adjusted to
the modules as built.

```markdown
## Design principles

- Before writing a helper, search `services/` for one. If the same rule
  would then exist in two places, move it to one module and call it from
  both.
- Matching an email address or phone number to a record goes through
  `services/contacts.py`. No other module reads `work_email`,
  `personal_email`, `work_phone`, `mobile_phone` or `Lead.email` to decide
  ownership, and none builds its own address map.
- A timeline item (email, call, text, note, activity) is serialized in one
  place, `services/timeline.py`. The feed, the per-record timeline and email
  search all return that shape.
- Sending a notification to a user goes through
  `services/notifications.py`; callers never choose between push, DM and
  channel themselves.
- Route handlers do auth, parsing and response shaping. Logic longer than a
  screen, or needed by a second caller, lives in `services/`. Route modules
  never import from other route modules.
- New record types, and new tables that hang off a record via
  `(entity_type, entity_id)`, are registered in `services/entities.py`.
  Merge and permanent-delete behavior for a satellite is declared there, not
  in the callers.
- A response shape that a client depends on has a test asserting its keys.
  Changing or removing a response field is a breaking change for the iOS
  app: add fields, don't rename them, and note the change in the PR.
- No file over ~600 lines gets a new responsibility; split it first. One
  settings section, one integration concern, or one router per file.
- Prefer extending `register_crud` with a hook over writing a parallel
  endpoint for a core record.
- For any change touching more than three files, write the plan and confirm
  it before coding.
```
