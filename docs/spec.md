# dbapi-mongodb 仕様

## 前提: サポートする SQL 構文（MongoDB 4.4 ターゲット）
- 目的: 生SQL互換を優先し、MongoDB 4.4 上で実運用できる範囲を先に固める。
- 対応（4.4）: `SELECT/INSERT/UPDATE/DELETE`、`CREATE/DROP TABLE`（コレクション作成/削除）、`CREATE/DROP INDEX`、`WHERE` の比較 (`=`/`<>`/`>`/`<`/`<=`/`>=`)、`AND`、`OR`、`IN`、`IS NULL/IS NOT NULL`、`NOT`（限定: `NOT IN` / `NOT LIKE` / `NOT (a=b)` / `NOT (x IS NULL)` / `NOT EXISTS`）、`BETWEEN`、`LIKE`（`%`/`_` を `$regex` に変換）、`ILIKE`、正規表現リテラル、`SELECT DISTINCT`（複数列）、`ORDER BY`、`LIMIT`、`OFFSET`、INNER/LEFT JOIN（最大 3 段、`ON a=b AND c=d` の複合等価、`ON` の比較条件を含む）、RIGHT OUTER JOIN（単一 JOIN のみ）、FULL OUTER JOIN（単一 JOIN・等価 ON（単一/複合））、`GROUP BY` + 集約（COUNT/SUM/AVG/MIN/MAX/COUNT DISTINCT）+ `HAVING`（集計 alias を解決、JOIN を含む集計にも適用）、`UNION ALL` / `UNION`（3 項以上を含む多段連結、連結後 `ORDER BY/LIMIT/OFFSET`）、サブクエリ（`WHERE IN/EXISTS/NOT EXISTS`（相関 EXISTS/NOT EXISTS は単一サブクエリ・単一テーブルの相関条件を対応）、比較式でのスカラサブクエリ、`FROM (SELECT ...)`）、`WITH`（非再帰 CTE）、簡易 CASE 集計（`SUM(CASE WHEN ... THEN ... ELSE ... END)` の単一 WHEN）。FULL OUTER JOIN + `GROUP BY/HAVING` は `COALESCE(left_col, right_col)` と `COUNT(*)` のパターンをサポート。
- 非対応（4.4）: ウィンドウ関数（MongoDB 5+ 専用）、非等価 ON を含む RIGHT/FULL OUTER JOIN、複数 JOIN を含む RIGHT/FULL JOIN 連鎖、FULL OUTER JOIN + 集計の複雑形（複数集計式・`COALESCE` 以外のグループキー等）、相関サブクエリのうち複雑形（複数テーブル/複数ネスト/SELECT 列での相関）、再帰 CTE（`WITH RECURSIVE`）、`UNION`/`UNION ALL` の混在連結など。
- 参考（MongoDB 5+）: ウィンドウ関数（`ROW_NUMBER/RANK/DENSE_RANK`）は 5+ 専用拡張として付録に記載する。
- プレースホルダー: `%s` と `%(name)s` の両方に対応（不足/余剰は `[mdb][E4]`）。
- `SELECT *` 時のフィールド順: コレクションのフィールド名をアルファベット順で返す。明示列指定時は SQL 記述順で返す。JOIN 時の `SELECT *` は左テーブル→右テーブルの順でアルファベット順。
- パーサー: SQLGlot を使用し、将来のサブクエリ対応を可能にする。

## リリース方針（Join-first / 4.4）
- 本リリースの正式保証は JOIN 系とする。対象は INNER/LEFT、複合 ON、非等価 ON、最大 3 段、JOIN 後の `ORDER BY/LIMIT/OFFSET`、JOIN + `GROUP BY/HAVING`。
- JOIN 以外の拡張 SQL は利用可能なものを維持しつつ、改善優先度は future とする。
- 対応外/制約外の構文は必ず `[mdb][E2] Unsupported SQL construct: <keyword>` で明示エラーにする。

## 1. 接続 (F1)
### 1.1. connect() に URI を渡した場合、MongoClient を初期化する (F1)
- 前提: 有効な MongoDB URI と DB 名を渡す
- 条件: `connect(uri, db_name)` を呼ぶ
- 振る舞い: `pymongo.MongoClient` を生成し、指定 DB を保持する接続オブジェクトを返す

