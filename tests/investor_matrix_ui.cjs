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
    res.setHeader('Content-Type', file.endsWith('.json') ? 'application/json' : 'text/html');
    res.end(body);
  });
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
    const page = await browser.newPage({viewport: {width: 1280, height: 900}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const dataset = JSON.parse(fs.readFileSync(path.join(root, 'data/latest-candidates.json'), 'utf8'));
    const stockRows = dataset.searchUniverse || dataset.candidates || [];
    stockRows.forEach(row => {
      row.fundamentals = {...row.fundamentals, companyProfile: {companyName: '企業情報テスト',
        headquarters: '<img src=x onerror=alert(1)>', employees: 1234, employeesAsOf: '2026-03-31'}};
    });
    await page.route('**/data/latest-candidates.json', route => route.fulfill({json: dataset}));
    await page.route('**/data/score-history-v2.json', route => route.fulfill({json: {
      schemaVersion: 3, scoreVersion: dataset.scoreVersion, factorVersion: dataset.factorVersion,
      rowFormat: ['code', 'score'], snapshots: [],
    }}));
    await page.route('**/data/valuation-history/*.json', route => {
      const prefix = route.request().url().split('/').pop().slice(0, 2);
      const stocks = Object.fromEntries(stockRows.filter(row => String(row.code).startsWith(prefix)).map(row => [String(row.code),
        Array.from({length: 30}, (_, index) => [`2026-01-${String(index + 1).padStart(2, '0')}`, 20 + index / 10, 2, 0, 'forecast', '2025-12-31', null])]));
      return route.fulfill({json: {schemaVersion: 1, basis: 'observed-edinet-valuation-v1',
        rowFormat: ['date', 'per', 'pbr', 'dividendYield', 'dividendYieldKind', 'fundamentalsAsOf', 'observedAt'], stocks}});
    });
    const market = JSON.parse(fs.readFileSync(path.join(root, 'data/market-environment.json'), 'utf8'));
    market.indicators.topixFutures = {label: 'TOPIX先物', value: 4081.5, previous: 4070,
      change: 11.5, changeRate: 11.5 / 4070, asOf: '2026-10-16', status: 'available',
      source: 'JPX / 大阪取引所', note: '大阪取引所 / 2026年12月限 / 清算値'};
    market.groups.futures = ['nikkei225Futures', 'topixFutures'];
    market.indicators.nikkei225.value = 65324.29;
    market.usSectors = {status: 'available', asOf: '2026-10-16', basis: '米国ETFの確定終値比・公式業種指数ではありません。',
      missingSymbols: [], rows: [{industry: '情報技術', symbol: 'XLK', changeRate: .03},
        {industry: 'エネルギー', symbol: 'XLE', changeRate: -.02}]};
    await page.route('**/data/market-environment.json', route => route.fulfill({json: market}));
    const analysis = JSON.parse(fs.readFileSync(path.join(root, 'data/nikkei225-analysis.json'), 'utf8'));
    analysis.distributionMode = 'private-cloud';
    const valuationBasis = 'nikkei-weighted-average';
    analysis.per = {
      status: 'available', asOf: '2026-10-15', valuationBasis, calculationVersion: 2,
      reference: {date: '2026-10-15', weightedPer: 16.79, weightedPbr: 1.83, valuationBasis,
        close: 64425.95, eps: 3837.16, bps: 35205.44,
        lowerMultiple: 16, lowerPrice: 61394.59, upperMultiple: 17, upperPrice: 65231.75},
      rows: [
        {date: '2026-10-14', weightedPer: 17.26, weightedPbr: 1.88, valuationBasis,
          close: 67744.29, eps: 3924.93, bps: 36034.20},
        {date: '2026-10-15', weightedPer: 16.79, weightedPbr: 1.83, valuationBasis,
          close: 64425.95, eps: 3837.16, bps: 35205.44},
      ],
    };
    analysis.investorFlows = {
      status: 'available', scope: '東京・名古屋二市場合計（金額）', rows: [
        {periodStart: '2026-09-21', periodEnd: '2026-09-25', flows: {foreign: {net100mYen: -12.5}}},
        {periodStart: '2026-09-28', periodEnd: '2026-10-02', flows: {
          foreign: {net100mYen: 123.45}, individual: {net100mYen: -8},
          investmentTrust: {net100mYen: 0}, trustBank: {net100mYen: null},
          proprietary: {net100mYen: 45.6},
        }},
      ],
    };
    analysis.nikkeiMarginDaily = {
      status: 'available', scope: '日経225現行構成銘柄の銘柄別信用残高合計',
      rows: [
        {date: '2026-10-06', buyBalanceThousandShares: 500000, sellBalanceThousandShares: 250000, marginRatio: 2, publishedAt: '2026-10-07'},
        {date: '2026-10-07', buyBalanceThousandShares: 600000, sellBalanceThousandShares: 300000, marginRatio: 2, publishedAt: '2026-10-08'},
      ],
    };
    analysis.margin = {status: 'available', scope: '東京・名古屋二市場合計', rows: [
      {weekEnd: '2026-10-02', buyBalanceThousandShares: 3765572, sellBalanceThousandShares: 354386, marginRatio: 10.6256,
        sellBalance100mYen: 7507.25, buyBalance100mYen: 65235.52, amountMarginRatio: 8.689669},
    ]};
    await page.route('**/data/nikkei225-analysis.json', route => route.fulfill({json: analysis}));
    await page.goto(`http://127.0.0.1:${server.address().port}/investment-candidate-app.html`);
    await page.waitForFunction(() => document.querySelectorAll('#nikkeiInvestorTable tbody tr').length === 2);
    await page.waitForFunction(() => document.querySelector('#companyProfileGrid').textContent.includes('企業情報テスト'));
    assert.equal(await page.locator('#companyProfileGrid img').count(), 0);
    assert.match(await page.locator('#companyProfileGrid').textContent(), /1,234人/);
    await page.waitForFunction(() => document.querySelector('#stockPerHistoryLabel').textContent.includes('30件'));
    assert.match(await page.locator('#stockPerHistoryLabel').textContent(), /21\.45倍/);
    assert.match(await page.locator('#stockYieldHistoryLabel').textContent(), /0\.00%/);
    await page.locator('[data-valuation-range="1m"]').click();
    assert.equal(await page.locator('[data-valuation-range="1m"]').getAttribute('aria-pressed'), 'true');
    await page.locator('[data-valuation-range="all"]').click();
    const valuation = await page.locator('#nikkeiValuationRows').textContent();
    assert.match(valuation, /16倍/);
    assert.match(valuation, /17倍/);
    assert.match(valuation, /1\.83倍/);
    assert(!valuation.includes('指数ベース'));
    assert.match(await page.locator('#nikkeiEpsHeading').textContent(), /3,837\.16円/);
    const latestValuation = page.locator('#nikkeiValuationHistory tbody tr').first();
    assert.deepEqual((await latestValuation.locator('td').allTextContents()).slice(-3), ['1.83', '3,837.16', '35,205.44']);
    assert(!html.includes('data-nikkei-indicator="per"'));
    const chartInk = await page.locator('#nikkeiEpsChart').evaluate(canvas => {
      const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
      return Array.from(data).some(value => value > 0);
    });
    assert(chartInk, 'EPS chart is blank');
    const rows = page.locator('#nikkeiInvestorTable tbody tr');
    assert.match(await rows.nth(0).textContent(), /2026\/10\/02/);
    assert.match(await rows.nth(1).textContent(), /2026\/09\/25/);
    const cells = rows.nth(0).locator('td');
    assert.equal(await cells.nth(0).textContent(), '+123.45');
    assert.equal(await cells.nth(1).textContent(), '-8');
    assert.equal(await cells.nth(2).textContent(), '0');
    assert.equal(await cells.nth(3).textContent(), '--');
    assert.equal(await cells.nth(4).textContent(), '+45.6');
    assert.equal(await cells.nth(0).getAttribute('class'), 'flow-positive');
    assert.equal(await cells.nth(1).getAttribute('class'), 'flow-negative');
    assert.equal(await cells.nth(3).getAttribute('class'), 'flow-neutral');
    assert.equal(await rows.nth(1).locator('td').nth(4).textContent(), '--');
    assert.match(await page.locator('#nikkeiMarginLatest').textContent(), /2026\/10\/02/);
    assert.match(await page.locator('#nikkeiMarginLatest').textContent(), /8\.69倍/);
    assert.match(await page.locator('#nikkeiMarginLatest').textContent(), /65,235\.52億円/);
    await page.locator('[data-nikkei-margin-view="market"]').click();
    assert.match(await page.locator('#nikkeiMarginLatest').textContent(), /2026\/10\/02/);
    assert.equal(await page.locator('[data-nikkei-margin-view="market"]').getAttribute('aria-pressed'), 'true');
    await page.locator('[data-nikkei-margin-view="nikkei"]').click();
    assert.match(await page.locator('#nikkeiMarginLatest').textContent(), /2026\/10\/07/);
    const screenshots = path.resolve(__dirname, '../work/tmp');
    fs.mkdirSync(screenshots, {recursive: true});
    await page.locator('#companyProfileSection').screenshot({path: path.join(screenshots, 'company-profile-desktop.png')});
    await page.locator('.valuation-history').screenshot({path: path.join(screenshots, 'valuation-archive-desktop.png')});
    await page.locator('.analysis-investor-card').screenshot({path: path.join(screenshots, 'investor-matrix-desktop.png')});
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#mobileBottomNav [data-mobile-page="market"]').click();
    await page.locator('button[data-mobile-market-view="industry"]').click();
    await page.locator('[data-sector-market="us"]').click();
    assert.match(await page.locator('#industryMarketUp').textContent(), /情報技術.*XLK/);
    assert.match(await page.locator('#industryMarketDown').textContent(), /エネルギー.*XLE/);
    assert.match(await page.locator('#industryMarketNote').textContent(), /公式業種指数ではありません/);
    await page.locator('#industryMarketPanel').screenshot({path: path.join(screenshots, 'us-sectors-mobile.png')});
    await page.locator('[data-sector-market="japan"]').click();
    assert.equal(await page.locator('[data-sector-market="japan"]').getAttribute('aria-pressed'), 'true');
    await page.locator('button[data-mobile-market-view="summary"]').click();
    await page.waitForSelector('#marketGrid .market-index-row');
    assert.equal(await page.locator('#marketGrid canvas').count(), 0);
    assert.equal(await page.locator('#marketGrid .market-index-row').count(), 9);
    assert.deepEqual(await page.locator('#marketGrid [data-market-key]').evaluateAll(rows => rows.map(row => row.dataset.marketKey)),
      ['nikkei225', 'topix', 'nikkei225Futures', 'topixFutures', 'sp500', 'nasdaq', 'vix', 'usdJpy', 'wtiCrudeOil']);
    const future = page.locator('#marketGrid [data-market-key="topixFutures"]');
    assert.match(await future.textContent(), /4,081.50/);
    assert.match(await future.textContent(), /OSE 清算値.*2026年12月限/);
    assert.equal(await future.locator('.market-change-amount').isVisible(), true);
    assert.match(await page.locator('#marketGrid [data-market-key="nikkei225"] .market-indicator-value').textContent(), /65,324.29/);
    await page.locator('#marketMoreButton').click();
    assert.equal(await page.locator('#marketGrid .market-index-row').count(), 14);
    assert.equal(await page.locator('#marketMoreButton').getAttribute('aria-expanded'), 'true');
    await page.locator('#marketMoreButton').click();
    assert.equal(await page.locator('#marketGrid .market-index-row').count(), 9);
    assert.equal(await page.locator('#mobilePortfolioTabs').isVisible(), false);
    for (const width of [390, 320, 768, 1280]) {
      await page.setViewportSize({width, height: 844});
      const bounds = await page.evaluate(() => ({
        contentWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth,
        overflowing: Array.from(document.querySelectorAll('body *')).filter(element => {
          const rect = element.getBoundingClientRect(); return rect.width && rect.right > innerWidth + 1;
        }).slice(0, 12).map(element => ({id: element.id, tag: element.tagName, className: element.className, width: element.getBoundingClientRect().width})),
      }));
      assert(bounds.contentWidth <= bounds.viewportWidth, `horizontal overflow: ${JSON.stringify(bounds)}`);
      assert(await page.locator('#marketGrid .market-indicator-value').first().evaluate(e => parseFloat(getComputedStyle(e).fontSize) >= 20));
    }
    await page.setViewportSize({width: 390, height: 844});
    await page.screenshot({path: path.join(screenshots, 'market-summary-mobile.png')});
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('button[data-mobile-market-view="technical"]').click();
    await page.evaluate(() => {
      document.body.dataset.mobilePage = 'market';
      document.body.dataset.mobileMarketView = 'technical';
    });
    const valuationLayout = await page.locator('#nikkeiValuationSection').evaluate(section => ({
      section: section.getBoundingClientRect().width,
      table: section.querySelector('table').getBoundingClientRect().width,
      cells: Array.from(section.querySelector('tbody tr').children).map(cell => ({
        text: cell.textContent, x: cell.getBoundingClientRect().x, width: cell.getBoundingClientRect().width,
      })),
    }));
    assert(valuationLayout.table <= valuationLayout.section + 1, JSON.stringify(valuationLayout));
    await page.locator('#nikkeiValuationSection').screenshot({path: path.join(screenshots, 'nikkei-valuation-mobile.png')});
    await page.evaluate(() => {
      document.body.dataset.mobilePage = 'market';
      document.body.dataset.mobileMarketView = 'stats';
      document.body.dataset.mobileStatView = 'investor';
    });
    await page.evaluate(() => { document.body.dataset.mobileStatView = 'margin'; });
    await page.locator('#nikkeiAnalysisPanel .analysis-detail-card').first().screenshot({path: path.join(screenshots, 'nikkei-margin-daily-mobile.png')});
    await page.locator('[data-nikkei-margin-view="market"]').click();
    await page.locator('#nikkeiAnalysisPanel .analysis-detail-card').first().screenshot({path: path.join(screenshots, 'market-margin-weekly-mobile.png')});
    await page.evaluate(() => { document.body.dataset.mobileStatView = 'investor'; });
    const dimensions = await page.locator('#nikkeiInvestorTableWrap').evaluate(element => ({
      content: element.scrollWidth, viewport: element.clientWidth,
      visible: getComputedStyle(element).display !== 'none',
    }));
    assert(dimensions.visible && dimensions.content > dimensions.viewport, JSON.stringify(dimensions));
    await page.locator('.analysis-investor-card').screenshot({path: path.join(screenshots, 'investor-matrix-mobile.png')});
    analysis.per.calculationVersion = 1;
    analysis.per.reference.eps = 3019.02;
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#nikkeiPerNote').textContent.includes('計算基準を更新'));
    assert(!(await page.locator('#nikkeiEpsHeading').textContent()).includes('3,019'));
    assert.match(await page.locator('#nikkeiValuationRows').textContent(), /取得できていません/);
    assert.deepEqual(errors, []);
    console.log('PASS: mobile summary, US sectors, amount-based margin, company profile, valuation archive, investor matrix; no page errors');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
