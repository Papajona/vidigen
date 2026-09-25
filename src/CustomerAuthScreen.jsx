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

  return <div className="modalBack authBackdrop">
    <div className="modal authModal authShell">
      <aside className="authBrandPanel">
        <button className="authClose" type="button" aria-label="Close" onClick={onClose}>×</button>
        <div className="authBrand">
          <span className="authLogo">V</span>
          <span>VIDIGEN</span>
        </div>
        <div className="authHero">
          <span className="authEyebrow">AI CREATIVE STUDIO</span>
          <h1>{mode==='signup'?'Turn ideas into finished scenes.':'Welcome back to your studio.'}</h1>
          <p>{mode==='signup'?'Create once and keep your projects, generations and credits together.':'Pick up where you left off and continue creating.'}</p>
        </div>
        <div className="authFeatureList">
          <span><i>✦</i> Cinematic image &amp; video generation</span>
          <span><i>◌</i> Projects, assets &amp; timeline editing</span>
          <span><i>↗</i> Your credits stay with your account</span>
        </div>
        <small className="authLegal">By continuing, you agree to use Vidigen responsibly and keep your account secure.</small>
      </aside>
      <section className="authFormPanel">
        <button className="authMobileClose" type="button" aria-label="Close" onClick={onClose}>×</button>
        <div className="authFormIntro">
          <span className="authEyebrow">{mode==='signup'?'GET STARTED':'SIGN IN'}</span>
          <h2>{mode==='signup'?'Create your account':'Sign in'}</h2>
          <p>{mode==='signup'?'A simple account for your Vidigen workspace.':'Access your projects, generations and credits.'}</p>
        </div>

        <div className="authModeTabs" role="tablist" aria-label="Account access">
          <button type="button" className={mode==='signin'?'active':''} onClick={()=>switchMode('signin')}>Sign in</button>
          <button type="button" className={mode==='signup'?'active':''} onClick={()=>switchMode('signup')}>Create account</button>
        </div>

        {mode==='signup'&&!awaitingConfirmation&&<div className="authBenefits">
          <span>✓ 500 MB storage</span>
          <span>✓ Projects &amp; generations</span>
          <span>✓ Credits &amp; billing</span>
        </div>}

        {awaitingConfirmation
          ? <div className="authConfirm">
              <div className="authConfirmIcon">✓</div>
              <b>Check your inbox</b>
              <p>{info||'We sent a confirmation link to your email address.'}</p>
              <div className="authActions">
                <button className="primary" type="button" onClick={()=>switchMode('signin')}>Continue to sign in</button>
                <button className="secondary" type="button" disabled={busy} onClick={resendConfirmation}>{busy?'Sending…':'Resend email'}</button>
              </div>
            </div>
          : <form onSubmit={handleEmailAuth} className="authForm">
              <label htmlFor="vidigen-email">Email address</label>
              <input id="vidigen-email" type="email" required autoComplete="email" inputMode="email" value={email} onChange={e=>setEmail(e.target.value)} placeholder="you@example.com"/>
              <label htmlFor="vidigen-password">Password</label>
              <input id="vidigen-password" type="password" required minLength={8} autoComplete={mode==='signup'?'new-password':'current-password'} value={password} onChange={e=>setPassword(e.target.value)} placeholder="At least 8 characters"/>
              {error&&<p className="authError" role="alert">{error}</p>}
              {info&&<p className="authInfo" role="status">{info}</p>}
              <button className="primary authSubmit" type="submit" disabled={busy}>{busy?'Please wait…':mode==='signup'?'Create account':'Sign in to Vidigen'}</button>
              {mode==='signin'&&<button className="authForgot" type="button" disabled={busy} onClick={sendRecovery}>Forgot your password?</button>}
            </form>
        }

        {!awaitingConfirmation&&<p className="authBottomSwitch">{mode==='signup'?'Already have an account?':'New to Vidigen?'} <button type="button" onClick={()=>switchMode(mode==='signup'?'signin':'signup')}>{mode==='signup'?'Sign in':'Create an account'}</button></p>}
      </section>
    </div>
  </div>;
}
