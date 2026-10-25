const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const vm = require('node:vm');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '../outputs');
const html = fs.readFileSync(path.join(root, 'investment-candidate-app.html'), 'utf8');
for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);

const dataset = JSON.parse(fs.readFileSync(path.join(root, 'data/latest-candidates.json'), 'utf8'));
const template = (dataset.searchUniverse || dataset.candidates)[0];
const topix = Array.from({length:1500}, (_, i) => ({code:String(1000+i), name:'fixture',
  isTopix:true, isStandard:i===2, exchangeSection:i===2?'スタンダード':'プライム'}));
const standardOnly = Array.from({length:1099}, (_, i) => ({code:String(3000+i), name:'fixture',
  isTopix:false, isStandard:true, exchangeSection:'スタンダード'}));
dataset.universe = {id:'topix-standard', expectedCount:2599};
dataset.topixComponents = topix;
dataset.standardComponents = [topix[2], ...standardOnly];
dataset.listedComponents = [...topix, ...standardOnly];
dataset.sources.standard = {...dataset.sources.topix};
dataset.searchUniverse = [
  {...template, ...topix[1], name:'TOPIXテスト', margin:2, score:80},
  {...template, ...topix[2], name:'重複テスト', margin:4, score:70},
  {...template, ...standardOnly[1], name:'スタンダードテスト', margin:null, score:60,
    supply:0, dataQuality:50, dataWarnings:['信用: 信用倍率未取得']},
];
dataset.candidates = [];
const complete = dataset.searchUniverse[0];
Object.assign(complete, {actualDps:20, actualDpsAsOf:'2026-03-31', forecastDps:40,
  dps:40, dpsKind:'forecast', dpsAsOf:'2027/03', dividendYield:0.02, dividendYieldKind:'forecast',
  dividendPayoutRatio:0.2, dividendPayoutRatioStatus:'calculated', doe:0.02,
  fundamentals:{asOf:'2026-03-31', eps:100, bps:1000},
  financialAcquisition:{edinet:{status:'available',at:'2026-10-19'},yahoo:{status:'available',at:'2026-10-19'}}});
Object.assign(dataset.searchUniverse[1], {actualDps:0,forecastDps:0,dps:0,dpsKind:'forecast',dividendYield:0,dividendYieldKind:'forecast',fundamentals:{}});
Object.assign(dataset.searchUniverse[2], {actualDps:null,forecastDps:null,dps:null,dividendYield:null,dividendYieldKind:'forecast',fundamentals:{},
  financialAcquisition:{edinet:{status:'not-attempted'},yahoo:{status:'error',at:'2026-10-19'}}});
dataset.financialCoverage = {standard:{total:2,fields:{per:{available:1,total:2,missing:1}}}};
delete dataset.topixPrices;
dataset.priceHistoryBundle = {status:'available',generatedAt:dataset.generatedAt,scoreVersion:dataset.scoreVersion,
  factorVersion:dataset.factorVersion,priceBasis:dataset.priceBasis,highLookbackDays:252,
  shards:[{key:'10',url:'data/price-history/topix-10.json'}, {key:'30',url:'data/price-history/topix-30.json'}]};
const server = http.createServer((req,res) => {
  const file = path.resolve(root, '.'+decodeURIComponent(req.url.split('?')[0]));
  if (!file.startsWith(root+path.sep)) {res.writeHead(403).end();return;}
  fs.readFile(file,(error,body) => {
    if (error) {res.writeHead(404).end();return;}
    res.setHeader('Content-Type',file.endsWith('.json')?'application/json':'text/html');res.end(body);
  });
});

