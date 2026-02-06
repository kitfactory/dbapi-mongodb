# 互換性→高速化 作業計画（MongoDB 4.4 / 生SQL互換）

MongoDB 4.4 をターゲットに、生SQL互換（既存アプリの SQL を落とさない）を最優先で進めるための作業計画。

## 前提
- [x] 4.4 レプリカセットを起動（`PORT=27019 ./start4xdb.sh`）し、`MONGODB_URI`/`MONGODB_DB` を環境変数で指定
- [x] テスト環境の LD_LIBRARY_PATH（`mongodb-4.4/libssl1.1/usr/lib/x86_64-linux-gnu`）を確認
- [x] 互換性の優先 SQL（本番相当）を 2〜3 本収集し、まず落とさない形を合意する（A1〜A3）

## 想定SQL（受け入れテスト）
定番の業務アプリ（一覧/検索/集計）で頻出の形を想定した「落とさない」ための受け入れテスト。ここを先に安定させ、その後に高速化へ進む。

### A1. 詳細（ユーザー内の注文一覧: 絞り込み + 並び替え + ページング）
- SQL:
  - `SELECT o.id, o.total, o.created_at FROM orders o WHERE o.tenant_id = %s AND o.user_id = %s ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s`
- 期待: 基底テーブル（orders）の `ORDER BY` が安定し、`LIMIT/OFFSET` の適用順が正しい（ユーザー名などは別クエリで取得する想定）

### A2. 検索（ILIKE + NOT IN + DISTINCT）
- SQL:
  - `SELECT DISTINCT u.id FROM users u JOIN orders o ON u.id = o.user_id WHERE u.tenant_id = %s AND u.name ILIKE %s AND o.status NOT IN (%s, %s) ORDER BY u.id LIMIT %s OFFSET %s`
- 期待: `DISTINCT`、`NOT IN`、`ORDER BY` の列解決、文字列検索（ILIKE）が期待どおりに動く

### A3. 集計（GROUP BY + HAVING + ORDER BY alias）
- SQL:
  - `SELECT o.status, COUNT(*) AS cnt, SUM(o.total) AS total_sum FROM orders o WHERE o.tenant_id = %s AND o.created_at BETWEEN %s AND %s GROUP BY o.status HAVING cnt >= %s ORDER BY cnt DESC LIMIT %s`
- 期待: `HAVING` と `ORDER BY <select-list-alias>` が SQL セマンティクスどおりに動く

### A4. 検索（ILIKE contains: 先頭が % の部分一致）
- SQL:
  - `SELECT u.id FROM users u WHERE u.tenant_id = %s AND u.name ILIKE %s ORDER BY u.id LIMIT %s OFFSET %s`
- 例: `%(name)s = '%user1%'`
- 期待: `%...%` の部分一致が期待どおりに動く（性能は弱くなりやすい点に注意）

### A5. 一覧（深い OFFSET ページング）
- SQL:
  - `SELECT o.id, o.created_at FROM orders o WHERE o.tenant_id = %s ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s`
- 期待: 深い `OFFSET` でもセマンティクスが崩れない（性能は弱くなりやすい点に注意）

### A11. EXISTS / NOT EXISTS（非相関サブクエリ）
- SQL（EXISTS）:
  - `SELECT u.id FROM users u WHERE u.tenant_id = %s AND EXISTS (SELECT id FROM orders o WHERE o.tenant_id = %s AND o.status = %s LIMIT 1) ORDER BY u.id LIMIT %s`
- SQL（NOT EXISTS）:
  - `SELECT u.id FROM users u WHERE u.tenant_id = %s AND NOT EXISTS (SELECT id FROM orders o WHERE o.tenant_id = %s AND o.status = %s LIMIT 1) ORDER BY u.id LIMIT %s`
- 期待: **非相関**の EXISTS/NOT EXISTS が正しく評価される（このケースは非相関の受け入れ確認）

### A12. JOIN（複合等価 ON: `ON a=b AND c=d`）
- SQL:
  - `SELECT o.id, o.created_at, u.name FROM orders o JOIN users u ON o.user_id = u.id AND o.tenant_id = u.tenant_id WHERE o.tenant_id = %s ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s`
- 期待: JOIN 条件が複合でも正しく結合できる（MongoDB 4.4 の `$lookup pipeline` を使用）

### A13. UNION ALL（連結後 OFFSET）
- SQL:
  - `SELECT id FROM users WHERE tenant_id = %s UNION ALL SELECT id FROM archived_users WHERE tenant_id = %s ORDER BY id LIMIT %s OFFSET %s`
