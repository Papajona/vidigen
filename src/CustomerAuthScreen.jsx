import React, {useState} from 'react';
import {supabase, supabaseConfigured} from './supabaseClient.js';

function friendlyAuthError(message=''){
  const m=String(message||'').toLowerCase();
  if(m.includes('invalid login credentials')) return 'The email or password is incorrect.';
  if(m.includes('email not confirmed')) return 'Please confirm your email before signing in.';
  if(m.includes('password') && m.includes('least')) return 'Use a stronger password and try again.';
  if(m.includes('rate limit')) return 'Too many attempts. Please wait a moment and try again.';
  if(m.includes('user already registered')) return 'That email is already registered. Try signing in instead.';
  return message || 'We could not complete that request. Please try again.';
}

export default function CustomerAuthScreen({onAuthenticated, onClose}) {
  const [mode,setMode]=useState('signin');
  const [email,setEmail]=useState('');
  const [password,setPassword]=useState('');
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [info,setInfo]=useState('');
  const [awaitingConfirmation,setAwaitingConfirmation]=useState(false);

  if(!supabaseConfigured){
    return <div className="modalBack">
      <div className="modal authModal">
        <div className="modalHead">
          <div><b>Vidigen account</b><small className="modalSub">Customer access is not configured</small></div>
          <button className="secondary" type="button" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <p>Production sign-in needs the frontend Supabase URL and anonymous key at build time. This build is missing one or both values.</p>
        <button className="primary" type="button" onClick={onClose}>Close</button>
      </div>
    </div>;
  }

  const normalizedEmail=email.trim().toLowerCase();

  function switchMode(next){
    setMode(next); setError(''); setInfo(''); setAwaitingConfirmation(false);
  }

  async function resendConfirmation(){
    if(!normalizedEmail){setError('Enter your email first.');return}
    setBusy(true); setError(''); setInfo('');
    try{
      const {error:err}=await supabase.auth.resend({type:'signup',email:normalizedEmail});
      if(err) throw err;
      setInfo('A new confirmation email has been sent. Check your inbox and spam folder.');
    }catch(err){setError(friendlyAuthError(err.message))}
    finally{setBusy(false)}
  }

  async function handleEmailAuth(e){
    e.preventDefault();
    setError(''); setInfo('');
    if(!normalizedEmail){setError('Enter your email address.');return}
    if(password.length<8){setError('Password must be at least 8 characters.');return}
    setBusy(true);
    try{
      if(mode==='signup'){
        const {data,error:err}=await supabase.auth.signUp({
          email:normalizedEmail,
          password,
          options:{emailRedirectTo:window.location.origin}
        });
        if(err) throw err;
        if(data.session?.access_token){
          onAuthenticated(data.session.access_token);
          return;
        }
        setAwaitingConfirmation(true);
        setInfo('Account created. Check your email to confirm your address, then sign in.');
      }else{
        const {data,error:err}=await supabase.auth.signInWithPassword({
          email:normalizedEmail,
          password
        });
        if(err) throw err;
        if(!data.session?.access_token) throw new Error('Sign-in completed without a session. Please try again.');
        onAuthenticated(data.session.access_token);
      }
    }catch(err){
      const msg=friendlyAuthError(err.message);
      setError(msg);
      if(String(err.message||'').toLowerCase().includes('email not confirmed')) setAwaitingConfirmation(true);
    }finally{
      setBusy(false);
    }
  }

  async function sendRecovery(){
    setError(''); setInfo('');
    if(!normalizedEmail){setError('Enter your email first.');return}
    setBusy(true);
    try{
      const {error:err}=await supabase.auth.resetPasswordForEmail(normalizedEmail,{
        redirectTo:window.location.origin
      });
      if(err) throw err;
      setInfo('Password recovery email sent. Open the link on this Vidigen site to set a new password.');
    }catch(err){setError(friendlyAuthError(err.message))}
    finally{setBusy(false)}
  }

  return <div className="modalBack">
    <div className="modal authModal">
      <div className="modalHead">
        <div>
          <b>{mode==='signup'?'Create your Vidigen account':'Welcome back'}</b>
          <small className="modalSub">{mode==='signup'?'Create once, then keep your projects and credits in one account.':'Sign in to generate, save projects and use your account credits.'}</small>
        </div>
        <button className="secondary" type="button" aria-label="Close account dialog" onClick={onClose}>×</button>
      </div>

      {mode==='signup'&&!awaitingConfirmation&&<div className="authBenefits">
        <span>✓ 500 MB storage</span>
        <span>✓ Account-scoped projects</span>
        <span>✓ Credits &amp; billing</span>
      </div>}

      {awaitingConfirmation
        ? <div className="authConfirm">
            <div className="authConfirmIcon">✓</div>
            <b>Check your email</b>
            <p>{info||'We sent a confirmation link to your email address.'}</p>
            <div className="authActions">
              <button className="primary" type="button" onClick={()=>switchMode('signin')}>Continue to sign in</button>
              <button className="secondary" type="button" disabled={busy} onClick={resendConfirmation}>{busy?'Sending…':'Resend confirmation'}</button>
            </div>
          </div>
        : <form onSubmit={handleEmailAuth}>
            <label>Email</label>
            <input type="email" required autoComplete="email" inputMode="email" value={email} onChange={e=>setEmail(e.target.value)} placeholder="you@example.com"/>
            <label>Password</label>
            <input type="password" required minLength={8} autoComplete={mode==='signup'?'new-password':'current-password'} value={password} onChange={e=>setPassword(e.target.value)} placeholder="At least 8 characters"/>
            {error&&<p className="authError" role="alert">{error}</p>}
            {info&&<p className="authInfo" role="status">{info}</p>}
            <button className="primary" type="submit" disabled={busy}>{busy?'Please wait…':mode==='signup'?'Create account':'Sign in'}</button>
            {mode==='signin'&&<button className="secondary authLinkButton" type="button" disabled={busy} onClick={sendRecovery}>Forgot password?</button>}
          </form>
      }

      {!awaitingConfirmation&&<button type="button" className="authSwitch" onClick={()=>switchMode(mode==='signup'?'signin':'signup')}>
        {mode==='signup'?'Already have an account? Sign in':"New to Vidigen? Create an account"}
      </button>}
    </div>
  </div>;
}
