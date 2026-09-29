const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(require('node:path').join(__dirname, '..', 'adspower.py'), 'utf8');
const script = source.match(/AD_QUANTITY = r"""([\s\S]*?)"""/)[1];
const id = 'a1781101617440872449';
const ad = {id, coinName:'USDT',currency:'RUB',tradeType:1,availableQuantity:260,overVerify:{types:[1],otherText:''}};
const plan = {adv_no:id,fiat:'RUB',before_available:'260',quantity:'107.4738',target_available:'367.4738',over_verify:'{"types":[1]}'};

async function run(responses, intent=plan) {
    const calls=[];
    const fn=vm.runInNewContext(`(${script})`, {
        location:{protocol:'https:',hostname:'www.mexc.com'}, URLSearchParams,
        fetch:async (url, options)=>{
            calls.push({url,method:options.method,body:options.body?.toString()});
            assert.ok(responses.length, 'Unexpected additional request');
            const body=responses.shift();
            return {ok:true,status:200,json:async()=>body};
        }
    });
    return {result:JSON.parse(JSON.stringify(await fn(id,intent))),calls};
}

(async()=>{
    let r=await run([{code:0,data:ad},{code:0},{code:0,data:{...ad,availableQuantity:367.4738}}]);
    assert.equal(r.result.availableQuantity,367.4738);
    assert.equal(r.calls.length,3);
    assert.equal(r.calls[1].url,'/api/platform/p2p/api/merchant/order/quantity');
    assert.equal(r.calls[1].method,'POST');
    assert.deepEqual([...new URLSearchParams(r.calls[1].body)], [['id',id],['quantity','107.4738']]);
    assert.ok(r.calls.every(c=>!c.url.includes('save_or_update')));

    const frozenPlan={...plan,before_frozen:'0',before_total:'260',target_total:'367.4738'};
    r=await run([{code:0,data:{...ad,availableQuantity:160,frozenQuantity:100}},
                 {code:0},
                 {code:0,data:{...ad,availableQuantity:167.4738,frozenQuantity:200}}],frozenPlan);
    assert.equal(r.calls.filter(c=>c.method==='POST').length,1);
    assert.equal(r.result.frozenQuantity,200);
    r=await run([{code:0,data:{...ad,availableQuantity:160,frozenQuantity:100}},
                 {code:0},
                 {code:0,data:{...ad,availableQuantity:167.4738,frozenQuantity:150}}],frozenPlan);
    assert.equal(r.result.error,'result_mismatch');

    const buy={...ad,tradeType:0,availableQuantity:20,overVerify:null};
    const buyPlan={...plan,side:'BUY',before_available:'20',quantity:'100',
        target_available:'120',over_verify:'null'};
    r=await run([{code:0,data:buy},{code:0},{code:0,data:{...buy,availableQuantity:120}}],buyPlan);
    assert.equal(r.result.availableQuantity,120);
    assert.deepEqual([...new URLSearchParams(r.calls[1].body)], [['id',id],['quantity','100']]);

    for(const changed of [{...ad,overVerify:null},{...ad,availableQuantity:259},{...ad,tradeType:0}]) {
        r=await run([{code:0,data:changed}]);
        assert.equal(r.result.error,'ad_changed');
        assert.equal(r.calls.length,1);
    }
    r=await run([{code:0,data:ad},{code:0},{code:0,data:{...ad,availableQuantity:367.4738,overVerify:null}}]);
    assert.equal(r.result.error,'result_mismatch');
    assert.equal(r.calls.filter(c=>c.method==='POST').length,1);
    r=await run([{code:0,data:ad}],null);
    assert.equal(r.calls[0].method,'GET');
    assert.equal(r.calls.length,1);
    console.log('Quantity-only requests: OK (mock fetch only)');
})().catch(error=>{console.error(error);process.exitCode=1;});
