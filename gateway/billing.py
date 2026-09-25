from __future__ import annotations
import hashlib, hmac, json, os, uuid
from datetime import datetime, timezone, timedelta
from typing import Any
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from gateway import persistence

PAYSTACK_BASE = os.getenv('PAYSTACK_BASE_URL', 'https://api.paystack.co').rstrip('/')
PAYSTACK_SECRET = os.getenv('PAYSTACK_SECRET_KEY', '')
PAYMENT_TEST_MODE = os.getenv('VIDIGEN_PAYMENT_TEST_MODE', 'false').lower() in {'1','true','yes','on'}
GOOGLE_PAY_TEST_MODE = os.getenv('GOOGLE_PAY_TEST_MODE', 'true').lower() in {'1','true','yes','on'}
TEST_PAYMENT_PRICES_GHS = (10.0, 20.0, 50.0)
BILLING_ENFORCE = os.getenv('VIDIGEN_BILLING_ENFORCE', 'true').lower() in {'1','true','yes','on'}
CURRENCY = os.getenv('VIDIGEN_BILLING_CURRENCY', 'GHS')
DEFAULT_PLANS = [
    {'slug':'free','name':'Free','price_ghs':0.0,'monthly_credits':0,'storage_gb':0.5,'watermark':True,'commercial_use':False,'priority':False,'interval':'monthly'},
    {'slug':'creator','name':'Creator','price_ghs':149.0,'monthly_credits':600,'storage_gb':25,'watermark':False,'commercial_use':True,'priority':False,'interval':'monthly'},
    {'slug':'pro','name':'Pro','price_ghs':399.0,'monthly_credits':1800,'storage_gb':100,'watermark':False,'commercial_use':True,'priority':True,'interval':'monthly'},
    {'slug':'studio','name':'Studio','price_ghs':999.0,'monthly_credits':5000,'storage_gb':500,'watermark':False,'commercial_use':True,'priority':True,'interval':'monthly'},
]
DEFAULT_MODEL_COSTS = [
    {'model_key':'default-video','operation':'video','credits_per_5s':80},
    {'model_key':'bytedance/seedance-2.0','operation':'video','credits_per_5s':110},
    {'model_key':'runway-premium','operation':'video','credits_per_5s':120},
    {'model_key':'default-image','operation':'image','credits_per_5s':6},
]

class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    plan_slug: str = Field(min_length=2,max_length=32)
    callback_url: str | None = Field(default=None,max_length=2000)
    payment_method: str = Field(default='paystack', pattern=r'^paystack$')

class CreditPackRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    credits: int = Field(default=150, ge=150, le=150)
    callback_url: str | None = Field(default=None, max_length=2000)

class AdjustPlanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    price_ghs: float = Field(ge=0, le=100000)
    monthly_credits: int = Field(ge=0, le=10_000_000)
    storage_gb: int = Field(ge=0, le=10000)
    watermark: bool
    commercial_use: bool
    priority: bool


class TestPaymentRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    amount_ghs: float = Field(gt=0, le=50)
    callback_url: str | None = Field(default=None, max_length=2000)


def _amount_subunit(ghs: float) -> int:
    return int(round(float(ghs) * 100))


def _is_paystack_test_key() -> bool:
    return PAYSTACK_SECRET.startswith('sk_test_')


def _validate_test_amount(amount_ghs: float) -> float:
    amount = round(float(amount_ghs), 2)
    if amount not in TEST_PAYMENT_PRICES_GHS:
        raise HTTPException(400, 'Test amount must be exactly GHS 10, GHS 20, or GHS 50.')
    return amount


def _sign(body: bytes) -> str:
    return hmac.new(PAYSTACK_SECRET.encode(), body, hashlib.sha512).hexdigest()