### 1.2. URI が空文字の場合、エラーを送出する (F1, F5)
- 前提: URI が空または None
- 条件: `connect` を呼ぶ
- 振る舞い: Error ID `[mdb][E1] Invalid connection URI` を送出する

### 1.3. 接続失敗時はエラーを送出する (F1, F5)
- 前提: URI に到達できない、またはホスト/ポートが無効
- 条件: `connect` を呼ぶ
- 振る舞い: Error ID `[mdb][E7] Connection failed` を送出する

### 1.4. 認証失敗時はエラーを送出する (F1, F5)
- 前提: 認証情報が誤っている
- 条件: `connect` を呼ぶ
- 振る舞い: Error ID `[mdb][E8] Authentication failed` を送出する

## 2. SELECT 変換 (F2)
### 2.1. 単純な SELECT * が find に変換される (F2)
- 前提: `SELECT * FROM users`
- 条件: `cursor.execute(sql)` を呼ぶ
- 振る舞い: `db["users"].find({})` を実行し、取得結果を行タプル配列で返す

### 2.2. WHERE = 条件がフィルタに適用される (F2)
- 前提: `SELECT * FROM users WHERE id = 1`
- 条件: `cursor.execute(sql)` を呼ぶ
- 振る舞い: `find({"id": 1})` を実行する

### 2.3. LIMIT と ORDER BY が find オプションに適用される (F2)
- 前提: `SELECT * FROM users WHERE active = true ORDER BY created_at DESC LIMIT 10`
- 条件: `cursor.execute(sql)`
- 振る舞い: `find({"active": true}).sort("created_at", -1).limit(10)` を実行する

### 2.4. INNER/LEFT JOIN（等価結合）をサポートする (F2, F8)
- 前提: `SELECT u.id, o.name FROM users u JOIN orders o ON u.id = o.user_id`
- 条件: `cursor.execute(sql)`
- 振る舞い: 基底テーブルから `$lookup` で結合し、JOIN キーが一致する行を返す。LEFT JOIN の場合は右側が欠損しても返す。JOIN は最大 3 段まで、結合条件は等価のみ。JOIN 先の列は別名を含めて投影できるようにする（P5 強化項目）。

### 2.5. LIKE/BETWEEN/OR をサポートする (F2, F8)
- 前提: `LIKE '%foo%'`, `BETWEEN 1 AND 10`, `OR` 条件を含む SELECT
- 条件: `cursor.execute(sql)`
- 振る舞い: LIKE は `%`/`_` を正規表現に変換し `$regex` で実行、BETWEEN は比較に展開、OR は `$or` で評価する

### 2.6. 明示列の順序と SELECT * の順序を固定する (F2)
- 前提: `SELECT col2, col1 FROM users` または `SELECT * FROM users`
- 条件: `cursor.execute(sql)`
- 振る舞い: 明示列は SQL 記述順で返し、`SELECT *` はフィールド名をアルファベット順に並べて返す

### 2.7. GROUP BY と集計関数をサポートする (F2, F8)
- 前提: `SELECT status, COUNT(*) FROM orders GROUP BY status`
- 条件: `cursor.execute(sql)`
- 振る舞い: `$group` に変換し、集計結果を返す（COUNT/SUM/AVG/MIN/MAX をサポート）。HAVING は GROUP 結果に対する比較/AND/OR/IN/BETWEEN/LIKE を `$match` として適用し、非集計列のみの HAVING は `[mdb][E2]`。集計 alias（例: `HAVING SUM(total) >= 100`）を解決できるよう強化する（P5）。

### 2.8. WHERE IN/EXISTS のサブクエリを先行実行して適用する (F11)
- 前提: `SELECT id FROM users WHERE id IN (SELECT id FROM users WHERE score >= 10)` または `WHERE EXISTS (SELECT 1 FROM users WHERE active = true)`
- 条件: `cursor.execute(sql)`
- 振る舞い: サブクエリを先に実行し、先頭列の結果リストで `$in` を構築する。`EXISTS` はサブクエリ件数 > 0 なら真、0 件なら偽として評価する（非相関サブクエリのみサポート）。

