import {createClient} from '@supabase/supabase-js';

// Public-safe values by design — the anon key is meant to be exposed client-side (it's
// what Supabase's own RLS policies exist to constrain), unlike SUPABASE_SERVICE_ROLE_KEY,
// which must never leave the gateway. Both come from Vite env vars, set at build time.
const SUPABASE_URL = import.meta.env.VITE_SUPABASE_URL || '';
const SUPABASE_ANON_KEY = import.meta.env.VITE_SUPABASE_ANON_KEY || '';

export const supabaseConfigured = Boolean(SUPABASE_URL && SUPABASE_ANON_KEY);

// A real client is only created when configured — calling any auth method against a
// misconfigured build should fail with a clear message, not throw deep inside the SDK.
export const supabase = supabaseConfigured
  ? createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
      auth: {persistSession: true, autoRefreshToken: true, detectSessionInUrl: true},
    })
  : null;