- 期待: `ORDER BY/LIMIT/OFFSET` が連結後の全体結果に対して適用される

### A14. DISTINCT（複数列）
- SQL:
  - `SELECT DISTINCT tenant_id, status FROM orders WHERE tenant_id IN (%s, %s) ORDER BY tenant_id, status LIMIT %s OFFSET %s`
- 期待: 複数列の重複排除が SQL セマンティクスどおりに動く

### A15. COUNT(DISTINCT)
- SQL（単体集計）:
  - `SELECT COUNT(DISTINCT o.user_id) AS uniq_users FROM orders o WHERE o.tenant_id = %s`
- SQL（GROUP BY 集計）:
  - `SELECT o.tenant_id, COUNT(DISTINCT o.user_id) AS uniq_users FROM orders o GROUP BY o.tenant_id ORDER BY o.tenant_id LIMIT %s`
- 期待: `COUNT(DISTINCT ...)` が単体集計/グループ集計で正しく動く

### A16. JOIN + GROUP BY（ユーザー別注文件数）
- SQL:
  - `SELECT u.id, COUNT(o.id) AS order_cnt FROM users u LEFT JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id WHERE u.tenant_id = %s GROUP BY u.id HAVING order_cnt >= %s ORDER BY order_cnt DESC, u.id ASC LIMIT %s OFFSET %s`
- 期待: JOIN 後に `GROUP BY/HAVING/ORDER BY/LIMIT/OFFSET` を SQL セマンティクスどおりに適用し、`COUNT(o.id)` が NULL を除外して集計される

### A17. 相関サブクエリ（EXISTS / NOT EXISTS）
- SQL（EXISTS）:
  - `SELECT u.id FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.tenant_id = u.tenant_id) ORDER BY u.id LIMIT %s`
- SQL（NOT EXISTS）:
  - `SELECT u.id FROM users u WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.tenant_id = u.tenant_id) ORDER BY u.id LIMIT %s`
- 期待: 外側行に依存する `EXISTS/NOT EXISTS` を正しく評価する（対象は単一サブクエリ・単一テーブルの相関条件）

### A18. UNION（重複除去）
- SQL:
  - `SELECT tenant_id FROM users WHERE tenant_id IN (%s, %s) UNION SELECT tenant_id FROM archived_users WHERE tenant_id IN (%s, %s) ORDER BY tenant_id LIMIT %s OFFSET %s`
- 期待: `UNION` で重複除去した結果集合に対して `ORDER BY/LIMIT/OFFSET` を適用する

### A19. JOIN（非等価 ON 条件）
- SQL:
  - `SELECT u.id, o.id FROM users u JOIN orders o ON u.id = o.user_id AND o.total >= u.min_total WHERE u.tenant_id = %s ORDER BY u.id, o.id LIMIT %s OFFSET %s`
- 期待: `ON` の比較条件（`>=`, `<=`, `>`, `<`, `<>`）を `$lookup pipeline + $expr` で評価できる

### A20. WITH（非再帰 CTE）
- SQL:
  - `WITH active_users AS (SELECT id, tenant_id FROM users WHERE tenant_id = %s) SELECT id FROM active_users WHERE id >= %s ORDER BY id LIMIT %s OFFSET %s`
- 期待: `WITH` の CTE をインラインサブクエリとして展開し、外側 SELECT の `WHERE/ORDER BY/LIMIT/OFFSET` を SQL セマンティクスどおりに適用する（`WITH RECURSIVE` は未対応）

### A21. スカラサブクエリ（比較式）
- SQL:
  - `SELECT id FROM users WHERE tenant_id = %s AND score >= (SELECT AVG(score) FROM users WHERE tenant_id = %s) ORDER BY id LIMIT %s`
- 期待: 比較式右辺の非相関サブクエリを単一値として評価し、`>=` で正しく比較できる

### A22. UNION ALL（3項以上の多段連結）
- SQL:
  - `SELECT id FROM users WHERE tenant_id = %s UNION ALL SELECT id FROM archived_users WHERE tenant_id = %s UNION ALL SELECT id FROM users WHERE tenant_id = %s ORDER BY id LIMIT %s OFFSET %s`
- 期待: 3 項以上の `UNION ALL` を連結し、全体に対して `ORDER BY/LIMIT/OFFSET` を適用できる

