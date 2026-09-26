const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const runtimeRequire = createRequire(process.env.PLAYWRIGHT_PACKAGE || require.resolve('playwright'));
const {chromium} = runtimeRequire('playwright');
const root = path.resolve(__dirname, '../outputs');
const html = fs.readFileSync(path.join(root, 'investment-candidate-app.html'), 'utf8');
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const server = http.createServer((req, res) => {
  const file = path.resolve(root, '.' + decodeURIComponent(req.url.split('?')[0]));
  if (!file.startsWith(root + path.sep)) {res.writeHead(403).end(); return;}
  fs.readFile(file, (error, body) => {
    if (error) {res.writeHead(404).end(); return;}
    res.setHeader('Content-Type', file.endsWith('.js') ? 'application/javascript' : file.endsWith('.json') ? 'application/json' : 'text/html');
    res.end(body);
  });
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless:true, channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/data/margin-history/*.json', route => {
      const code = route.request().url().split('/').pop().replace('.json','');
      const rows = Array.from({length:20}, (_, i) => {
        const date = new Date(); date.setUTCDate(date.getUTCDate() - (19-i)*7);
        return {date:date.toISOString().slice(0,10), buy:200+i*10, sell:i===10?0:100, ratio:i===10?null:(200+i*10)/100, publishedAt:'2026-09-24'};
      });
      return route.fulfill({json:{schemaVersion:1,unit:'shares',code,rows}});
    });
    await page.goto(`http://127.0.0.1:${server.address().port}/investment-candidate-app.html`);
    await page.waitForFunction(() => document.querySelector('#selectedName').textContent.length > 1);
    for (const width of [1440,390]) {
      await page.setViewportSize({width,height:900});
      // Open detail using the app's own tab handlers, independent of mobile screen routing.
      await page.locator('#marginHistoryTab').evaluate(button => button.click());
      await page.waitForSelector('#marginHistoryContent svg', {state:'attached'});
      assert.equal(await page.locator('#marginHistoryContent svg').count(),2);
      await page.locator('[data-margin-months="12"]').evaluate(button => button.click());
      await page.waitForFunction(() => document.querySelector('#marginHistoryContent').textContent.includes('20週分'));
      await page.locator('#fundamentalTab').evaluate(button => button.click());
      assert.equal(await page.locator('#marginHistoryPanel').evaluate(panel => panel.hidden),true);
      await page.locator('#scoreHistoryTab').evaluate(button => button.click());
      await page.locator('#marginHistoryTab').evaluate(button => button.click());
      await page.waitForSelector('#marginHistoryContent svg', {state:'attached'});
      // Isolate the real detail panel for responsive visual QA without modifying source.
      await page.evaluate(() => {
        document.body.classList.add('mobile-detail-open');
      });
      fs.mkdirSync(path.resolve(__dirname, '../work/tmp'), {recursive:true});
      await page.locator('#stockDetailPanel').screenshot({path:path.resolve(__dirname, `../work/tmp/margin-${width}.png`)});
    }
    await page.unroute('**/data/margin-history/*.json');
    await page.route('**/data/margin-history/*.json', route => route.fulfill({status:503,body:'unavailable'}));
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#selectedName').textContent.length > 1);
    await page.locator('#marginHistoryTab').evaluate(button => button.click());
    await page.waitForFunction(() => document.querySelector('#marginHistoryContent').textContent.includes('取得できません'));
    await page.locator('#fundamentalTab').evaluate(button => button.click());
    assert.equal(await page.locator('#marginHistoryPanel').evaluate(panel => panel.hidden),true);
    if (process.env.MARGIN_REAL_DIR) {
      await page.unroute('**/data/margin-history/*.json');
      await page.route('**/data/margin-history/*.json', route => {
        const code = route.request().url().split('/').pop().replace('.json','');
        const data = JSON.parse(fs.readFileSync(path.join(process.env.MARGIN_REAL_DIR, 'margin-history', `${code}.json`),'utf8'));
        return route.fulfill({json:data});
      });
      await page.reload();
      await page.waitForFunction(() => document.querySelector('#selectedName').textContent.length > 1);
      await page.locator('#marginHistoryTab').evaluate(button => button.click());
      await page.waitForFunction(() => document.querySelector('#marginHistoryContent').textContent.includes('5週分'));
      const content = await page.locator('#marginHistoryContent').textContent();
      assert(content.includes('2026-09-18') && content.includes('2026-09-25'));
      await page.evaluate(() => document.body.classList.add('mobile-detail-open'));
      await page.locator('#stockDetailPanel').screenshot({path:path.resolve(__dirname, '../work/tmp/margin-real-mobile.png')});
      console.log('PASS: official five-week data and publication dates rendered');
    }
    assert.deepEqual(errors, []);
    console.log('PASS: inline syntax, charts, ranges, mobile/desktop tab transitions; no page errors');
  } finally {if(browser) await browser.close(); await new Promise(resolve => server.close(resolve));}
})().catch(error => {console.error(error);process.exitCode=1;});
