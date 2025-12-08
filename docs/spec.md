# dbapi-mongodb 仕様

## 前提: サポートする SQL 構文（拡張後）
- 対応: `SELECT/INSERT/UPDATE/DELETE`、`CREATE/DROP TABLE`（コレクション作成/削除）、`CREATE/DROP INDEX`、`WHERE` の比較 (`=`/`<>`/`>`/`<`/`<=`/`>=`)、`AND`、`OR`、`IN`、`BETWEEN`、`LIKE`（`%`/`_` を `$regex` に変換）、`ILIKE`、正規表現リテラル、`ORDER BY`、`LIMIT`、`OFFSET`、INNER/LEFT JOIN の等価結合（複合キー、最大 3 段）、`GROUP BY` + 集約（COUNT/SUM/AVG/MIN/MAX）+ `HAVING`（集計 alias を解決）、`UNION ALL`、サブクエリ（`WHERE IN/EXISTS`、`FROM (SELECT ...)`）、`ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)`（MongoDB 5.x+）、簡易 CASE 集計（`SUM(CASE WHEN ... THEN ... ELSE ... END)` の単一 WHEN）
- 強化予定（P5/P6）: JOIN 投影/alias 解決の強化、CASE を含む集計 (`SUM(CASE WHEN ... THEN ... ELSE ... END)`)、HAVING での集計 alias 解決、ROW_NUMBER 以外の基本ウィンドウ関数（MongoDB 5+ 前提）の検討。
- 非対応: 非等価 JOIN、フル/右外部 JOIN、`ROW_NUMBER` 以外のウィンドウ関数、MongoDB 5 未満でのウィンドウ関数、`UNION`（重複除去）。
- プレースホルダー: `%s` と `%(name)s` の両方に対応（不足/余剰は `[mdb][E4]`）。
- `SELECT *` 時のフィールド順: コレクションのフィールド名をアルファベット順で返す。明示列指定時は SQL 記述順で返す。JOIN 時の `SELECT *` は左テーブル→右テーブルの順でアルファベット順。
- パーサー: SQLGlot を使用し、将来のサブクエリ対応を可能にする。

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

### 2.10. ROW_NUMBER ウィンドウ関数をサポートする (F12)
- 前提: `SELECT id, ROW_NUMBER() OVER (PARTITION BY category ORDER BY created_at) AS rn FROM items`
- 条件: `cursor.execute(sql)`
- 振る舞い: MongoDB 5.x 以降では `$setWindowFields` を用いて ROW_NUMBER を計算する。PARTITION BY が省略された場合は全件を一括で計算する。MongoDB 5 未満では `[mdb][E2] Unsupported SQL construct: WINDOW_FUNCTION` を返す。

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
  - サブクエリ: `WHERE IN (SELECT ...)`、`EXISTS (SELECT ...)` を先行実行し、結果リスト/存在可否で置換する（非相関のみ）。`FROM (SELECT ...) AS t` は拡張対象（スカラサブクエリは非対応）。  
  - UNION/UNION ALL: `UNION ALL` のみサポート（重複除去は [mdb][E2]）。ORDER/LIMIT は全体にのみ適用。  
  - HAVING: GROUP BY 後の比較/AND/OR/IN/BETWEEN/LIKE を `$match` として適用（非集計列を含む HAVING は [mdb][E2]）。  
  - JOIN 拡張: 等価 JOIN の多段（最大 3 段）。非等価 JOIN、RIGHT/FULL OUTER は当面 [mdb][E2]。  
  - 文字列マッチ拡張: `ILIKE` を大小区別なし `$regex`、`/pattern/` の正規表現リテラルも `$regex` で対応。  
  - 名前付きパラメータ: `%(name)s` を dict で受け、不足/余剰は [mdb][E4]。  
  - 型拡張: Decimal/UUID は文字列化、tz 付き datetime はそのまま返却、Binary は base64 文字列化。未対応型は文字列化。  
- P2: ORM 最小 CRUD（単一テーブル相当の add/get/select/update/delete。PK を `_id` にマッピングし、リレーション/JOIN は当面対象外）  
- P3: async dialect（Core CRUD/DDL/Index を async でラップ。トランザクション方針は同期と同じ。実装はスレッドプールラップをベースとし、ネイティブ async は将来検討）  
- P4: Mongo 5+ 拡張（低優先度。`ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)` を `$setWindowFields` で対応。Mongo 5.0 未満は [mdb][E2]。その他のウィンドウ関数は非対応。7.x で動作確認済み）
- P5: JOIN 投影/alias 強化、CASE 集計（`SUM(CASE WHEN ... THEN ... END)`）、HAVING で集計 alias を解決。JOIN + WHERE/HAVING の alias 解決を強化する。
- P6: ウィンドウ関数拡張（ROW_NUMBER 以外の基本ウィンドウ関数を検討。MongoDB 5+ 前提）
