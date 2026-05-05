-- Reference SQL for Supabase dashboard (run/adapt in SQL editor after enabling extensions).
-- 1. Add user_id to synced tables (adjust if columns already exist).
-- 2. Enable RLS and policies per table.

-- Example for public.time_entries (repeat pattern for matters, clients, profile, billing_codes):
-- ALTER TABLE public.time_entries ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES auth.users(id);
-- CREATE INDEX IF NOT EXISTS idx_time_entries_user_id ON public.time_entries(user_id);

-- ALTER TABLE public.matters ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES auth.users(id);
-- ALTER TABLE public.clients ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES auth.users(id);
-- ALTER TABLE public.profile ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES auth.users(id);
-- ALTER TABLE public.billing_codes ADD COLUMN IF NOT EXISTS user_id uuid REFERENCES auth.users(id);

-- RLS (run per table after ALTER):
-- ALTER TABLE public.time_entries ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "users_can_only_access_own_data" ON public.time_entries
--   FOR ALL USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

-- Auth UI: add redirect URL arden://auth/callback
-- Auth settings: enable refresh token rotation
