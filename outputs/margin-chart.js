(() => {
  const cache = new Map();
  let selected = "", months = 3, request = 0;
  const valid = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const fmt = value => valid(value) ? value.toLocaleString("ja-JP", {maximumFractionDigits: 2}) : "算出不可";
  const esc = value => String(value ?? "未取得").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  function draw(payload) {
    const panel = document.querySelector("#marginHistoryContent");
    const all = payload.rows;
    const cutoff = new Date();
    cutoff.setUTCMonth(cutoff.getUTCMonth() - months);
    const rows = all.filter(row => Date.parse(row.date) >= cutoff.getTime());
    if (!rows.length) { panel.textContent = "この期間の信用残高履歴はありません。公表データの取得後に表示されます。"; return; }
    const last = rows.at(-1), prev = all[all.length - 2];
    const diff = prev && valid(last.ratio) && valid(prev.ratio) && Date.parse(last.date) - Date.parse(prev.date) <= 10 * 86400000 ? last.ratio - prev.ratio : null;
    panel.innerHTML = `<p>信用倍率 <strong>${fmt(last.ratio)}${valid(last.ratio) ? "倍" : ""}</strong> / 前回差 ${diff === null ? "未算出" : (diff >= 0 ? "+" : "") + diff.toFixed(2) + "倍"}</p>
      <p>買い残 ${fmt(last.buy)}株 / 売り残 ${fmt(last.sell)}株</p>
      <p>基準日 ${esc(last.date)} / 公表日 ${esc(last.publishedAt)}</p>
      ${Date.now() - Date.parse(last.date) > 7 * 86400000 ? '<p role="status">注意：最新の保存データは7日以上前です。</p>' : ''}
      <div class="history-table-wrap" role="region" tabindex="0" aria-label="信用需給の日次履歴"><table class="history-table"><thead><tr><th scope="col">基準日</th><th scope="col">信用倍率</th><th scope="col">売り残（株）</th><th scope="col">買い残（株）</th><th scope="col">公表日</th></tr></thead><tbody>${[...rows].reverse().map(row => `<tr><th scope="row">${esc(row.date)}</th><td>${valid(row.ratio) ? fmt(row.ratio) + "倍" : "算出不可"}</td><td>${fmt(row.sell)}</td><td>${fmt(row.buy)}</td><td>${esc(row.publishedAt)}</td></tr>`).join("")}</tbody></table></div>
      <p>${rows.length}観測日。日次へ移行する前の期間には週次観測が含まれます。未取得日は補間せず、売り残ゼロの倍率は算出不可です。</p>`;
  }
  async function show(code) {
    selected = String(code);
    const id = ++request, panel = document.querySelector("#marginHistoryContent");
    panel.textContent = "信用残高履歴を読み込み中です。";
    try {
      if (!/^[0-9A-Z]{4}$/.test(selected)) throw Error("code");
      const key = selected;
      if (!cache.has(key)) {
        const response = await fetch(`data/margin-history/${encodeURIComponent(key)}.json`, {cache: "no-store"});
        if (!response.ok) throw Error("fetch");
        const data = await response.json();
        if (data.schemaVersion !== 1 || data.code !== key || data.unit !== "shares" || !Array.isArray(data.rows)) throw Error("schema");
        let previousDate = "";
        for (const row of data.rows) {
          if (!/^\d{4}-\d{2}-\d{2}$/.test(row.date) || !Number.isFinite(Date.parse(row.date)) || row.date <= previousDate) throw Error("date");
          if ([row.buy, row.sell, row.ratio].some(value => value !== null && !valid(value))) throw Error("number");
          previousDate = row.date;
        }
        cache.set(key, data);
      }
      if (id === request) draw(cache.get(key));
    } catch {
      if (id !== request) return;
      panel.textContent = "信用残高履歴を取得できませんでした。未蓄積または通信エラーです。";
      const retry = document.createElement("button");
      retry.type = "button"; retry.textContent = "再読み込み";
      retry.onclick = () => { cache.delete(selected); void show(selected); };
      panel.append(retry);
    }
  }
  window.marginHistoryChart = {show};
  window.addEventListener("resize", () => {
    if (!document.querySelector("#marginHistoryPanel").hidden && cache.has(selected)) draw(cache.get(selected));
  });
  document.querySelectorAll("[data-margin-months]").forEach(button => {
    button.addEventListener("click", () => {
      months = Number(button.dataset.marginMonths);
      document.querySelectorAll("[data-margin-months]").forEach(other => other.classList.toggle("active", other === button));
      if (selected) void show(selected);
    });
  });
})();