### 2.9. FROM サブクエリを先行実行して結果に対して外側の SELECT を適用する (F11)
- 前提: `SELECT id, name FROM (SELECT id, name FROM users WHERE id >= 2) AS t WHERE id < 3`
- 条件: `cursor.execute(sql)`
- 振る舞い: FROM 句のサブクエリを先行実行し、得られた行をインラインビューとして保持した上で、外側の WHERE/ORDER/LIMIT/投影を適用する（非相関サブクエリのみサポート）。

### 2.10. JOIN を含む SELECT で ORDER BY を指定した場合、テーブル修飾/alias を解決して並べ替える (F2, F8, F17)
- 前提: `SELECT u.id, o.total FROM users u JOIN orders o ON u.id = o.user_id ORDER BY o.total DESC`
- 条件: `cursor.execute(sql)`
- 振る舞い: `$lookup` の結果（右テーブル列）を含めた列解決を行い、JOIN 先列は `__joinN.<field>` を `$sort` 対象として扱う。

### 2.11. JOIN を含む SELECT で LIMIT/OFFSET を指定した場合、OFFSET→LIMIT の順に適用する (F2, F8, F17)
- 前提: `SELECT u.id FROM users u LEFT JOIN orders o ON u.id = o.user_id ORDER BY u.id LIMIT 10 OFFSET 5`
- 条件: `cursor.execute(sql)`
- 振る舞い: SQL セマンティクスに合わせ、`$sort` の後に `$skip`（OFFSET）→ `$limit`（LIMIT）の順で適用する。

### 2.12. UNION ALL に ORDER BY/LIMIT/OFFSET を指定した場合、連結後の結果に適用する (F2, F17)
- 前提: `SELECT id FROM users UNION ALL SELECT id FROM archived_users ORDER BY id LIMIT 10 OFFSET 0`
- 条件: `cursor.execute(sql)`
- 振る舞い: 2 つの SELECT 結果を連結した後の結果集合に対して `ORDER BY/LIMIT/OFFSET` を適用する（部分集合ごとの並べ替えではない）。

### 2.13. SELECT DISTINCT を複数列で指定した場合、複合キーで重複排除する (F2, F17)
- 前提: `SELECT DISTINCT tenant_id, status FROM orders ORDER BY tenant_id, status LIMIT 20 OFFSET 0`
- 条件: `cursor.execute(sql)`
- 振る舞い: 指定列の組み合わせをキーに重複排除し、`ORDER BY/LIMIT/OFFSET` を適用して返す。

### 2.14. COUNT(DISTINCT <column>) を指定した場合、ユニーク件数を返す (F2, F8, F17)
- 前提: `SELECT COUNT(DISTINCT user_id) FROM orders` または `SELECT tenant_id, COUNT(DISTINCT user_id) FROM orders GROUP BY tenant_id`
- 条件: `cursor.execute(sql)`
- 振る舞い: 対象列のユニーク値数を返す。GROUP BY がある場合はグループ単位でユニーク件数を返す。

### 2.15. JOIN を含む GROUP BY で集計した場合、JOIN 後の結果に対して集約/HAVING/並び替えを適用する (F2, F8, F17)
- 前提: `SELECT u.id, COUNT(o.id) AS order_cnt FROM users u LEFT JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id WHERE u.tenant_id = 1 GROUP BY u.id HAVING order_cnt >= 1 ORDER BY order_cnt DESC, u.id ASC LIMIT 20 OFFSET 0`
- 条件: `cursor.execute(sql)`
- 振る舞い: JOIN 後の行集合に対して `GROUP BY` 集約を実施し、`HAVING` で集計 alias を評価した上で `ORDER BY/LIMIT/OFFSET` を適用する。`COUNT(o.id)` は NULL を件数に含めない。

### 2.16. 相関 EXISTS/NOT EXISTS を指定した場合、外側行に依存して真偽判定する (F2, F11, F17)
- 前提: `SELECT u.id FROM users u WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.tenant_id = u.tenant_id) ORDER BY u.id LIMIT 20`
- 条件: `cursor.execute(sql)`
- 振る舞い: 外側行の列をサブクエリ評価に渡して `EXISTS/NOT EXISTS` を判定する。対象は単一サブクエリ・単一テーブルの相関条件に限定する。