async def _paystack(method: str, path: str, *, payload: dict | None = None):
    if not PAYSTACK_SECRET:
        raise HTTPException(503, 'PAYSTACK_SECRET_KEY is not configured.')
    headers={'Authorization':f'Bearer {PAYSTACK_SECRET}','Content-Type':'application/json'}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as c:
        r=await c.request(method, PAYSTACK_BASE+path, headers=headers, json=payload)
    try: data=r.json()
    except Exception: data={'message':r.text[:500]}
    if r.status_code >= 400 or not data.get('status', False):
        raise HTTPException(502, f"Paystack request failed: {data.get('message','unknown error')}")
    return data.get('data') or {}

async def _ensure_plan_row(plan: dict) -> dict:
    if not persistence.enabled():
        return plan
    rows = await persistence.sb_request('GET','subscription_plans',params={'slug':f"eq.{plan['slug']}",'select':'*','limit':'1'})
    if rows: return rows[0]
    created = await persistence.sb_request('POST','subscription_plans',plan)
    return created[0] if created else plan

async def ensure_default_plans():
    if not persistence.enabled(): return
    for p in DEFAULT_PLANS:
        await _ensure_plan_row(p)

async def _get_plan(slug: str) -> dict | None:
    if not persistence.enabled():
        return next((p for p in DEFAULT_PLANS if p['slug']==slug), None)
    rows=await persistence.sb_request('GET','subscription_plans',params={'slug':f'eq.{slug}','select':'*','limit':'1'})
    return rows[0] if rows else None

async def _sync_paystack_plan(plan: dict) -> dict:
    name=plan['name']; amount=_amount_subunit(plan['price_ghs']); interval=plan.get('interval','monthly')
    existing=plan.get('paystack_plan_code')
    payload={'name':name,'amount':amount,'interval':interval,'currency':CURRENCY,'send_invoices':True}
    if existing:
        return await _paystack('PUT', f'/plan/{existing}', payload=payload)
    created=await _paystack('POST','/plan',payload=payload)
    if persistence.enabled() and created.get('plan_code'):
        await persistence.sb_request('PATCH','subscription_plans',{'paystack_plan_code':created['plan_code']},params={'slug':f"eq.{plan['slug']}"})
    return created

FREE_DAILY_FEATURE_LIMIT = 5

async def get_user_plan_slug(user_id: str) -> str:
    if not persistence.enabled():
        return 'free'
    rows=await persistence.sb_request('GET','subscriptions',params={
        'user_id':f'eq.{user_id}','status':'in.(active,past_due)',
        'select':'plan_slug','order':'created_at.desc','limit':'1'
    })
    return rows[0].get('plan_slug') if rows and rows[0].get('plan_slug') else 'free'

async def enforce_free_daily_feature(user_id: str, feature: str) -> dict:
    slug=await get_user_plan_slug(user_id)
    if slug != 'free':
        return {'allowed':True,'plan':slug,'count':None,'limit':None}
    result=await persistence.consume_daily_feature(user_id,feature,FREE_DAILY_FEATURE_LIMIT)
    if not result:
        raise HTTPException(503,'Daily feature usage could not be recorded.')
    count=int(result.get('new_count') or 0)
    if not result.get('allowed', True):
        raise HTTPException(429,f'Free plan limit reached: {FREE_DAILY_FEATURE_LIMIT} uses per day for {feature.replace("_"," ")}.')
    return {'allowed':True,'plan':'free','count':count,'limit':FREE_DAILY_FEATURE_LIMIT}

async def refund_free_daily_feature(user_id: str, feature: str) -> None:
    try:
        await persistence.release_daily_feature(user_id,feature)
    except Exception:
        pass

