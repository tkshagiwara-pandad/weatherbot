# Weather Trading Bot — CLAUDE.md

Polymarket の天気マーケットに自動売買するボット。
Open-Meteo の 4 モデルアンサンブル予報と市場価格の乖離（edge）を狙う。

---

## ファイル構成

```
weatherbot/
├── main.py                  # エントリポイント・メインループ
├── config.json              # 設定（取引額・都市・スキャン間隔など）
├── .env                     # WALLET_PRIVATE_KEY（git 管理外）
├── com.weatherbot.plist     # macOS launchd 自動起動設定
├── requirements.txt
├── bot/
│   ├── market.py            # Polymarket API クライアント（Gamma / CLOB）
│   ├── weather.py           # Open-Meteo 予報クライアント + 精度ログ
│   ├── strategy.py          # シグナル生成・PnL 追跡・自己学習
│   ├── trader.py            # スキャン→判断→注文実行のオーケストレーター
│   └── notifier.py          # Telegram 通知
└── signer/
    └── signer.py            # EIP-712 署名・デイリー上限管理
```

---

## 設定（config.json）

| キー | 説明 |
|------|------|
| `max_trade_usdc` | 1注文の上限 USDC |
| `daily_limit_usdc` | 1日の取引上限 USDC |
| `min_edge` | 最低エッジ（例: 0.05 = 5%） |
| `trade_amount_usdc` | 1注文の基本サイズ USDC |
| `max_days_ahead` | 何日先までのマーケットを対象にするか |
| `scan_interval_minutes` | 通常スキャン間隔（分） |
| `peak_scan_interval_minutes` | ピーク時スキャン間隔（分） |
| `peak_hours_utc` | ピーク時間帯（UTC 時）の配列 |
| `focus_cities` | 取引対象都市リスト（空=全都市） |
| `watch_cities` | 毎日 Telegram に天気予報を送る都市 |
| `telegram_token` / `telegram_chat_id` | Telegram 通知設定 |

---

## 起動方法

### 手動起動（テスト・デバッグ）

```bash
# ドライラン（注文なし）
python main.py --dry-run

# ライブ取引
WALLET_PRIVATE_KEY=0x... python main.py
```

### launchd（macOS 自動起動）

```bash
# 登録
cp com.weatherbot.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.weatherbot.plist

# 状態確認
launchctl list | grep weatherbot   # PID が表示されれば稼働中

# 停止 / 再起動
launchctl unload ~/Library/LaunchAgents/com.weatherbot.plist
launchctl load   ~/Library/LaunchAgents/com.weatherbot.plist

# ログ確認
tail -f bot.log
```

> ライブ取引にするには plist の `--dry-run` を削除し、
> `EnvironmentVariables` に `WALLET_PRIVATE_KEY` を設定する。

---

## ログファイル

| ファイル | 内容 |
|----------|------|
| `bot.log` | 実行ログ（launchd 経由の stdout/stderr） |
| `trades.jsonl` | 発注済みトレード・解決後 PnL |
| `forecast_log.jsonl` | 各都市×日付の予報値（4モデル別 tmax 含む） |
| `accuracy_log.jsonl` | 解決後の実測値 vs 予報値（翌日以降に記録） |

---

## 主要コンポーネント

### 天気予報（bot/weather.py）

- **4モデルアンサンブル**: `jma_seamless`, `ecmwf_ifs025`, `gfs_seamless`, `icon_seamless`
- モデル間の標準偏差を `temp_std_c` として記録 → strategy の sigma に反映
- 座標は ICAO 空港コード（Polymarket の解決観測点に合わせる）
- `prefetch()`: スキャン前に一括取得してキャッシュ
- `record_actuals()`: `forecast_log.jsonl` の過去日付エントリに対し archive API で実測値を取得 → `accuracy_log.jsonl` に追記

### シグナル生成（bot/strategy.py）

対象マーケット:
1. **気温**: 上下方向 or 範囲（例: "above 80°F"）→ ロジスティック関数で確率推定
2. **降水（雨）**: 特定日付の binary 降水マーケット → `precip_prob` を使用
3. **降雪**: WMO コードが雪（71-77, 85-86）なら `precip_prob`、それ以外は 0

除外: 月間集計・イベント系・金額条件付き・exact 1°F マーケット

**自己学習**: 解決済みトレード 20 件以上で win_rate を計算し `edge_adjustment` を増減。

### 注文実行（bot/trader.py）

スキャン → エッジ計算 → book 取得（ask 確認） → 以下を全て満たす場合に発注:
- `ask_size × best_ask ≥ 1.0 USDC`（ゴーストクォート排除）
- bid/ask 両建て、スプレッド ≤ 20%
- real_edge（ask 価格ベース）≥ min_edge + edge_adjustment

---

## 設計上の注意点

- **座標は空港（ICAO）**: Polymarket は空港の観測値で解決するため、市街地座標を使うと予報がずれる
- `jma_seamless` は GSM（約 20km 解像度）で空港周辺と整合しやすい
- tenki.jp は MSM（約 5km）で市街地向け → 空港解決マーケットでは必ずしも精度が高くない
- **`--dry-run` でも署名サービスは初期化される**（ダミー秘密鍵使用）
- デイリーリミットは `signer.py` 側で管理。`signer.daily_remaining()` で残額確認可能