### 2.17. UNION（重複除去）に ORDER BY/LIMIT/OFFSET を指定した場合、重複除去後の結果に適用する (F2, F17)
- 前提: `SELECT tenant_id FROM users UNION SELECT tenant_id FROM archived_users ORDER BY tenant_id LIMIT 20 OFFSET 0`
- 条件: `cursor.execute(sql)`
- 振る舞い: UNION 結果で重複行を除去した後の結果集合に対して `ORDER BY/LIMIT/OFFSET` を適用する。

### 2.18. JOIN の ON 句に比較条件を含む場合、$lookup pipeline で評価する (F2, F8, F17)
- 前提: `SELECT u.id, o.id FROM users u JOIN orders o ON u.id = o.user_id AND o.total >= u.min_total WHERE u.tenant_id = 1`
- 条件: `cursor.execute(sql)`
- 振る舞い: `ON` 句の比較条件（`>=`, `<=`, `>`, `<`, `<>`）を `$expr` で評価する。結合段数は最大 3 段とする。

### 2.19. WITH（非再帰 CTE）を指定した場合、CTE をサブクエリとしてインライン展開する (F2, F11, F17)
- 前提: `WITH active_users AS (SELECT id, tenant_id FROM users WHERE tenant_id = 1) SELECT id FROM active_users WHERE id >= 10 ORDER BY id`
- 条件: `cursor.execute(sql)`
- 振る舞い: CTE 定義をインラインサブクエリとして展開し、外側 SELECT に対して `WHERE/ORDER BY/LIMIT/OFFSET` を適用する。`WITH RECURSIVE` は `[mdb][E2]` とする。

### 2.20. 比較式に非相関スカラサブクエリを指定した場合、先頭行先頭列を単一値として比較する (F2, F11, F17)
- 前提: `SELECT id FROM users WHERE score >= (SELECT AVG(score) FROM users WHERE tenant_id = 1) ORDER BY id`
- 条件: `cursor.execute(sql)`
- 振る舞い: サブクエリを先行実行し、先頭行先頭列を単一値として比較演算（`=`, `<>`, `>`, `>=`, `<`, `<=`）に適用する。結果 0 件時は `NULL` 相当として比較する。

### 2.21. UNION/UNION ALL を 3 項以上で連結した場合、左から順に連結し末尾で ORDER/LIMIT/OFFSET を適用する (F2, F17)
- 前提: `SELECT id FROM users UNION ALL SELECT id FROM archived_users UNION ALL SELECT id FROM users ORDER BY id LIMIT 20`
- 条件: `cursor.execute(sql)`
- 振る舞い: 3 項以上の UNION 連結を左結合でフラット化して実行し、最終結果に `ORDER BY/LIMIT/OFFSET` を適用する。`UNION` と `UNION ALL` の混在連結は `[mdb][E2]` とする。

### 2.22. RIGHT OUTER JOIN（単一 JOIN）を指定した場合、LEFT JOIN に正規化して同等セマンティクスで実行する (F2, F8, F17)
- 前提: `SELECT u.id, o.id FROM users u RIGHT OUTER JOIN orders o ON u.id = o.user_id` または `... ON u.id = o.user_id AND u.tenant_id = o.tenant_id`
- 条件: `cursor.execute(sql)`
- 振る舞い: 単一の RIGHT JOIN は内部で LEFT JOIN へ正規化して実行し、右側欠損保持を含む結果を返す。ON は単一等価または複合等価（`AND`）を許可する。非等価 ON は `[mdb][E2] Unsupported SQL construct: RIGHT_JOIN_NON_EQ`、複数 JOIN を含む RIGHT JOIN は `[mdb][E2] Unsupported SQL construct: RIGHT_JOIN_CHAIN` とする。

