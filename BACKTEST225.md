# Nikkei 225 Score Backtest

This folder adds a point-in-time backtest for the investment score strategy.

## Project Layout

- `backtest225/data`: CSV loading and optional J-Quants client
- `backtest225/scoring`: factor and total score calculation
- `backtest225/simulation`: next-open trading simulation
- `backtest225/reporting`: CSV and HTML report output
- `data/input`: verified input CSV files
- `outputs/backtests`: generated result files

## Required CSV Files

Put these files in `data/input`.

- `prices.csv`: `date,code,open,high,low,close,volume,turnover_value`
- `nikkei225_membership.csv`: `code,start_date,end_date`
- `themes.csv`: `code,theme_name`

Optional files:

- `margin.csv`: `date,code,margin_ratio`
- `tdnet_events.csv`: `date,code,event_type`
- `earnings_events.csv`: `date,code,event_type`
- `news_counts.csv`: `date,code,news_count_7d`
- `benchmark_nikkei225.csv`: `date,open,close`

`nikkei225_membership.csv` is important. If it only contains today's Nikkei 225 names, the test has survivorship bias.

## API Keys

The code does not store API keys. If you add a fetch script later, read keys from environment variables:

- `JQUANTS_API_KEY`
- `JQUANTS_REFRESH_TOKEN`

## Run

From the repository root:

```powershell
python -m backtest225.run_backtest
```

## Prepare Inputs

Copy the theme file:

```powershell
python -m backtest225.data.build_inputs sync-themes
```

Check files:

```powershell
python -m backtest225.data.build_inputs validate
```

Check production readiness before trusting a result:

```powershell
python -m backtest225.data.build_inputs production-check --start-date 2021-01-01 --end-date 2025-12-31
```

This writes:

- `outputs/backtests/production_readiness.csv`
- `outputs/backtests/production_readiness_issues.csv`
- `outputs/backtests/membership_daily_coverage.csv`

Treat the result as production-like only after `採用履歴あり` and `価格カバーあり` are ready. `需給あり`, `開示イベントあり`, and `ニュースあり` are separate enrichment stages.

Fetch `prices.csv` from J-Quants after `nikkei225_membership.csv` is ready:

```powershell
python -m backtest225.data.build_inputs fetch-prices-jquants --from-date 2019-01-01 --to-date 2025-12-31
```

Or use the PowerShell helper. It asks for the API key without saving it to a file:

```powershell
.\scripts\run_jquants_price_fetch.ps1 -FromDate 2023-01-01 -ToDate 2025-12-31
```

If environment variables are not visible from Codex, test the key without saving it:

```powershell
.\scripts\test_jquants_key.ps1
```

Create a membership template:

```powershell
python -m backtest225.data.build_inputs membership-template
```

Do not run the final backtest with the sample membership row. Replace it with verified historical Nikkei 225 membership periods first.

Seed verified constituent-change events from saved official Nikkei PDF releases:

```powershell
python -m backtest225.data.build_inputs seed-membership-changes --overwrite
```

This creates `nikkei225_membership_changes.csv` and `nikkei225_membership_unresolved_sources.csv`. The main `nikkei225_membership.csv` is not overwritten until all required source years are confirmed.

Build the period-based membership file after the change events are reviewed:

```powershell
python -m backtest225.data.build_inputs build-membership-history --start-date 2021-01-01 --end-date 2025-12-31 --overwrite
```

The command backs into the 2021-2025 history from the current 225 constituents and the verified official change events. Keep `nikkei225_membership.current_2026_backup.csv` as a rollback copy of the current-only universe.

For a quick pipeline test, create a simplified current-membership universe:

```powershell
python -m backtest225.data.build_inputs fetch-current-nikkei225-membership --start-date 2023-01-01 --overwrite
```

This avoids the sample row, but it is not a survivorship-bias-free historical universe.

## Production-like Verification Order

1. Replace `nikkei225_membership.csv` with verified point-in-time membership periods.
2. Run `production-check` and confirm daily active membership is around 225.
3. Re-fetch prices for every code that appears in the historical membership file.
4. Run the backtest first without margin, TDnet, earnings, or news.
5. Add `margin.csv`, then TDnet/earnings events, then `news_counts.csv`, one layer at a time.

Do not backfill optional data with guesses. If a row was not knowable after that day's close, leave it out and let the reports show it as missing.

To include historical news counts:

```powershell
python -m backtest225.run_backtest --include-news
```

Outputs:

- `outputs/backtests/summary.csv`
- `outputs/backtests/trades.csv`
- `outputs/backtests/daily_equity.csv`
- `outputs/backtests/score_band_performance.csv`
- `outputs/backtests/yearly_performance.csv`
- `outputs/backtests/parameters.csv`
- `outputs/backtests/report.html`

## Point-in-time Rules

- Scores are calculated after each close.
- Buy and sell orders execute at the next business day's open.
- Buy signal: previous score below 75 and current score at least 75.
- Sell signal: current score below 65.
- Maximum positions: 5, equal allocation.
- Trading cost: round trip 0.2%.

No placeholder market data is generated. Missing files or columns are reported as errors.
