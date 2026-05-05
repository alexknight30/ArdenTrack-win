/**
 * Copy into ardentime.com (e.g. app/auth/desktop-callback/page.tsx).
 * Wire your Supabase browser client; query params: code, state (PKCE) or
 * code_challenge per your flow. After session exists, exchange and redirect to:
 * arden://auth/callback?access_token=...&refresh_token=...&expires_in=...
 */
"use client";

import { useEffect, useState } from "react";
import { createClient } from "@supabase/supabase-js";

export default function DesktopCallbackPage() {
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    const supabase = createClient(
      process.env.NEXT_PUBLIC_SUPABASE_URL!,
      process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!,
    );

    (async () => {
      try {
        const url = new URL(window.location.href);
        const code = url.searchParams.get("code");
        if (!code) {
          setErr("Missing authorization code");
          return;
        }
        const { data, error } = await supabase.auth.exchangeCodeForSession(code);
        if (error || !data.session) {
          setErr(error?.message ?? "No session");
          return;
        }
        const { access_token, refresh_token, expires_in } = data.session;
        const u = new URL("arden://auth/callback");
        u.searchParams.set("access_token", access_token);
        u.searchParams.set("refresh_token", refresh_token);
        u.searchParams.set("expires_in", String(expires_in ?? 3600));
        window.location.href = u.toString();
      } catch (e: unknown) {
        setErr(e instanceof Error ? e.message : "Error");
      }
    })();
  }, []);

  if (err) {
    return <p style={{ fontFamily: "system-ui" }}>Error: {err}</p>;
  }
  return <p style={{ fontFamily: "system-ui" }}>Connecting ArdenTrack…</p>;
}