### 2.23. FULL OUTER JOIN（単一 JOIN・等価 ON（単一/複合））を指定した場合、LEFT JOIN と右片側差分を UNION ALL して実行する (F2, F8, F17)
- 前提: `SELECT u.id AS uid, o.id AS oid FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id ORDER BY oid` または `... ON u.id = o.user_id AND u.tenant_id = o.tenant_id`
- 条件: `cursor.execute(sql)`
- 振る舞い: 単一 JOIN の等価 ON（単一/複合）の FULL JOIN は `LEFT JOIN` 結果と、反対向き `LEFT JOIN` の「右片側差分（左キー群 IS NULL）」を `UNION ALL` して実行する。`ORDER BY/LIMIT/OFFSET` は統合後の全体結果に適用する。非等価 ON は `[mdb][E2] Unsupported SQL construct: FULL_JOIN_NON_EQ`、複数 JOIN を含む FULL JOIN は `[mdb][E2] Unsupported SQL construct: FULL_JOIN_CHAIN` とする。

### 2.24. FULL OUTER JOIN 後に COALESCE キーで GROUP BY/HAVING を指定した場合、DB 側で UNION 統合後に集計する (F2, F8, F17)
- 前提: `SELECT COALESCE(u.tenant_id, o.tenant_id) AS tenant_id, COUNT(*) AS cnt FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id GROUP BY COALESCE(u.tenant_id, o.tenant_id) HAVING cnt >= 1 ORDER BY tenant_id LIMIT 20`
- 条件: `cursor.execute(sql)`
- 振る舞い: `LEFT JOIN` と右片側差分を `$unionWith` で統合後、`$group` で `COALESCE` キーごとに `COUNT(*)` を集計し、`HAVING/ORDER BY/LIMIT/OFFSET` を適用する。対象は `COALESCE(left_col, right_col) + COUNT(*)` の単一グループキー・単一件数集計パターンに限定する。

## 3. DML 変換 (F3)
### 3.1. INSERT が insert_one に変換される (F3)
- 前提: `INSERT INTO users (id, name) VALUES (1, 'Alice')`
- 条件: `cursor.execute(sql)`
- 振る舞い: `db["users"].insert_one({"id": 1, "name": "Alice"})` を実行し、挿入件数を 1 として返す

### 3.2. UPDATE が update_many に変換される (F3)
- 前提: `UPDATE users SET name = 'Bob' WHERE id = 1`
- 条件: `cursor.execute(sql)`
- 振る舞い: `update_many({"id": 1}, {"$set": {"name": "Bob"}})` を実行し、影響件数を返す

### 3.3. DELETE が delete_many に変換される (F3)
- 前提: `DELETE FROM users WHERE id = 1`
- 条件: `cursor.execute(sql)`
- 振る舞い: `delete_many({"id": 1})` を実行し、削除件数を返す

### 3.4. WHERE なしの UPDATE/DELETE はガードする (F3, F5)
- 前提: `UPDATE users SET name = 'Bob'` または `DELETE FROM users`
- 条件: `cursor.execute(sql)`
- 振る舞い: Error ID `[mdb][E3] Unsafe operation without WHERE` を送出する

## 4. パラメータバインド (F4)
### 4.1. プレースホルダーに位置引数を適用する (F4)
- 前提: `SELECT * FROM users WHERE id = %s` とパラメータ `(1,)`
- 条件: `cursor.execute(sql, params)`
- 振る舞い: フィルタ `{"id": 1}` を生成し `find` を実行する

### 4.2. プレースホルダー数とパラメータ数が不一致の場合、エラーを送出する (F4, F5)
- 前提: `SELECT * FROM users WHERE id = %s` とパラメータが空
- 条件: `cursor.execute(sql, params)`
- 振る舞い: Error ID `[mdb][E4] Parameter count mismatch` を送出する

### 4.3. 名前付きプレースホルダーに dict パラメータを適用する (F4)
- 前提: `SELECT * FROM users WHERE id = %(id)s` とパラメータ `{"id": 1}`
- 条件: `cursor.execute(sql, params)`
- 振る舞い: フィルタ `{"id": 1}` を生成し `find` を実行する（不足/余剰キーは `[mdb][E4]`）

## 5. 例外/エラーメッセージ (F5)
- Error ID とメッセージは下表のとおり。実装は文字列を完全一致で返す。

