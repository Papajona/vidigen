const GOOGLE_PAY_SCRIPT='https://pay.google.com/gp/p/js/pay.js';
let scriptPromise=null;

function loadScript(){
  if(window.google?.payments?.api?.PaymentsClient) return Promise.resolve();
  if(scriptPromise) return scriptPromise;
  scriptPromise=new Promise((resolve,reject)=>{
    const s=document.createElement('script');
    s.src=GOOGLE_PAY_SCRIPT;
    s.async=true;
    s.onload=()=>resolve();
    s.onerror=()=>reject(new Error('Google Pay TEST library could not be loaded. Check HTTPS/network access.'));
    document.head.appendChild(s);
  });
  return scriptPromise;
}

export async function runGooglePayTest(amountGhs){
  await loadScript();
  if(!window.google?.payments?.api?.PaymentsClient) throw new Error('Google Pay API is unavailable in this browser.');
  const amount=Number(amountGhs).toFixed(2);
  const client=new window.google.payments.api.PaymentsClient({environment:'TEST'});
  const request={
    apiVersion:2,
    apiVersionMinor:0,
    allowedPaymentMethods:[{
      type:'CARD',
      parameters:{allowedAuthMethods:['PAN_ONLY','CRYPTOGRAM_3DS'],allowedCardNetworks:['AMEX','DISCOVER','INTERAC','JCB','MASTERCARD','VISA']},
      tokenizationSpecification:{
        type:'PAYMENT_GATEWAY',
        parameters:{
          gateway:import.meta.env.VITE_GOOGLE_PAY_TEST_GATEWAY||'example',
          gatewayMerchantId:import.meta.env.VITE_GOOGLE_PAY_TEST_GATEWAY_MERCHANT_ID||'exampleGatewayMerchantId'
        }
      }
    }],
    merchantInfo:{merchantName:import.meta.env.VITE_GOOGLE_PAY_TEST_MERCHANT_NAME||'Vidigen AI TEST'},
    transactionInfo:{countryCode:'GH',currencyCode:'GHS',totalPriceStatus:'FINAL',totalPrice:amount}
  };
  const ready=await client.isReadyToPay({apiVersion:2,apiVersionMinor:0,allowedPaymentMethods:request.allowedPaymentMethods});
  if(!ready.result) throw new Error('Google Pay TEST reports that this device/browser is not ready for the configured test payment method.');
  const paymentData=await client.loadPaymentData(request);
  return {ok:true,amountGhs:Number(amount),paymentData,mode:'TEST',charged:false};
}
