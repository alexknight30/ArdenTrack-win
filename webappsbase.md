# Web app + Supabase alignment (`webappsbase`)

This document is the **single checklist** for:

1. **Supabase project setup** (everything you must configure in the dashboard / SQL).
2. **Your separate web app repo** — moving APIs from **local paths / JSON / SQLite-adjacent reads** to **Supabase** so they match what **ArdenTrack-win** already reads/writes via [`ardentrack/supabase_sync.py`](ardentrack/supabase_sync.py).

Update this file when your web schema or routes change so both repos stay in sync.

---

## Part 1 — Everything to do in Supabase

**Step-by-step (dashboard navigation + full SQL):** see [**`supabase/SUPABASE_SETUP_WALKTHROUGH.md`**](supabase/SUPABASE_SETUP_WALKTHROUGH.md).

### 1.1 Project & API keys

1. Create or select a Supabase project.
2. **Settings → API**: copy **`SUPABASE_URL`** (Project URL) and **`SUPABASE_ANON_KEY`** (anon `public`). These are safe to ship in desktop and browser clients; they are not the service-role secret.

### 1.2 Authentication (desktop + browser)

1. **Authentication → URL configuration**
   - Add redirect: **`arden://auth/callback`** (ArdenTrack / Electron desktop OAuth finish).
   - Add your web origins as needed (e.g. `https://ardentime.com`, local dev `http://localhost:3000`).
2. **Authentication → Providers**: enable the providers you use (e.g. email, Google).
3. **Optional but recommended**: enable **refresh token rotation** (matches ArdenTrack [`ardentrack/auth.py`](ardentrack/auth.py) refresh flow).

### 1.3 Tables & columns (must align with ArdenTrack)

ArdenTrack expects **Postgres tables** in the **`public`** schema with names **`time_entries`**, **`matters`**, **`clients`**, **`profile`**.  
Every synced row must include **`user_id uuid NOT NULL REFERENCES auth.users(id)`** so **RLS** can use **`auth.uid()`**.

Below: **logical** shapes. Adjust Postgres types (e.g. `timestamptz` vs `text`) as long as PostgREST returns compatible JSON for your web app and daemon.

#### `time_entries`

Used by daemon: **upsert on conflict `id`** ([`push_time_entry`](ardentrack/supabase_sync.py)).

| Column | Notes |
|--------|--------|
| `id` | `text` or `uuid` — **PRIMARY KEY** or **UNIQUE** (daemon upserts on `id`). |
| `user_id` | `uuid` → `auth.users` |
| `date` | text `YYYY-MM-DD` (matches SQLite) |
| `start_time` | text `HH:MM` |
| `duration_min` | `numeric` / `double precision` |
| `description` | text |
| `matter` | text (nullable) |
| `confidence` | numeric (nullable) |
| `reviewed` | boolean or integer (0/1) |
| `outstanding_flags` | text |
| `task_code`, `activity_code`, `chunk_id` | text (nullable) |
| `created_at`, `updated_at` | timestamptz or text ISO |

#### `matters`

Daemon: **`select *`**, merge by **`name`**, upsert locally. Cloud rows must have **`name`**.

Mirror SQLite / [`MATTER_COLUMNS`](ardentrack/db.py):  
`name`, `shorthand`, `client`, `description`, `color`, `rate`, `billable`, `weekly_hours_goal`, `practice_area`, `clio_matter_id`, `external_sync_source`, `last_synced_at`, `created_at`, `updated_at` (+ `user_id`).

**Constraint**: uniqueness per user, e.g. **`UNIQUE (user_id, name)`** (so two users can each have a matter named `"General"`).

#### `clients`

Mirror [`CLIENT_COLUMNS`](ardentrack/db.py):  
`name`, `color`, `client_id`, `contact_person_name`, `email`, `phone`, `mailing_address`, `client_type`, `client_status`, `notes`, `date_opened`, `billing_contact_info`, `associated_matters`, `industry`, `conflict_check_status`, `referral_source`, `clio_client_id`, `created_at`, `updated_at` (+ `user_id`).

**Constraint**: **`UNIQUE (user_id, name)`**.

#### `profile`

Daemon **`pull_profile`**: **`select *`**, **`limit 1`** — assumes **one profile row per user** after RLS.

Mirror [`PROFILE_COLUMNS`](ardentrack/db.py) where possible:  
`firm_name`, `firm_address`, `firm_city`, `firm_state`, `firm_zip`, `firm_phone`, `firm_email`, `timekeeper_name`, `yearly_hour_goal`, `license_key`, `updated_at` (+ `user_id`).

SQLite uses singleton `id = 1`; in Postgres use **`user_id` as the scoped key** (primary key `(user_id)` or `user_id UNIQUE`). Daemon merge ignores unknown keys; **`id`** in cloud is optional if not in use.

#### `billing_codes` (optional but in original plan)

If the web app edits billing codes in Supabase: add **`user_id`**, RLS, and columns compatible with your UI (and optionally mirror SQLite `billing_codes` JSON fields). **ArdenTrack-win does not currently sync `billing_codes` in [`supabase_sync.py`](ardentrack/supabase_sync.py)** — add later if needed.

### 1.4 Row Level Security (required)

For each table: **`time_entries`**, **`matters`**, **`clients`**, **`profile`**, (`billing_codes` if used):

1. **`ALTER TABLE … ENABLE ROW LEVEL SECURITY;`**
2. Policy (pattern):

```sql
CREATE POLICY "users_own_rows"
ON public.time_entries
FOR ALL
TO authenticated
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());
```

