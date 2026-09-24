import React, {useState} from 'react';
import {supabase, supabaseConfigured} from './supabaseClient.js';

/**
 * The customer-facing counterpart to what didn't exist before: a real sign-up/sign-in flow.
 * Everything else in this app (billing, credits, generation jobs) already requires a
 * Supabase-verified user_id — this is what actually gets a real customer one, rather than
 * expecting them to paste a raw access token into a settings field.
 */
export default function CustomerAuthScreen({onAuthenticated}) {
  const [mode, setMode] = useState('signin'); // 'signin' | 'signup'
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [info, setInfo] = useState('');

  // No auth-state listener here on purpose — that now lives at the App level (main.jsx),
  // registered once for the whole session so it keeps working after this component
  // unmounts on sign-in. This component only needs to report its OWN sign-up/sign-in
  // success immediately (better UX than waiting for that event to propagate); the OAuth
  // redirect-return case is covered by the App-level listener picking up the new session.

  if (!supabaseConfigured) {
    return (
      <div className="modalBack">
        <div className="modal">
          <div className="modalHead"><b>Sign-in not configured</b></div>
          <p>This build is missing VITE_SUPABASE_URL / VITE_SUPABASE_ANON_KEY at build time,
             so customer sign-in can't work yet. This is a build configuration gap, not
             something to work around with a manual token — set those two env vars and
             rebuild.</p>
        </div>
      </div>
    );
  }

  async function handleEmailAuth(e) {
    e.preventDefault();
    setError(''); setInfo(''); setBusy(true);
    try {
      if (mode === 'signup') {
        const {data, error: err} = await supabase.auth.signUp({email, password});
        if (err) throw err;
        if (data.session?.access_token) {
          onAuthenticated(data.session.access_token);
        } else {
          // Email confirmation is on for this Supabase project — no session yet, and that's
          // correct behavior, not a bug to route around.
          setInfo('Account created — check your email to confirm before signing in.');
        }
      } else {
        const {data, error: err} = await supabase.auth.signInWithPassword({email, password});
        if (err) throw err;
        onAuthenticated(data.session.access_token);
      }
    } catch (err) {
      setError(err.message || 'Something went wrong.');
    } finally {
      setBusy(false);
    }
  }

  async function handleGoogle() {
    setError('');
    const {error: err} = await supabase.auth.signInWithOAuth({
      provider: 'google',
      options: {redirectTo: window.location.origin},
    });
    if (err) setError(err.message);
    // No further handling here — the redirect leaves the page, and onAuthStateChange above
    // picks up the session on return.
  }

  return (
    <div className="modalBack">
      <div className="modal">
        <div className="modalHead"><b>{mode === 'signup' ? 'Create your account' : 'Sign in'}</b></div>
        <p>{mode === 'signup' ? 'Free accounts include 500 MB storage, 5 avatar generations/day and 5 photo enhancements/day.' : 'Welcome back.'}</p>

        <button className="primary" onClick={handleGoogle} style={{marginBottom: 12}}>
          Continue with Google
        </button>

        <form onSubmit={handleEmailAuth}>
          <label>Email</label>
          <input type="email" required value={email} onChange={e => setEmail(e.target.value)} />
          <label>Password</label>
          <input type="password" required minLength={8} value={password} onChange={e => setPassword(e.target.value)} />
          {error && <p style={{color: 'var(--bad, #f87171)'}}>{error}</p>}
          {info && <p className="muted">{info}</p>}
          <button className="primary" type="submit" disabled={busy} style={{marginTop: 12}}>
            {busy ? 'Please wait…' : mode === 'signup' ? 'Create account' : 'Sign in'}
          </button>
        </form>

        <p className="muted" style={{marginTop: 14, cursor: 'pointer'}}
           onClick={() => { setMode(mode === 'signup' ? 'signin' : 'signup'); setError(''); setInfo(''); }}>
          {mode === 'signup' ? 'Already have an account? Sign in' : "New here? Create an account"}
        </p>
      </div>
    </div>
  );
}