| Error ID | 条件 | メッセージ |
| --- | --- | --- |
| [mdb][E1] | URI が空/None | `Invalid connection URI` |
| [mdb][E2] | JOIN など未対応構文 | `Unsupported SQL construct: <keyword>` |
| [mdb][E3] | WHERE なしの UPDATE/DELETE | `Unsafe operation without WHERE` |
| [mdb][E4] | プレースホルダー数とパラメータ数が不一致 | `Parameter count mismatch` |
| [mdb][E5] | SQL 解析に失敗 | `Failed to parse SQL` |
| [mdb][E6] | トランザクションが未対応のサーバー | `Transactions not supported on this server` |
| [mdb][E7] | 接続に失敗 | `Connection failed` |
| [mdb][E8] | 認証に失敗 | `Authentication failed` |

## 6. トランザクション/セッション (F6)
### 6.1. begin/commit/rollback が Mongo セッションをラップする (F6)
- 前提: 接続がレプリカセット/トランザクション対応クラスタ（例: MongoDB 4.x 以降）に接続済み
- 条件: `connection.begin()`, `connection.commit()`, `connection.rollback()` を呼ぶ
- 振る舞い: MongoDB セッションを開始/コミット/アボートし、失敗時は [mdb][E5] で包んで送出する

### 6.2. サーバーがトランザクション非対応の場合は no-op で成功扱いにする (F6)
- 前提: 接続先がスタンドアロン構成またはトランザクション未対応バージョン（例: MongoDB 3.6）
- 条件: `connection.begin()` を呼ぶ
- 振る舞い: エラーは送出せず、内部的にはセッションを張らずに no-op とし、後続の `commit`/`rollback` も成功扱いで返す

## 7. メタデータ取得 (F7)
### 7.1. コレクション一覧を返す (F7)
- 前提: 有効な接続
- 条件: `connection.list_tables()` を呼ぶ
- 振る舞い: DB のコレクション名一覧をリストで返す

## 8. DDL 変換 (F8 相当)
### 8.1. CREATE TABLE がコレクション作成に変換される
- 前提: `CREATE TABLE users (...)`
- 条件: `cursor.execute(sql)`
- 振る舞い: `db.create_collection("users")` を呼び、存在していてもエラーにしない

### 8.2. DROP TABLE がコレクション削除に変換される
- 前提: `DROP TABLE users`
- 条件: `cursor.execute(sql)`
- 振る舞い: `db.drop_collection("users")` を呼ぶ（存在しなくてもエラーにしない）

### 8.3. CREATE INDEX がインデックス作成に変換される
- 前提: `CREATE INDEX idx_users_name ON users(name)`
- 条件: `cursor.execute(sql)`
- 振る舞い: `db["users"].create_index("name", name="idx_users_name")` を実行する（ASC/DESC 指定、複合インデックス、UNIQUE オプションを受け付ける。既存の場合はエラーにせずスキップ）

### 8.4. DROP INDEX がインデックス削除に変換される
- 前提: `DROP INDEX idx_users_name ON users`
- 条件: `cursor.execute(sql)`
- 振る舞い: `db["users"].drop_index("idx_users_name")` を実行する（存在しなくてもエラーにしない）

## 8. DBAPI 互換の補足
- `rowcount`: 書き込み系は影響件数、SELECT は取得件数を保持する。
- `lastrowid`: INSERT の `inserted_id` を返す（ObjectId は文字列化して返却）。
- `description`: SELECT 時に列名を列挙し、型は簡易に Python 型名または `str` を返す。
- `autocommit`: MongoDB は非トランザクションが基本のため、デフォルト autocommit 相当とし、`begin()` 時のみセッションを張る。
- カーソル再利用: 同一カーソルで複数回 `execute` を呼ぶと最新の結果に上書きする。

## 9. 型変換方針
- `ObjectId` は文字列化して返す。
- `datetime` は `datetime` のまま返す。
- 数値/ブール/文字列は MongoDB の値をそのまま対応付けて返す。
- サポート外の型は文字列化し、元型のまま返すかどうかは将来拡張とする。

