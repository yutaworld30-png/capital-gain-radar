(() => {
  const cache = new Map();
  let selected = "", months = 3, request = 0;
  const valid = value => typeof value === "number" && Number.isFinite(value) && value >= 0;
  const fmt = value => valid(value) ? value.toLocaleString("ja-JP", {maximumFractionDigits: 2}) : "算出不可";
  const esc = value => String(value ?? "未取得").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  function chart(rows, balances) {
    const w = Math.max(320, document.querySelector("#marginHistoryContent").clientWidth), h = 220, left = 62, right = w - 20, bottom = 185;
    const start = Date.parse(rows[0].date), end = Date.parse(rows.at(-1).date);
    const x = row => end === start ? (left + right) / 2 : left + (Date.parse(row.date) - start) / (end - start) * (right - left);
    const values = rows.flatMap(row => balances ? [row.buy, row.sell] : [row.ratio]).filter(valid);
    const max = Math.max(1, ...values) * 1.1, y = value => bottom - value / max * 155;
    let body = `<path d="M${left} 20V${bottom}H${right}" fill="none" stroke="#8796a8"/>`;
    for (let i = 0; i <= 4; i++) {
      const value = max * i / 4;
      body += `<text x="${left - 5}" y="${y(value) + 4}" text-anchor="end" font-size="11">${balances ? fmt(value / 1000) : fmt(value)}</text>`;
    }
    body += `<text x="${left}" y="12" font-size="11">${balances ? "千株" : "倍"}</text>`;
    let previous = null;
    const barWidth = Math.min(12, (right - left) / Math.max(rows.length, 1) / 3);
    rows.forEach(row => {
      const title = esc(`${row.date} 信用倍率 ${fmt(row.ratio)} / 買い残 ${fmt(row.buy)}株 / 売り残 ${fmt(row.sell)}株`);
      if (balances) {
        [[row.buy, "#0f8f6a", -barWidth], [row.sell, "#bc3b42", 0]].forEach(([value, color, offset]) => {
          if (valid(value)) body += `<rect x="${x(row) + offset}" y="${y(value)}" width="${barWidth}" height="${bottom - y(value)}" fill="${color}"><title>${title}</title></rect>`;
        });
      } else {
        if (valid(row.ratio)) {
          if (previous && Date.parse(row.date) - Date.parse(previous.date) <= 10 * 86400000) body += `<path d="M${x(previous)} ${y(previous.ratio)}L${x(row)} ${y(row.ratio)}" stroke="#286bc4" stroke-width="2"/>`;
          body += `<circle cx="${x(row)}" cy="${y(row.ratio)}" r="4" fill="#286bc4"><title>${title}</title></circle>`;
          previous = row;
        } else previous = null;
      }
    });
    body += `<text x="${left}" y="210" font-size="12">${esc(rows[0].date)}</text><text x="${right}" y="210" text-anchor="end" font-size="12">${esc(rows.at(-1).date)}</text>`;
    return `<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${balances ? "信用買い残と売り残" : "信用倍率"}の観測日別推移" style="width:100%;display:block">${body}</svg>`;
  }
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
      ${chart(rows, false)}${chart(rows, true)}
      <p>緑：買い残　赤：売り残（棒グラフは千株）</p>
      <p>${rows.length}観測日を表示。日次へ移行する前の期間には週次観測が含まれます。未取得日は補間しません。売り残ゼロの倍率は算出不可です。</p>
      <details><summary>観測データ一覧</summary><div style="overflow-x:auto"><table><thead><tr><th>基準日</th><th>倍率</th><th>買い残（株）</th><th>売り残（株）</th><th>公表日</th></tr></thead><tbody>${[...rows].reverse().map(row => `<tr><td>${esc(row.date)}</td><td>${fmt(row.ratio)}</td><td>${fmt(row.buy)}</td><td>${fmt(row.sell)}</td><td>${esc(row.publishedAt)}</td></tr>`).join("")}</tbody></table></div></details>`;
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