## 互換性（C1: 既存機能の SQL セマンティクス修正）
- [x] JOIN を含む `ORDER BY` の列解決（右テーブル/alias を正しく解決する）
- [x] JOIN の `LIMIT/OFFSET` 適用順（`$skip` → `$limit`。`ORDER BY` があればその後に適用）
- [x] `UNION ALL` の `ORDER BY/LIMIT/OFFSET` を「連結後の全体」に適用（列/alias 解決も含む）
- [x] 互換性テスト追加（translation テスト + MongoDB 4.4 実通信テスト）

## 互換性（C2: 頻出構文の追加）
- [x] `IS NULL` / `IS NOT NULL`（`IS NOT NULL` は `NOT (x IS NULL)` として解釈）
- [x] `NOT`（限定: `NOT IN` / `NOT LIKE` / `NOT (a=b)` / `NOT (x IS NULL)`）
- [x] `EXISTS` / `NOT EXISTS`（非相関サブクエリ）
- [x] `SELECT DISTINCT`（複数列）
- [x] JOIN の複合等価条件（`ON a=b AND c=d`）
- [x] `ORDER BY` の出力列 alias 対応（GROUP BY の集計 alias: `ORDER BY cnt` など）
- [x] `COUNT(DISTINCT ...)`
- [x] `UNION ALL` の `OFFSET`
- [x] JOIN を含む `GROUP BY` + 集計 alias（`COUNT(o.id)` / `HAVING order_cnt`）
- [x] 相関 `EXISTS/NOT EXISTS`（単一サブクエリ・単一テーブル）
- [x] `UNION`（重複除去）
- [x] 非等価 JOIN（`ON` の比較条件）
- [x] `WITH`（非再帰 CTE）
- [x] スカラサブクエリ（比較式）
- [x] `UNION/UNION ALL` 多段連結（3 項以上）

## JOIN拡張（J1: Join-first 実装結果）
Join-first 方針に基づく JOIN の未対応領域（RIGHT/FULL OUTER）の実装結果。

### A23. RIGHT OUTER JOIN（単一等価 ON）
- SQL:
  - `SELECT u.id, o.id FROM users u RIGHT OUTER JOIN orders o ON u.id = o.user_id ORDER BY o.id LIMIT %s OFFSET %s`
- 期待: 右テーブル（orders）を基準に欠損行を保持し、`ORDER BY/LIMIT/OFFSET` を全体結果に適用する

### A24. RIGHT OUTER JOIN（複合等価 ON）
- SQL:
  - `SELECT u.id, o.id FROM users u RIGHT OUTER JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id WHERE o.tenant_id = %s ORDER BY o.id LIMIT %s OFFSET %s`
- 期待: 複合 ON 条件でも RIGHT JOIN の欠損保持が崩れない

### A25. FULL OUTER JOIN（単一等価 ON）
- SQL:
  - `SELECT u.id, o.id FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id ORDER BY u.id, o.id LIMIT %s OFFSET %s`
- 期待: 左右いずれかにのみ存在する行を保持し、重複行を作らない

### A26. FULL OUTER JOIN（複合等価 ON）
- SQL:
  - `SELECT u.id, o.id FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id ORDER BY u.id, o.id LIMIT %s OFFSET %s`
- 期待: 複合 ON 条件でも FULL JOIN の左右欠損行を正しく保持する

### A27. RIGHT/FULL JOIN + GROUP BY
- SQL:
  - `SELECT COALESCE(u.tenant_id, o.tenant_id) AS tenant_id, COUNT(*) AS cnt FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id GROUP BY COALESCE(u.tenant_id, o.tenant_id) ORDER BY tenant_id LIMIT %s`
- 期待: JOIN 後に `GROUP BY/HAVING` を適用しても件数整合が崩れない

### 実装タスク（着手順）
- [x] J1-1 RIGHT OUTER JOIN（単一等価 ON）を実装（`$lookup` ベース）
- [x] J1-2 RIGHT OUTER JOIN（複合等価 ON）を実装（`$lookup pipeline`）
- [x] J1-3 FULL OUTER JOIN（単一等価 ON）を実装（LEFT + RIGHT 片側差分統合）
- [x] J1-4 FULL OUTER JOIN（複合等価 ON）を実装
- [x] J1-5 RIGHT/FULL + `ORDER BY/LIMIT/OFFSET` の適用順を固定（FULL の `UNION ALL` を DB 側 `$unionWith` で実行）
- [x] J1-6 RIGHT/FULL + `GROUP BY/HAVING` の集計整合を固定（A27: `COALESCE(...) + COUNT(*)` パターン）
- [x] J1-7 非対応組み合わせ（RIGHT/FULL + 非等価 ON など）を `[mdb][E2]` で明示エラー化（`RIGHT_JOIN_NON_EQ` / `FULL_JOIN_NON_EQ` / `FULL_JOIN_CHAIN`）
- [x] J1-8 translation/dbapi/acceptance テストを追加（A23〜A27）
- [x] J1-9 4.4 実通信ベンチでスロークエリ有無を確認（A23〜A27）

