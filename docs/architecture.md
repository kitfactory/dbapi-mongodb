# mongo-dbapi アーキテクチャ

## レイヤー構造と依存方向
- DBAPI ファサード層: `connect()`・接続/カーソルオブジェクトを公開。外部から唯一の入口。→ 翻訳層・Mongo クライアント層に依存。
- SQL 翻訳層: SQL の構文解析と AST 簡易化を行い、Mongo クエリ（フィルタ/投影/オプション）へ変換。→ エラー整形層に依存。
- Mongo クライアント層: `pymongo` への薄いアダプター。CRUD・セッション制御・メタデータ取得・DDL（コレクション作成/削除/インデックス）・JOIN/集計用 `$lookup`/`$group` を担当。→ エラー整形層に依存。
- 結果整形層: `pymongo` の返却値を DBAPI 互換の行タプル/カウントに整形。JOIN 時は `$lookup` の結果をフラット化して返す。
- エラー整形層: 例外を Error ID 付きメッセージにマッピングし、仕様で定義した文字列を返す。

依存方向は左から右へのみ（DBAPI → 翻訳 → クライアント → 結果/エラー）。ユーティリティ/定数は下位でのみ共有し、循環を禁止する。SQL パーサーは `SQLGlot` を使用し、`SELECT/INSERT/UPDATE/DELETE`、`CREATE/DROP TABLE/INDEX`、`WHERE`（比較/AND/OR/IN/BETWEEN/LIKE）、`ORDER BY`、`LIMIT/OFFSET`、INNER/LEFT 等価 JOIN（2 段まで）、`GROUP BY`+集計、`UNION ALL`、`HAVING` に対応し、サブクエリ対応を見据える。LIKE は `%`/`_` を `$regex` に変換し、大文字小文字は区別（ILIKE/正規表現リテラルは拡張項目）。

## 主要インターフェース（案）
- `connect(uri: str, db_name: str, **kwargs) -> Connection`
- `class Connection:`  
  - `cursor() -> Cursor`  
  - `begin() -> None` / `commit() -> None` / `rollback() -> None`（セッションを内部に保持。開始時にサーバーのトランザクション対応可否をチェックし、非対応なら no-op）  
  - `close() -> None`  
  - `list_tables() -> list[str]`
- `class Cursor:`  
  - `execute(sql: str, params: Sequence | Mapping | None = None) -> Self`  
  - `fetchone() -> tuple | None`  
  - `fetchall() -> list[tuple]`  
  - `close() -> None`
- 翻訳層ユーティリティ  
  - `parse_sql(sql: str) -> ParsedQuery`（失敗時は [mdb][E5]）  
  - `to_mongo_query(parsed: ParsedQuery, params) -> MongoQueryParts`（未対応構文は [mdb][E2]、JOIN は等価結合、GROUP BY は `$group`、LIKE/ILIKE は `$regex`、サブクエリは先行実行で結果リスト適用、UNION ALL は SELECT 結果を concat）
- クライアント層ユーティリティ  
  - `execute_find(parts: MongoQueryParts) -> list[dict]`  
  - `execute_write(parts: MongoQueryParts) -> int`

## エラー/ログ方針
- エラーは `MongoDbApiError`（独自例外）を基点に、Error ID をメッセージ先頭に付与（例: `[mdb][E2] Unsupported SQL construct: JOIN`）。
- トランザクション開始時に `hello`/`isMaster`/`server_info` などで対応状況を判定し、未対応環境（例: MongoDB 3.6）は no-op で成功扱いとし、`commit`/`rollback` もエラーにしない。MongoDB 4.x 以降のレプリカセットではセッションを張って実際に commit/rollback を行う（4.x 系ではトランザクションをサポートすることを必須要件とする）。
- 接続失敗/認証失敗は [mdb][E7]/[mdb][E8] で返し、元例外を cause に保持する。
- ログは DEBUG レベルのみで出力し、実行クエリ概要と変換後クエリ詳細を記録する。デフォルト INFO ではログを出さない。PII はログに含めない。
- 例外チェーンは保持し、呼び出し側が元例外を辿れるよう `__cause__` を設定。

## DBAPI 互換ポリシー
- `rowcount` は SELECT/書き込み結果件数を反映し、`lastrowid` は `inserted_id`（ObjectId は文字列化）を返す。
- `description` は列名と簡易型を返却する。`SELECT *` の列順はアルファベット順、明示列は記述順。JOIN 時の `SELECT *` は左→右テーブルのアルファベット順。
- プレースホルダーは `%s` と `%(name)s` に対応（不足/余剰は [mdb][E4]）。
- `autocommit` はデフォルト ON 相当で、`begin()` 呼び出し時のみセッションを張る（未対応環境では no-op）。
- SQLAlchemy 方言を提供し、モジュール属性（apilevel/threadsafety/paramstyle=pyformat、スキーム `mongodb+dbapi://`）を設定する。Core の text() ベースで確認済み、Table/Column 互換と ORM CRUD は今後拡張、async dialect はロードマップ上で検討。
- 拡張機能: サブクエリ/UNION/HAVING/非等価・多段 JOIN/ILIKE・正規表現リテラル/名前付きパラメータ/ウィンドウ関数の翻訳パスを追加し、Decimal/UUID/tz datetime など型変換のポリシーを明確化する。
- 優先実装順: 1) SQLAlchemy Core 強化（Table/Column CRUD/DDL/Index）、2) ORM 最小 CRUD、3) async dialect（Core CRUD 基準）、4) ウィンドウ関数（Mongo 5+ 前提）。

## 設定と環境
- 環境変数（例）: `MONGODB_URI`（接続先 URI）、`MONGODB_DB`（デフォルト DB 名）。`.env.sample` は作成せず、必要なら `.env` を手元で用意する。
- トランザクションを利用する場合、レプリカセット/トランザクション対応クラスタ（MongoDB 4.x 以降）であることを前提とし、非対応環境では no-op で成功扱いとする（安全ガードとしてはログのみ）。4.x 系で接続した場合は begin/commit/rollback が実際に動作することを担保する。

## データフロー
1. `connect` で MongoClient と DB を初期化し、Connection を返す。  
2. `cursor.execute` が SQL 文字列と params を受け取り、翻訳層で AST 解析・Mongo クエリ部品に変換。  
3. クライアント層が `pymongo` へ CRUD/セッション API を呼び出し、例外はエラー整形層でマッピング。  
4. 結果整形層が `find` 結果をタプル行へ、書き込み結果を件数へ正規化し、フェッチ系メソッドが返す。