## 10. SQLAlchemy 方言（F10）
- DBAPI モジュール属性（`apilevel`、`threadsafety`、`paramstyle=pyformat`）を提供し、dialect から利用できるようにする。接続スキームは `mongodb+dbapi://`。
- SQLAlchemy からの CRUD/SELECT/WHERE/ORDER/LIMIT/OFFSET/JOIN/DDL 呼び出しを受け付ける。
- トランザクションは MongoDB 4.0+ でのみ有効化し、3.6 では no-op で成功扱いとする。
- ウィンドウ関数（ROW_NUMBER）のみ MongoDB 5.x+ で `$setWindowFields` に変換する。MongoDB 5.0 未満では `[mdb][E2] Unsupported SQL construct: WINDOW_FUNCTION` を返す。
- async 方言: Core CRUD/DDL/Index を async API（`create_async_engine`）経由で実行できるようにし、当面は sync 実装をスレッドプールでラップする（将来ネイティブ async も検討）。トランザクションポリシーは sync と同じ（4.x のみ有効、3.6 は no-op）、ORM/relationship や statement cache は対象外。README に保証レベルと制限事項を明記する。

## 11. 拡張機能（F11/F12）
- P1: SQLAlchemy Core 強化（Table/Column CRUD/DDL/Index を実通信で通す）  
  - サブクエリ: `WHERE IN (SELECT ...)`、`EXISTS (SELECT ...)`、比較式のスカラサブクエリを先行実行して置換する。相関は `EXISTS/NOT EXISTS` の単一サブクエリ・単一テーブルに限定して対応し、複雑相関は [mdb][E2]。`FROM (SELECT ...) AS t` は拡張対象。  
  - CTE: 非再帰 CTE（`WITH`）をサブクエリとしてインライン展開して処理する。`WITH RECURSIVE` は [mdb][E2]。  
  - UNION/UNION ALL: `UNION ALL` と `UNION`（重複除去）をサポートし、3 項以上の多段連結も扱う。`ORDER BY/LIMIT/OFFSET` は連結/重複除去後の全体結果に適用。  
  - HAVING: GROUP BY 後の比較/AND/OR/IN/BETWEEN/LIKE を `$match` として適用（非集計列を含む HAVING は [mdb][E2]）。  
  - JOIN 拡張: 等価 JOIN と `ON` 比較条件（`>=`, `<=`, `>`, `<`, `<>`）を含む JOIN を最大 3 段まで対応。RIGHT OUTER は単一 JOIN、FULL OUTER は単一 JOIN かつ等価 ON（単一/複合）を対応し、それ以外は [mdb][E2]。  
  - 文字列マッチ拡張: `ILIKE` を大小区別なし `$regex`、`/pattern/` の正規表現リテラルも `$regex` で対応。  
  - 名前付きパラメータ: `%(name)s` を dict で受け、不足/余剰は [mdb][E4]。  
  - 型拡張: Decimal/UUID は文字列化、tz 付き datetime はそのまま返却、Binary は base64 文字列化。未対応型は文字列化。  
- P2: ORM 最小 CRUD（単一テーブル相当の add/get/select/update/delete。PK を `_id` にマッピングし、リレーション/JOIN は当面対象外）  
- P3: async dialect（Core CRUD/DDL/Index を async でラップ。トランザクション方針は同期と同じ。実装はスレッドプールラップをベースとし、ネイティブ async は将来検討）  
- P4: Mongo 5+ 拡張（低優先度。`ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)` を `$setWindowFields` で対応。Mongo 5.0 未満は [mdb][E2]。その他のウィンドウ関数は非対応。7.x で動作確認済み）
- P5: JOIN 投影/alias 強化、CASE 集計（`SUM(CASE WHEN ... THEN ... END)`）、HAVING で集計 alias を解決。JOIN + WHERE/HAVING の alias 解決を強化する。
- P6: ウィンドウ関数拡張（ROW_NUMBER 以外の基本ウィンドウ関数を検討。MongoDB 5+ 前提）

## 付録A: MongoDB 5+ のみの拡張（参考）
### A.1. ROW_NUMBER/RANK/DENSE_RANK ウィンドウ関数をサポートする (F16)
- 前提: `SELECT id, ROW_NUMBER() OVER (PARTITION BY category ORDER BY created_at) AS rn FROM items`
- 条件: `cursor.execute(sql)`
- 振る舞い: MongoDB 5.x 以降では `$setWindowFields` を用いてウィンドウ関数を計算する。MongoDB 5 未満では `[mdb][E2] Unsupported SQL construct: WINDOW_FUNCTION` を返す。