(async () => {
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser = await chromium.launch({headless:true,channel:process.env.PLAYWRIGHT_CHANNEL || 'msedge'});
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const loadedPricePrefixes = [];
    await page.route('**/data/price-history/*.json', route => {
      const prefix = route.request().url().split('topix-').pop().split('.json')[0];
      loadedPricePrefixes.push(prefix);
      const chartHistory = Array.from({length:90},(_,i)=>{
        const day = new Date('2026-07-01T00:00:00Z');day.setUTCDate(day.getUTCDate()+i);
        return [day.toISOString().slice(0,10),100+i,105+i,95+i,102+i,100000];
      });
      return route.fulfill({json:{generatedAt:dataset.generatedAt,scoreVersion:dataset.scoreVersion,
        factorVersion:dataset.factorVersion,priceBasis:dataset.priceBasis,highLookbackDays:252,shardKey:prefix,
        prices:dataset.searchUniverse.filter(row=>row.code.startsWith(prefix)).map(row=>({code:row.code,chartHistory}))}});
    });
    await page.route('**/data/latest-candidates.json',route => route.fulfill({json:dataset}));
    const market = JSON.parse(fs.readFileSync(path.join(root,'data/market-environment.json'),'utf8'));
    market.vixTechnical = {rows:[],status:'unavailable'};
    await page.route('**/data/market-environment.json',route => route.fulfill({json:market}));
    await page.goto(`http://127.0.0.1:${server.address().port}/investment-candidate-app.html`);
    try {
      await page.waitForFunction(() => document.querySelectorAll('#candidateRows tr[data-code]').length===3);
    } catch (error) {
      console.error(await page.locator('#candidateStatus').innerText(), await page.locator('#candidateRows').innerText(), errors);
      throw error;
    }
    const codes = () => page.locator('#candidateRows tr[data-code]').evaluateAll(rows => rows.map(row=>row.dataset.code).sort());
    assert.deepEqual(await codes(),['1001','1002','3001']);
    await page.locator('#candidateRows tr[data-code="1001"]').click();
    await page.locator('#fundamentalTab').click();
    assert.match(await page.locator('#fundamentalGrid').innerText(),/年間配当・実績\s*20.0円/);
    assert.match(await page.locator('#fundamentalGrid').innerText(),/年間配当・会社予想\s*40.0円/);
    assert.match(await page.locator('#fundamentalGrid').innerText(),/2026\/03\/31/);
    assert.match(await page.locator('#detailQualityPanel').innerText(),/確認済み/);
    assert.match(await page.locator('#qualityList').innerText(),/スタンダードの情報取得率/);
    await page.locator('#candidateRows tr[data-code="1002"]').click();
    assert.match(await page.locator('#fundamentalGrid').innerText(),/年間配当・会社予想\s*0.0円/);
    await page.locator('#standardUniverse').click();
    assert.deepEqual(await codes(),['1002','3001']);
    assert.equal(await page.locator('#standardUniverse').getAttribute('aria-pressed'),'true');
    assert.match(await page.locator('#candidateRows tr[data-code="3001"] td').nth(4).innerText(),/未取得/);
    assert.equal(await page.locator('#metricMargin').innerText(),'4.0');
    await page.locator('#topixUniverse').click();
    assert.deepEqual(await codes(),['1001','1002']);
    await page.locator('#allUniverse').click();
    assert.deepEqual(await codes(),['1001','1002','3001']);
    await page.locator('#searchBox').fill('スタンダードテスト');
    assert.equal(await page.locator('#searchClear').isVisible(), true);
    await page.locator('#searchClear').click();
    assert.equal(await page.locator('#searchBox').inputValue(), '');
    assert.deepEqual(await codes(),['1001','1002','3001']);
    assert.equal(await page.locator('#searchClear').isVisible(), false);
    await page.locator('#searchBox').fill('スタンダードテスト');
    await page.locator('#searchSuggestions [data-code="3001"]').click();
    assert.match(await page.locator('#selectedCode').innerText(),/スタンダード/);
    assert(!(await page.locator('#selectedCode').innerText()).includes('TOPIX'));
    await page.locator('#chartTab').click();
    await page.waitForFunction(()=>document.querySelector('#chartLegend').textContent.includes('ローソク足'));
    assert.deepEqual(loadedPricePrefixes,['30']);
    await page.locator('#fundamentalTab').click();
    assert.match(await page.locator('#fundamentalGrid').innerText(),/年間配当・会社予想\s*未取得/);
    assert.match(await page.locator('#detailQualityPanel').innerText(),/順次取得待ち/);
    await page.locator('#candidateRows [data-compare-code="3001"]').click();
    assert.match(await page.locator('#comparePanel').innerText(),/スタンダードテスト/);
    await page.locator('#searchBox').fill('');
    const shots = path.resolve(__dirname,'../work/tmp'); fs.mkdirSync(shots,{recursive:true});
    await page.locator('.segmented[aria-label="対象銘柄"]').screenshot({path:path.join(shots,'standard-selector-desktop.png')});
    for (const width of [390,320]) {
      await page.setViewportSize({width,height:844});
      await page.locator('#mobileFilterOpen').click();
      await page.locator('#standardUniverse').click();
      await page.locator('#mobileFilterSheet').screenshot({path:path.join(shots,`standard-filter-${width}.png`)});
      await page.locator('#mobileFilterApply').click();
      assert.deepEqual(await codes(),['1002','3001']);
      assert.equal(await page.locator('#mobileFilterCount').isVisible(),true);
      assert.equal(await page.locator('#mobileCandidateCards .mobile-stock-card').count(),2);
      await page.locator('#mobileSearchBox').fill('スタンダードテスト');
      assert.equal(await page.locator('#mobileSearchClear').isVisible(), true);
      await page.locator('.mobile-search-row').screenshot({path:path.join(shots,`search-clear-${width}.png`)});
      await page.locator('#mobileSearchClear').click();
      assert.equal(await page.locator('#mobileSearchBox').inputValue(), '');
      assert.equal(await page.locator('#mobileCandidateCards .mobile-stock-card').count(),2);
      assert.equal(await page.locator('#mobileSearchClear').isVisible(), false);
      const standardCard = page.locator('#mobileCandidateCards .mobile-stock-card').filter({hasText:'スタンダードテスト'});
      assert.match(await standardCard.innerText(),/未取得/);
      assert(!(await standardCard.innerText()).includes('0.0倍'));
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await page.screenshot({path:path.join(shots,`standard-ranking-${width}.png`)});
      await page.locator('#mobileFilterOpen').click();
      await page.locator('#allUniverse').click();
      await page.locator('#mobileFilterApply').click();
    }
    dataset.sources.standard.status = 'stale-fallback';
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#candidateStatus').textContent.includes('データ待ち'));
    assert.equal(await page.locator('#candidateRows tr[data-code]').count(),0);
    dataset.sources.standard.status = 'available';
    const rows = dataset.searchUniverse;
    const bundle = {schemaVersion:1,status:'available',generatedAt:dataset.generatedAt,
      scoreVersion:dataset.scoreVersion,factorVersion:dataset.factorVersion,recordCount:rows.length,
      shards:[{url:'data/search-universe/part-000.json',rowCount:2},
        {url:'data/search-universe/part-001.json',rowCount:1}]};
    await page.route('**/data/latest-candidates.json',route => route.fulfill({json:{...dataset,searchUniverse:[],searchUniverseBundle:bundle}}));
    await page.route('**/data/search-universe/*.json',route => {
      const second = route.request().url().endsWith('part-001.json');
      return route.fulfill({json:{schemaVersion:1,generatedAt:dataset.generatedAt,
        scoreVersion:dataset.scoreVersion,factorVersion:dataset.factorVersion,
        rows:second?rows.slice(2):rows.slice(0,2)}});
    });
    await page.reload();
    await page.waitForFunction(()=>document.querySelectorAll('#candidateRows tr[data-code]').length===3);
    assert.deepEqual(await codes(),['1001','1002','3001']);
    await page.route('**/data/search-universe/part-001.json',route => route.fulfill({json:{
      schemaVersion:1,generatedAt:'wrong-date',scoreVersion:dataset.scoreVersion,
      factorVersion:dataset.factorVersion,rows:rows.slice(2),
    }}));
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#candidateStatus').textContent.includes('データ読込失敗'));
    assert.equal(await page.locator('#candidateRows tr[data-code]').count(),0);
    assert.deepEqual(errors,[]);
    console.log('PASS: Standard membership, scope filters, mobile widths, stale source gate and candidate shards');
  } finally {if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