Repeat with table name changed for `matters`, `clients`, `profile`, `billing_codes`.

**Important**: Inserts/updates from the **browser** and from **ArdenTrack** must send **`user_id`** equal to the authenticated user when required by your policies (daemon sets `user_id` in [`push_time_entry`](ardentrack/supabase_sync.py)).

### 1.5 Indexes (recommended)

- `time_entries (user_id)`, `(user_id, date)`
- `matters (user_id)`, `(user_id, name)`
- `clients (user_id)`, `(user_id, name)`
- `profile (user_id)` unique

### 1.6 Starter SQL (adapt names/types)

Run in **SQL Editor** after adjusting types:

```sql
-- Example: matters — repeat pattern for clients, profile, time_entries, billing_codes

CREATE TABLE IF NOT EXISTS public.matters (
  user_id uuid NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
  name text NOT NULL,
  shorthand text,
  client text,
  description text DEFAULT '',
  color text,
  rate double precision,
  billable integer DEFAULT 1,
  weekly_hours_goal double precision,
  practice_area text,
  clio_matter_id text,
  external_sync_source text,
  last_synced_at timestamptz,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  PRIMARY KEY (user_id, name)
);

ALTER TABLE public.matters ENABLE ROW LEVEL SECURITY;

CREATE POLICY "users_own_matters"
ON public.matters FOR ALL TO authenticated
USING (user_id = auth.uid())
WITH CHECK (user_id = auth.uid());
```

Generate **`time_entries`** with **`id`** primary key (global uniqueness is fine if IDs are UUIDs from ArdenTrack).

### 1.7 Verification in Supabase

- **Table Editor**: confirm columns and **`user_id`**.
- **Authentication → Policies**: each table shows RLS enabled + policy.
- Optional: test as a user with **SQL** `select auth.uid()` in a policy-safe query from the dashboard (service role) vs from client with JWT.

---

## Part 2 — Web app repo: move off local paths to Supabase

### 2.1 What “local” meant before

ArdenTrack historically shared JSON under **`%LOCALAPPDATA%/Arden/userdata/`** ([`ardentrack/paths.py`](ardentrack/paths.py)): `profile.json`, `matters.json`, `clients.json`, `sheet.csv`, etc. APIs that **read/write filesystem paths** or only those files must change.

### 2.2 Target architecture

| Old | New |
|-----|-----|
| Read/write JSON files or local DB paths | **Supabase** tables above with **`@supabase/supabase-js`** (or server routes using **service role** only where absolutely necessary — prefer user JWT + RLS). |
| No auth user scope | **Logged-in user**; every row scoped by **`user_id`** matching **`auth.uid()`**. |

### 2.3 Environment (web app)

- **`NEXT_PUBLIC_SUPABASE_URL`**
- **`NEXT_PUBLIC_SUPABASE_ANON_KEY`**

Never expose **service_role** in browser bundles.

### 2.4 Auth session

- Use the same Supabase Auth session as desktop where applicable (email/password, OAuth).
- **Desktop callback**: implement **`GET /auth/desktop-callback`** (see [`web/ardentime-desktop-callback.example.tsx`](web/ardentime-desktop-callback.example.tsx)) — **`exchangeCodeForSession`** then redirect **`arden://auth/callback?...`**.

### 2.5 Replace API implementations (checklist)

For each route that today uses **local paths** or **mock file reads**:

1. Identify the entity (**matters**, **clients**, **profile**, **time_entries**, …).
2. Map to the **same column names** as Part 1 (snake_case to match Postgres + daemon).
3. Use **`supabase.from('matters').select('*')`** etc.; RLS returns only the current user’s rows.
4. **Inserts/updates**: set **`user_id`** if your policy requires it on insert (often `user_id: (await supabase.auth.getUser()).data.user.id` or from session).
5. Remove or gate **legacy file** code paths behind a feature flag during migration.

### 2.6 Conflict rules vs ArdenTrack

- **Matters/clients**: daemon merges by **`name`** per user. Web edits should **upsert on `(user_id, name)`** or delete/update consistently so pulls don’t resurrect stale names.
- **Time entries**: daemon generates deterministic **`id`** per slot; avoid deleting IDs from the web without understanding sync.

### 2.7 Testing order (recommended)

1. Supabase schema + RLS (Part 1).
2. Web app read-only lists (matters/clients/profile) against Supabase.
3. Web writes + verify in Table Editor.
4. Run ArdenTrack dev — pull/push round-trip.
5. Production bundle (Electron + PyInstaller) final smoke test.

---

## Part 3 — Files in *this* repo to keep aligned

| File | Role |
|------|------|
| [`ardentrack/supabase_sync.py`](ardentrack/supabase_sync.py) | Source of truth for **table names** and **push payload** shape for `time_entries`. |
| [`ardentrack/db.py`](ardentrack/db.py) | **`MATTER_COLUMNS`**, **`CLIENT_COLUMNS`**, **`PROFILE_COLUMNS`** — mirror in Postgres where possible. |
| [`web/ardentime-desktop-callback.example.tsx`](web/ardentime-desktop-callback.example.tsx) | Desktop OAuth redirect example. |
| [`supabase/manual_dashboard_steps.sql`](supabase/manual_dashboard_steps.sql) | Short SQL snippets (older companion doc). |

---

## Changelog

| Date | Change |
|------|--------|
| (initial) | Added Part 1–3: Supabase checklist + web app migration + repo pointers. |
| (see file) | Link to detailed Supabase UI + SQL: [`supabase/SUPABASE_SETUP_WALKTHROUGH.md`](supabase/SUPABASE_SETUP_WALKTHROUGH.md). |