## 高速化（P1: 押し込み / パイプライン最適化）
- [ ] JOIN で `$project` をパイプラインへ押し込み（返却フィールドを削減）※`LIMIT` が大きい場合に自動有効化、強制は `MONGO_DBAPI_JOIN_PROJECT=1`
- [x] `$match` の前倒し（左テーブル条件は `$lookup` 前、右テーブル条件は `$lookup` pipeline へは既定 OFF）
- [x] `UNION ALL` を `$unionWith` へ（MongoDB 4.4 で DB 側連結）
- [x] `JOIN + GROUP BY` の `COUNT(<join-col>)` で `unwind` を回避（`$lookup` 配列を `$addFields` で件数化）
- [ ] GROUP BY/HAVING で不要列の除去（`$project` の整理）

## 計測結果（受け入れSQLベンチ）
環境: MongoDB 4.4 / `users=20000, orders=120000, repeat=15`（`scripts/benchmark_acceptance.py`）

- A1（詳細: ユーザー内注文一覧）: `3.2ms` → `3.1ms`（差はほぼ無し）
- A2（DISTINCT + NOT IN）: `192.0ms` → `195.1ms`（差はほぼ無し）
- A4（ILIKE contains）: `5.9ms` → `5.9ms`（差はほぼ無し）
- A5（深い OFFSET）: `5.2ms` → `5.6ms`（差はほぼ無し）
- A11（EXISTS）: `8.0ms` → `7.7ms`（約 3.8% 改善）
- A12（JOIN 複合ON）: `29.5ms` → `28.6ms`（約 3.1% 改善）
- A13（UNION ALL + OFFSET）: `20.3ms` → `8.8ms`（約 2.3x）
- A14（DISTINCT 複数列）: `44.9ms` → `44.3ms`（約 1.3% 改善）
- A15（COUNT DISTINCT）: `28.2ms` → `29.0ms`（差はほぼ無し）
- A16（JOIN + GROUP BY）: `282.2ms` → `266.1ms`（約 5.7% 改善）
- A17（相関 EXISTS/NOT EXISTS）: `2409.8ms` → `2434.4ms`（差はほぼ無し、かつ遅い）
- A18（UNION 重複除去）: `47.9ms` → `48.0ms`（差はほぼ無し）
- A19（JOIN 非等価 ON）: `244.9ms` → `255.6ms`（差はほぼ無し）
- U1（UNION ALL）: `20.0ms` → `10.6ms`（約 1.9x）

### JOIN拡張（A23〜A27）再計測（MongoDB 4.4 / `users=10000, orders=50000, repeat=3`）
- A23（RIGHT OUTER JOIN）: `81.2ms` → `74.9ms`（約 7.8% 改善）
- A24（RIGHT OUTER JOIN 複合ON）: `68.2ms` → `67.2ms`（差はほぼ無し）
- A25（FULL OUTER JOIN）: `5018.6ms` → `3322.4ms`（約 33.8% 改善、ただし遅い）
- A26（FULL OUTER JOIN 複合ON）: `6180.2ms` → `5366.2ms`（約 13.2% 改善、ただし遅い）
- A27（FULL OUTER JOIN + GROUP BY）: `3067.3ms` → `3093.1ms`（差はほぼ無し、かつ遅い）
- 注記: FULL OUTER JOIN 系は互換性は確保したが、依然としてスロークエリ帯。実運用では `RIGHT/LEFT` 優先や事前集約を推奨。

## 高速化（P2: 取得/メモリ）
- [ ] `fetchall()` 前提の全件 `list(cursor)` を避け、バッチ取得（巨大結果でのメモリ/レイテンシ改善）
- [ ] `FROM (SELECT ...)` のインライン実行を最小化（必要時のみ materialize）
- [ ] 性能注意点と推奨インデックス（JOIN キー/WHERE/ORDER）を docs に整理

## 参考: 過去の拡張計画
- 旧 P1→P6（SQLAlchemy/async/MongoDB 5+ ウィンドウ関数等）の完了チェックは `docs/plan1.bck` に退避
