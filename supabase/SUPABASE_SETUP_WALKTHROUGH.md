# Supabase setup — detailed walkthrough (ArdenTrack + web app)

Follow steps **in order**. Supabase UI labels can shift slightly between versions; if you do not see an exact name, look for the closest match under the same area (Authentication, Settings, SQL Editor).

---

## Before you start

- Use a **desktop browser** (Chrome/Edge/Firefox).
- Have this doc open next to your Supabase project tab.

---

## Step 1 — Create or open your project

1. Go to [https://supabase.com/dashboard](https://supabase.com/dashboard) and sign in.
2. **Create a new project** (green **New project**) *or* click an existing project.
3. If creating new:
   - Choose **Organization**, **Name**, **Database password** (save it in a password manager).
   - Pick a **Region** close to your users.
   - Click **Create new project** and wait until status is **Healthy** (can take 1–2 minutes).

You should see the **project home** with the left sidebar (icons for Table Editor, SQL Editor, Authentication, etc.).

---

## Step 2 — Copy API URL and anon key (for env vars)

These become `SUPABASE_URL` and `SUPABASE_ANON_KEY` in ArdenTrack, Electron, and your web app.

1. In the **left sidebar**, click the **gear / Project Settings** (sometimes **Settings** at the bottom of the sidebar).
2. Open **Configuration** (or **Project Settings**) → **API** (or **Data API**).
3. Find:
   - **Project URL** — copy the full `https://xxxxx.supabase.co` string. This is `SUPABASE_URL`.
   - **Project API keys** — find the key named **`anon` `public`**. Click **Reveal** if needed, then copy. This is `SUPABASE_ANON_KEY` (long JWT-looking string).

**Do not** use the **`service_role` `secret`** key in browser or desktop apps. Keep it server-only if you ever need it.

4. Paste both into a secure note; you will add them to environment variables later (not in git).

---

## Step 3 — Authentication: redirect URLs (desktop + web)

ArdenTrack’s desktop OAuth finish uses the custom scheme **`arden://auth/callback`**. Your website will use normal HTTPS URLs.

1. In the **left sidebar**, click **Authentication** (user icon).
2. Open **URL Configuration** (sometimes under Authentication → **URL Configuration**).
3. Find **Redirect URLs** (allow list).
4. Click **Add URL** and add **each** of these on its own line (adjust localhost port to match your dev server):

   | URL | Purpose |
   |-----|--------|
   | `arden://auth/callback` | Electron / desktop PKCE finish |
   | `https://ardentime.com/auth/callback` | Production web (change domain if yours differs) |
   | `http://localhost:3000/auth/callback` | Local Next.js (example; use your port/path) |

5. **Save** (button may say **Save**, **Update**, or apply automatically per row).

**Site URL** (same Auth settings area): set to your main web origin, e.g. `https://ardentime.com` or `http://localhost:3000` for dev — this affects default redirects for some flows.

---

## Step 4 — Authentication: enable sign-in providers

1. Still under **Authentication**, open **Providers**.
2. For each method you want (e.g. **Email**, **Google**):
   - Toggle **Enable**.
   - Fill required fields (Google needs OAuth client ID/secret from Google Cloud Console).
3. **Save** each provider.

Until at least one provider is enabled, users cannot sign in.

---

## Step 5 — Optional: refresh token rotation

1. Under **Authentication**, look for **Refresh tokens**, **Advanced**, or **Session settings** (location varies).
2. If you see **Detect and revoke potentially compromised refresh tokens** / **Refresh token reuse detection** or **Refresh token rotation**, enable it if you want stricter session hygiene (recommended for production).

If you cannot find it, you can skip; ArdenTrack refresh still works with default Supabase auth behavior in most projects.

---

## Step 6 — Create tables (SQL Editor)

You will run **one script** that creates `matters`, `clients`, `profile`, and `time_entries` with `user_id` and constraints aligned with [`ardentrack/supabase_sync.py`](../ardentrack/supabase_sync.py).

1. In the **left sidebar**, click **SQL Editor**.
2. Click **New query**.
3. Paste the entire script below into the editor.
4. Click **Run** (or press the shortcut shown, e.g. Ctrl+Enter).

**Important:** Run this on a **fresh** project or one without conflicting table names. If a table already exists, you may need to `DROP TABLE ... CASCADE` manually (destructive) or alter columns by hand — backup first.

### Full script (copy everything in this block)

```sql
-- =============================================================================
-- ArdenTrack / ArdenTime — public schema tables + RLS
-- Run once in Supabase SQL Editor. Adjust if tables already exist.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- matters: one row per (user, name)
-- ---------------------------------------------------------------------------
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

CREATE INDEX IF NOT EXISTS idx_matters_user_id ON public.matters (user_id);

-- ---------------------------------------------------------------------------
-- clients: one row per (user, name)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.clients (
  user_id uuid NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
  name text NOT NULL,
  color text,
  client_id text,
  contact_person_name text,
  email text,
  phone text,
  mailing_address text,
  client_type text DEFAULT '',
  client_status text DEFAULT '',
  notes text,
  date_opened text,
  billing_contact_info text,
  associated_matters text,
  industry text,
  conflict_check_status text DEFAULT '',
  referral_source text,
  clio_client_id text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now(),
  PRIMARY KEY (user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_clients_user_id ON public.clients (user_id);

-- ---------------------------------------------------------------------------
-- profile: one row per user (daemon uses limit 1 after RLS)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.profile (
  user_id uuid PRIMARY KEY REFERENCES auth.users (id) ON DELETE CASCADE,
  firm_name text DEFAULT '',
  firm_address text DEFAULT '',
  firm_city text DEFAULT '',
  firm_state text DEFAULT '',
  firm_zip text DEFAULT '',
  firm_phone text DEFAULT '',
  firm_email text DEFAULT '',
  timekeeper_name text DEFAULT '',
  yearly_hour_goal double precision DEFAULT 0,
  license_key text DEFAULT '',
  updated_at timestamptz DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- time_entries: daemon upserts on conflict (id) — id is text stable id from app
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.time_entries (
  id text PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES auth.users (id) ON DELETE CASCADE,
  date text NOT NULL,
  start_time text NOT NULL,
  duration_min double precision NOT NULL,
  description text DEFAULT '',
  matter text,
  confidence double precision,
  reviewed integer DEFAULT 0,
  outstanding_flags text DEFAULT '',
  task_code text,
  activity_code text,
  chunk_id text,
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_time_entries_user_id ON public.time_entries (user_id);
CREATE INDEX IF NOT EXISTS idx_time_entries_user_date ON public.time_entries (user_id, date);

-- =============================================================================
-- Row Level Security + policies (authenticated users only see own rows)
-- =============================================================================

ALTER TABLE public.matters ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.clients ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.profile ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.time_entries ENABLE ROW LEVEL SECURITY;

-- Drop policies if re-running script (ignore errors first time)
DROP POLICY IF EXISTS "users_own_matters" ON public.matters;
DROP POLICY IF EXISTS "users_own_clients" ON public.clients;
DROP POLICY IF EXISTS "users_own_profile" ON public.profile;
DROP POLICY IF EXISTS "users_own_time_entries" ON public.time_entries;

CREATE POLICY "users_own_matters"
  ON public.matters FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE POLICY "users_own_clients"
  ON public.clients FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE POLICY "users_own_profile"
  ON public.profile FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

CREATE POLICY "users_own_time_entries"
  ON public.time_entries FOR ALL TO authenticated
  USING (user_id = auth.uid())
  WITH CHECK (user_id = auth.uid());

-- Optional: allow service role full access (Supabase uses this for dashboard tools)
-- Default superuser bypass may apply; policies above affect JWT role "authenticated".
```

5. Confirm **Success** in the results panel. If you see an error, read the message:
   - **already exists** — table was created earlier; fix by renaming or dropping in a separate maintenance query (careful in production).
   - **permission denied** — run as project owner; refresh dashboard.

---

## Step 7 — Verify in Table Editor

1. Open **Table Editor** in the left sidebar.
2. Schema **public** — you should see **matters**, **clients**, **profile**, **time_entries**.
3. Click each table and confirm columns exist; **`user_id`** should appear on all four.

---

## Step 8 — Verify RLS (quick check)

1. **Authentication → Policies** (or **Table Editor → table → RLS** depending on UI).
2. Each of the four tables should show **RLS enabled** and a policy like **users_own_…**.

Alternatively run in **SQL Editor**:

```sql
SELECT tablename, rowsecurity
FROM pg_tables
WHERE schemaname = 'public'
  AND tablename IN ('matters', 'clients', 'profile', 'time_entries');
```

`rowsecurity` should be `true` for each.

---

## Step 9 — Optional: `billing_codes` later

The daemon does **not** sync `billing_codes` yet. When your web app needs it:

1. Add a table with **`user_id`** + your JSON/text columns.
2. Enable RLS + same **`user_id = auth.uid()`** policy pattern.

See [`webappsbase.md`](../webappsbase.md) Part 1.

---

## Step 10 — Connect ArdenTrack / web env

Set these where you run the app (system env, `.env` for dev, Electron builder config):

| Variable | Value |
|----------|--------|
| `SUPABASE_URL` | Project URL from Step 2 |
| `SUPABASE_ANON_KEY` | anon `public` key from Step 2 |

Restart the app after changing env vars.

---

## Troubleshooting

| Symptom | What to check |
|--------|----------------|
| Desktop redirect never completes | Redirect URL **`arden://auth/callback`** exactly in Auth URL configuration |
| **42501** / RLS blocks inserts | JWT present? **`user_id`** on row equals **`auth.uid()`**? |
| Upsert fails on **time_entries** | **`id`** is primary key; daemon sends **`on_conflict=id`** |
| Empty lists from web | User logged in? Using **anon** key + user session, not service_role in browser |

---

## Related docs in this repo

- [`webappsbase.md`](../webappsbase.md) — web app migration + column reference  
- [`manual_dashboard_steps.sql`](manual_dashboard_steps.sql) — short SQL snippets (superseded by this walkthrough’s full script)
