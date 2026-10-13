const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('adspower.py', 'utf8');
const script = source.match(/AD_PUBLISH = r"""([\s\S]*?)"""/)[1];
const advNo = 'a1835908380845933569';

async function scenario(initialState, responseCode = 0) {
    let state = initialState;
    const requests = [];
    const context = {
        location: {protocol: 'https:', hostname: 'www.mexc.co'},
        URLSearchParams,
        fetch: async (url, options) => {
            requests.push({url, options});
            if (url.endsWith('/switch')) {
                if (responseCode === 0) state = 1;
                return {ok: true, status: 200, json: async () => ({code: responseCode})};
            }
            return {ok: true, status: 200, json: async () => ({code: 0, data: {
                id: advNo, state, tradeType: 0, currency: 'TJS', coinName: 'USDT',
                availableQuantity: 1000,
            }})};
        },
    };
    const publish = vm.runInNewContext(`(${script})`, context);
    return {result: await publish(advNo, {side: 'BUY', fiat: 'TJS'}), requests};
}

(async () => {
    const closed = await scenario(0);
    assert.equal(closed.result.state, 1);
    assert.equal(closed.requests.length, 3);
    assert.equal(closed.requests[1].url, '/api/platform/p2p/api/merchant/order/switch');
    assert.equal(closed.requests[1].options.method, 'PUT');
    assert.equal(closed.requests[1].options.body.get('id'), advNo);
    assert.equal(closed.requests[1].options.body.get('state'), 'OPEN');
    assert.equal(closed.requests[1].options.body.get('authVersion'), 'v2');

    const open = await scenario(1);
    assert.equal(open.result.state, 1);
    assert.equal(open.requests.length, 1);

    const rejected = await scenario(0, 60017);
    assert.equal(rejected.result.error, 'publish_rejected');
    assert.equal(rejected.requests.length, 2);
    console.log('Eflp ad publication: OK (mock fetch only)');
})().catch(error => { console.error(error); process.exitCode = 1; });
