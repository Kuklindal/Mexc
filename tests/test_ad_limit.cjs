const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'adspower.py'), 'utf8');
const script = source.match(/AD_LIMIT = r"""([\s\S]*?)"""/)[1];
const id = 'a1835907281334303745';
const ad = {
    id, state:1, tradeType:1, coinName:'USDT', coinId:128, currency:'TJS',
    priceType:0, price:11.1, availableQuantity:500, frozenQuantity:0,
    minTradeLimit:1000, maxTradeLimit:3000, expirationTime:30,
    paymethodIds:[526], overVerify:{types:[1]}, kycLevel:2,
    tradeTerms:'existing terms', autoResponse:'existing reply',
    merchantTradeEnable:true, onlyTradeKybUser:false, display:1, adsType:0
};
const plan = {
    edit_limit:true, side:'SELL', fiat:'TJS', before_available:'500', before_frozen:'0',
    price:'11.1', minimum:'1000', before_maximum:'3000', target_maximum:'4440'
};

async function run(responses, intent=plan) {
    const calls=[];
    const fn=vm.runInNewContext(`(${script})`, {
        location:{protocol:'https:',hostname:'www.mexc.co'}, URLSearchParams,
        fetch:async (url, options)=>{
            calls.push({url,method:options.method,body:options.body?.toString()});
            assert.ok(responses.length, 'Unexpected request');
            const data=responses.shift();
            if (data instanceof Error) throw data;
            return {ok:true,status:200,json:async()=>data};
        }
    });
    return {result:JSON.parse(JSON.stringify(await fn(id,intent))),calls};
}

(async()=>{
    let r=await run([{code:0,data:ad},{code:0},
                     {code:0,data:{...ad,maxTradeLimit:4440}}]);
    assert.equal(r.result.maxTradeLimit,4440);
    assert.equal(r.calls.length,3);
    assert.equal(r.calls[1].method,'PUT');
    assert.equal(r.calls[1].url,'/api/platform/p2p/api/merchant/order');
    const body=new URLSearchParams(r.calls[1].body);
    assert.equal(body.get('id'),id);
    assert.equal(body.get('maxTradeLimit'),'4440');
    assert.equal(body.get('minTradeLimit'),'1000');
    assert.equal(body.get('price'),'11.1');
    assert.equal(body.get('quantity'),'500');
    assert.equal(body.get('oldAvailableQuantity'),'500');
    assert.equal(body.get('newAvailableQuantity'),'500');
    assert.equal(body.get('paymethodIds'),'526');
    assert.equal(body.get('tradeTerms'),'existing terms');
    assert.equal(body.get('overVerify'),JSON.stringify({types:[1]}));

    r=await run([{code:0,data:{...ad,maxTradeLimit:4440}}]);
    assert.equal(r.result.maxTradeLimit,4440);
    assert.equal(r.calls.length,1);
    r=await run([{code:0,data:{...ad,currency:'GEL'}}]);
    assert.equal(r.result.error,'ad_changed');
    assert.equal(r.calls.length,1);
    r=await run([{code:0,data:ad},{code:60064}]);
    assert.equal(r.result.error,'limit_rejected');
    assert.equal(r.calls.length,2);
    r=await run([{code:0,data:ad},{code:0},
                 {code:0,data:{...ad,maxTradeLimit:4440,price:12}}]);
    assert.equal(r.result.error,'result_mismatch');
    console.log('Advertisement limit edit: OK (mock fetch only)');
})().catch(error=>{console.error(error);process.exitCode=1;});