async def user_billing_summary(user_id: str) -> dict:
    if not persistence.enabled():
        return {'configured':False,'billing_enforced':False,'message':'Supabase billing persistence is required in production.'}
    plans=await persistence.sb_request('GET','subscription_plans',params={'select':'*','order':'price_ghs.asc'})
    subs=await persistence.sb_request('GET','subscriptions',params={'user_id':f'eq.{user_id}','status': 'in.(active,past_due)','select':'*','order':'created_at.desc','limit':'1'})
    wallet=await persistence.sb_request('GET','credit_wallets',params={'user_id':f'eq.{user_id}','select':'*','limit':'1'})
    return {'configured':True,'billing_enforced':BILLING_ENFORCE,'plans':plans or DEFAULT_PLANS,'subscription':subs[0] if subs else None,'wallet':wallet[0] if wallet else None}

async def consume_credits(user_id: str, operation: str, model: str, duration_seconds: int = 5, *, metadata: dict | None=None) -> dict:
    if not persistence.enabled():
        if BILLING_ENFORCE and user_id and os.getenv('VIDIGEN_ALLOW_BILLING_BYPASS','false').lower() not in {'1','true','yes'}:
            raise HTTPException(503,'Billing persistence is required for production generation.')
        return {'charged':0,'bypassed':True}
    model_key=(model or '').lower() or ('default-image' if operation=='image' else 'default-video')
    rows=await persistence.sb_request('GET','ai_model_prices',params={'model_key':f'eq.{model_key}','operation':f'eq.{operation}','select':'credits_per_5s','limit':'1'})
    base=int(rows[0]['credits_per_5s']) if rows else (6 if operation=='image' else 80)
    multiplier=max(1, int((max(1,duration_seconds)+4)//5))
    charge=base*multiplier
    # Ensure a wallet row exists before attempting the atomic debit below — the RPC's WHERE
    # clause only matches an EXISTING row, so a brand-new user with no wallet yet would
    # otherwise always fail the balance check even though they should get the free plan's
    # starting allowance. This existence check has a narrow, harmless race of its own (two
    # simultaneous first-ever requests could both try to insert), handled by the ON CONFLICT
    # in the atomic functions themselves for the actual balance mutation — but plain wallet
    # creation isn't itself a balance mutation, so this part doesn't need to be atomic.
    wallet_rows=await persistence.sb_request('GET','credit_wallets',params={'user_id':f'eq.{user_id}','select':'user_id','limit':'1'})
    if not wallet_rows:
        free=await _get_plan('free') or DEFAULT_PLANS[0]
        try:
            await persistence.sb_request('POST','credit_wallets',{'user_id':user_id,'balance':free['monthly_credits'],'monthly_allowance':free['monthly_credits']})
        except persistence.PersistenceError:
            pass  # Another concurrent request already created it — fine, proceed to the atomic debit below.
    result=await persistence.sb_request('POST','rpc/consume_credits_atomic',{'p_user_id':user_id,'p_amount':charge})
    if not result:
        wallet_rows=await persistence.sb_request('GET','credit_wallets',params={'user_id':f'eq.{user_id}','select':'balance','limit':'1'})
        balance=int(wallet_rows[0]['balance']) if wallet_rows else 0
        raise HTTPException(402, f'Insufficient credits. This operation requires {charge} credits and your balance is {balance}.')
    new_balance=result[0]['new_balance']
    await persistence.sb_request('POST','credit_transactions',{'user_id':user_id,'amount':-charge,'kind':'generation','operation':operation,'model_key':model_key,'metadata':metadata or {}})
    return {'charged':charge,'balance':new_balance,'model_key':model_key}


async def refund_credits(user_id: str, amount: int, reason: str, metadata: dict|None=None) -> dict:
    if not persistence.enabled() or amount <= 0:
        return {'refunded':0}
    result=await persistence.sb_request('POST','rpc/refund_credits_atomic',{'p_user_id':user_id,'p_amount':amount})
    new_balance=result[0]['new_balance'] if result else amount
    await persistence.sb_request('POST','credit_transactions',{'user_id':user_id,'amount':amount,'kind':'refund','operation':reason,'model_key':'','metadata':metadata or {}})
    return {'refunded':amount,'balance':new_balance}

def build_router(auth_dependency, admin_dependency):
    router=APIRouter(prefix='/api/billing',tags=['billing'])

    @router.get('/plans')
    async def plans(user=Depends(auth_dependency)):
        await ensure_default_plans()
        if persistence.enabled():
            return {'currency':CURRENCY,'plans':await persistence.sb_request('GET','subscription_plans',params={'select':'*','active':'eq.true','order':'price_ghs.asc'})}
        return {'currency':CURRENCY,'plans':DEFAULT_PLANS}

    @router.get('/me')
    async def me(user=Depends(auth_dependency)):
        uid=(user or {}).get('sub') if isinstance(user,dict) else None
        if not uid: raise HTTPException(401,'Signed-in user required.')
        return await user_billing_summary(uid)

    @router.post('/checkout/paystack')
    async def checkout(req: CheckoutRequest, request: Request, user=Depends(auth_dependency)):
        uid=(user or {}).get('sub'); email=(user or {}).get('email')
        if not uid or not email: raise HTTPException(401,'A signed-in account with an email address is required.')
        if req.payment_method != 'paystack': raise HTTPException(400,'Google Pay is a separate integration and cannot be routed through the Paystack checkout endpoint.')
        await ensure_default_plans(); plan=await _get_plan(req.plan_slug)
        if not plan or not plan.get('active', True): raise HTTPException(404,'Plan not found.')
        if float(plan.get('price_ghs') or 0) <= 0: raise HTTPException(400,'The Free plan does not require checkout.')
        if not PAYSTACK_SECRET: raise HTTPException(503,'Paystack is not configured yet.')
        if not plan.get('paystack_plan_code'):
            await _sync_paystack_plan(plan); plan=await _get_plan(req.plan_slug) or plan
        callback=req.callback_url or os.getenv('VIDIGEN_BILLING_CALLBACK_URL') or str(request.base_url).rstrip('/')+'/api/billing/callback'
        metadata={'type':'vidigen_subscription','user_id':uid,'plan_slug':plan['slug'],'plan_id':plan.get('id'),'monthly_credits':int(plan['monthly_credits']),'payment_method':'paystack'}
        payload={'email':email,'amount':_amount_subunit(plan['price_ghs']),'currency':CURRENCY,'callback_url':callback,'metadata':metadata}
        if plan.get('paystack_plan_code'): payload['plan']=plan['paystack_plan_code']
        data=await _paystack('POST','/transaction/initialize',payload=payload)
        reference=data.get('reference')
        if persistence.enabled():
            await persistence.sb_request('POST','payments',{'user_id':uid,'plan_id':plan.get('id'),'provider':'paystack','reference':reference,'amount':plan['price_ghs'],'currency':CURRENCY,'status':'pending','metadata':metadata})
        return {'authorization_url':data.get('authorization_url'),'access_code':data.get('access_code'),'reference':reference,'plan':plan['slug']}

    @router.post('/checkout/credits')
    async def checkout_credits(req: CreditPackRequest, request: Request, user=Depends(auth_dependency)):
        uid=(user or {}).get('sub'); email=(user or {}).get('email')
        if not uid or not email: raise HTTPException(401,'A signed-in account with an email address is required.')
        price=30.0
        if not PAYSTACK_SECRET: raise HTTPException(503,'Paystack is not configured yet.')
        callback=req.callback_url or os.getenv('VIDIGEN_BILLING_CALLBACK_URL') or str(request.base_url).rstrip('/')+'/billing/callback'
        metadata={'type':'vidigen_credit_pack','user_id':uid,'credits':150,'amount_ghs':price,'payment_method':'paystack'}
        data=await _paystack('POST','/transaction/initialize',payload={'email':email,'amount':_amount_subunit(price),'currency':CURRENCY,'callback_url':callback,'metadata':metadata})
        reference=data.get('reference')
        if persistence.enabled():
            await persistence.sb_request('POST','payments',{'user_id':uid,'provider':'paystack','reference':reference,'amount_ghs':price,'amount':price,'currency':CURRENCY,'status':'pending','payment_method':'paystack','metadata':metadata})
        return {'authorization_url':data.get('authorization_url'),'access_code':data.get('access_code'),'reference':reference,'credits':150,'price_ghs':price}

    @router.get('/callback')
    async def billing_callback(request: Request):
        reference=request.query_params.get('trxref') or request.query_params.get('reference') or ''
        return {
            'ok': True,
            'message': 'Payment return received. Vidigen applies subscriptions and credit packs from the verified Paystack webhook.',
            'reference': reference or None,
        }

    @router.get('/paystack/verify/{reference}')
    async def verify(reference: str, user=Depends(auth_dependency)):
        uid=(user or {}).get('sub');
        if not uid: raise HTTPException(401,'Signed-in user required.')
        data=await _paystack('GET',f'/transaction/verify/{reference}')
        if data.get('status') != 'success': return {'status':data.get('status'),'reference':reference}
        if persistence.enabled():
            rows=await persistence.sb_request('GET','payments',params={'reference':f'eq.{reference}','user_id':f'eq.{uid}','select':'*','limit':'1'})
            if rows: return {'status':'success','reference':reference,'payment':rows[0]}
        return {'status':'success','reference':reference}

    @router.post('/paystack/webhook')
    async def webhook(request: Request):
        raw=await request.body(); sig=request.headers.get('x-paystack-signature','')
        if not PAYSTACK_SECRET or not sig or not hmac.compare_digest(sig,_sign(raw)):
            raise HTTPException(401,'Invalid Paystack signature.')
        event_id=hashlib.sha256(raw).hexdigest()
        if persistence.enabled():
            prior=await persistence.sb_request('GET','payment_events',params={'provider_event_id':f'eq.{event_id}','select':'id','limit':'1'})
            if prior: return {'ok':True,'duplicate':True}
        evt=json.loads(raw.decode('utf-8') or '{}'); event=evt.get('event',''); data=evt.get('data') or {}
        if persistence.enabled():
            await persistence.sb_request('POST','payment_events',{'provider':'paystack','provider_event_id':event_id,'event':event,'payload':data})
        if event == 'charge.success':
            metadata=data.get('metadata') or {}
            uid=metadata.get('user_id')
            # Paystack can legitimately retry delivery with a different webhook
            # event envelope. The event hash alone is therefore not sufficient to
            # prevent a payment reference from being credited twice.
            reference=data.get('reference')
            if persistence.enabled() and reference:
                prior_payment=await persistence.sb_request(
                    'GET','payments',
                    params={'reference':f'eq.{reference}','status':'eq.success','select':'id','limit':'1'}
                )
                if prior_payment:
                    return {'ok':True,'duplicate_payment':True,'reference':reference}
            if metadata.get('type') == 'vidigen_credit_pack':
                if uid and int(metadata.get('credits') or 0) == 150 and persistence.enabled():
                    await persistence.sb_request('POST','rpc/grant_subscription_credits_atomic',{'p_user_id':uid,'p_amount':150,'p_monthly_allowance':0})
                    ref=data.get('reference')
                    if ref:
                        await persistence.sb_request('PATCH','payments',{'status':'success','verified_at':datetime.now(timezone.utc).isoformat()},params={'reference':f'eq.{ref}'})
                return {'ok':True,'credit_pack':150}
            plan_slug=metadata.get('plan_slug')
            if not uid and data.get('customer',{}).get('customer_code'):
                rows=await persistence.sb_request('GET','subscriptions',params={'paystack_customer_code':f"eq.{data['customer']['customer_code']}",'status':'eq.active','select':'user_id,plan_id,plan_slug','limit':'1'})
                if rows: uid=rows[0]['user_id']; plan_slug=rows[0].get('plan_slug')
            if not plan_slug and data.get('plan',{}).get('plan_code'):
                rows=await persistence.sb_request('GET','subscription_plans',params={'paystack_plan_code':f"eq.{data['plan']['plan_code']}",'select':'slug','limit':'1'})
                if rows: plan_slug=rows[0]['slug']
            plan=await _get_plan(plan_slug) if plan_slug else None
            if uid and plan:
                started=datetime.now(timezone.utc); ends=started+timedelta(days=31)
                sub_data={'user_id':uid,'plan_id':plan.get('id'),'plan_slug':plan['slug'],'status':'active','started_at':started.isoformat(),'current_period_end':ends.isoformat(),'paystack_customer_code':(data.get('customer') or {}).get('customer_code'),'paystack_subscription_code':(data.get('subscription') or {}).get('subscription_code'),'updated_at':started.isoformat()}
                existing=await persistence.sb_request('GET','subscriptions',params={'user_id':f'eq.{uid}','status':'eq.active','select':'id','limit':'1'})
                if existing: await persistence.sb_request('PATCH','subscriptions',sub_data,params={'id':f"eq.{existing[0]['id']}"})
                else: await persistence.sb_request('POST','subscriptions',sub_data)
                wallet=await persistence.sb_request('GET','credit_wallets',params={'user_id':f'eq.{uid}','select':'user_id','limit':'1'})
                if not wallet:
                    await persistence.sb_request('POST','credit_wallets',{'user_id':uid,'balance':0,'monthly_allowance':0})
                await persistence.sb_request('POST','rpc/grant_subscription_credits_atomic',{'p_user_id':uid,'p_amount':int(plan['monthly_credits']),'p_monthly_allowance':int(plan['monthly_credits'])})
                ref=data.get('reference')
                if ref: await persistence.sb_request('PATCH','payments',{'status':'success','verified_at':started.isoformat()},params={'reference':f'eq.{ref}'})
        return {'ok':True}

    @router.patch('/admin/plans/{slug}')
    async def admin_adjust(slug: str, req: AdjustPlanRequest, user=Depends(admin_dependency)):
        await ensure_default_plans(); plan=await _get_plan(slug)
        if not plan: raise HTTPException(404,'Plan not found.')
        patch=req.model_dump(); patch['updated_at']=datetime.now(timezone.utc).isoformat()
        if persistence.enabled():
            rows=await persistence.sb_request('PATCH','subscription_plans',patch,params={'slug':f'eq.{slug}'})
            plan=rows[0] if rows else {**plan,**patch}
        else: plan={**plan,**patch}
        sync=None
        if PAYSTACK_SECRET and float(plan.get('price_ghs') or 0)>0:
            sync=await _sync_paystack_plan(plan)
        if persistence.enabled(): await persistence.log_admin_action(user.get('sub'),'adjust_billing_plan',{'slug':slug,'price_ghs':req.price_ghs,'monthly_credits':req.monthly_credits})
        return {'plan':plan,'paystack_sync':sync}

    @router.post('/admin/model-costs')
    async def admin_model_cost(req: dict, user=Depends(admin_dependency)):
        key=str(req.get('model_key','')).strip(); op=str(req.get('operation','video')).strip(); credits=int(req.get('credits_per_5s',0))
        if not key or op not in ('video','image','audio') or credits<1: raise HTTPException(400,'Invalid model-cost configuration.')
        if persistence.enabled():
            rows=await persistence.sb_request('PATCH','ai_model_prices',{'credits_per_5s':credits},params={'model_key':f'eq.{key}','operation':f'eq.{op}'})
            if not rows: rows=await persistence.sb_request('POST','ai_model_prices',{'model_key':key,'operation':op,'credits_per_5s':credits})
        else: rows=[{'model_key':key,'operation':op,'credits_per_5s':credits}]
        if persistence.enabled(): await persistence.log_admin_action(user.get('sub'),'adjust_model_credit_cost',{'model_key':key,'operation':op,'credits_per_5s':credits})
        return rows[0]

    @router.get('/test/config')
    async def test_config(user=Depends(auth_dependency)):
        if not PAYMENT_TEST_MODE:
            raise HTTPException(404, 'Payment test environment is disabled.')
        return {
            'enabled': True,
            'currency': CURRENCY,
            'prices_ghs': list(TEST_PAYMENT_PRICES_GHS),
            'paystack_test_key_configured': _is_paystack_test_key(),
            'google_pay_test_mode': GOOGLE_PAY_TEST_MODE,
            'note': 'Test transactions never grant production subscription entitlements.'
        }

    @router.post('/test/paystack/checkout')
    async def test_paystack_checkout(req: TestPaymentRequest, request: Request, user=Depends(auth_dependency)):
        uid=(user or {}).get('sub'); email=(user or {}).get('email')
        if not uid or not email: raise HTTPException(401,'A signed-in account with an email address is required.')
        if not PAYMENT_TEST_MODE: raise HTTPException(404,'Payment test environment is disabled.')
        if not _is_paystack_test_key():
            raise HTTPException(503,'Paystack TEST mode requires a sk_test_ secret key. Live keys are refused by the test endpoint.')
        amount=_validate_test_amount(req.amount_ghs)
        callback=req.callback_url or os.getenv('VIDIGEN_BILLING_CALLBACK_URL') or str(request.base_url).rstrip('/')+'/billing/test-callback'
        metadata={'type':'vidigen_payment_test','user_id':uid,'test_amount_ghs':amount,'payment_method':'paystack_test','production_entitlement':False}
        data=await _paystack('POST','/transaction/initialize',payload={'email':email,'amount':_amount_subunit(amount),'currency':CURRENCY,'callback_url':callback,'metadata':metadata})
        reference=data.get('reference')
        if persistence.enabled():
            await persistence.sb_request('POST','payments',{'user_id':uid,'provider':'paystack_test','reference':reference,'amount':amount,'currency':CURRENCY,'status':'test_pending','metadata':metadata})
        return {'authorization_url':data.get('authorization_url'),'access_code':data.get('access_code'),'reference':reference,'amount_ghs':amount,'mode':'TEST'}

    @router.get('/test/paystack/verify/{reference}')
    async def test_paystack_verify(reference: str, user=Depends(auth_dependency)):
        uid=(user or {}).get('sub')
        if not uid: raise HTTPException(401,'Signed-in user required.')
        if not PAYMENT_TEST_MODE: raise HTTPException(404,'Payment test environment is disabled.')
        if not _is_paystack_test_key(): raise HTTPException(503,'Paystack TEST mode requires a sk_test_ secret key.')
        data=await _paystack('GET',f'/transaction/verify/{reference}')
        result={'status':data.get('status'),'reference':reference,'mode':'TEST','production_entitlement_granted':False}
        if data.get('status')=='success' and persistence.enabled():
            await persistence.sb_request('PATCH','payments',{'status':'test_success','verified_at':datetime.now(timezone.utc).isoformat()},params={'reference':f'eq.{reference}','user_id':f'eq.{uid}'})
        return result

    @router.get('/google-pay/status')
    async def google_pay_status(user=Depends(auth_dependency)):
        enabled=os.getenv('GOOGLE_PAY_ENABLED','false').lower() in {'1','true','yes','on'}
        return {
            'enabled': enabled,
            'test_mode': GOOGLE_PAY_TEST_MODE,
            'environment': 'TEST' if GOOGLE_PAY_TEST_MODE else 'PRODUCTION',
            'processor': os.getenv('GOOGLE_PAY_PROCESSOR',''),
            'mode': 'google-pay-api-test' if GOOGLE_PAY_TEST_MODE else 'production-gated',
            'note': 'Google Pay TEST is a separate workflow. Paystack is not assumed to be the Google Pay production processor.'
        }

    return router
