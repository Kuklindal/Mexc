// Run with: node tests/test_adspower_dom.cjs. Uses fixtures, never a real browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '..', 'adspower.py'), 'utf8')
  .match(/ORDER_VIEW = r"""([\s\S]*?)"""/)[1];
const inspect = new Function('document', 'location', 'getComputedStyle', 'orderNo', 'click',
  `return (${source})(orderNo, click)`);
const order = 'd1823135371445801984';
let clicks = 0;
const button = {innerText: 'Проверка пройдена', getClientRects: () => [1],
  getAttribute: () => null, click: () => clicks++};
const heading = {innerText: 'Ожидание проверки', getClientRects: () => [1]};
const panel = {innerText: 'Ожидание проверки', getClientRects: () => [1],
  querySelectorAll: selector => selector === 'button' ? [button] : [heading]};
const document = {body: panel, querySelectorAll: () => [panel]};
const location = {protocol: 'https:', hostname: 'www.mexc.com',
  pathname: '/ru-RU/buy-crypto/order-processing', search: '?id=' + order};
const run = (click = false) => inspect(document, location, () => ({visibility: 'visible'}), order, click).state;

assert.equal(run(), 'ready'); // No number in standalone page text, exact URL and heading.
assert.equal(clicks, 0);
location.search = '?id=d9999999999999999999';
assert.equal(run(true), 'blocked');
location.search = '?id=' + order + '&id=d9999999999999999999';
assert.equal(run(true), 'blocked');
location.search = '?id=' + order;
panel.innerText += ' d9999999999999999999';
assert.equal(run(true), 'missing');
panel.innerText = 'Ожидание проверки';
heading.innerText = 'Ордер отменён';
assert.equal(run(true), 'missing');
heading.innerText = 'Ожидание проверки';
button.disabled = true;
assert.equal(run(true), 'blocked');
button.disabled = false;
location.hostname = 'mexc.com.evil.test';
assert.equal(run(true), 'blocked');
location.hostname = 'www.mexc.com';
assert.equal(clicks, 0);
assert.equal(run(true), 'clicked');
assert.equal(clicks, 1);

location.pathname = '/ru-RU/buy-crypto/control';
assert.equal(run(true), 'missing'); // A merchant dialog still needs its full number.
panel.innerText += ' ' + order;
assert.equal(run(), 'ready');
assert.equal(clicks, 1);
console.log('AdsPower DOM guards: OK (fixture clicks only)');
