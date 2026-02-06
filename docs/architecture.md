# dbapi-mongodb アーキテクチャ

## レイヤー構造と依存方向
- DBAPI ファサード層: `connect()`・接続/カーソルオブジェクトを公開。外部から唯一の入口。→ 翻訳層・Mongo クライアント層に依存。
- SQL 翻訳層: SQL の構文解析と AST 簡易化を行い、Mongo クエリ（フィルタ/投影/オプション）へ変換。→ エラー整形層に依存。
- Mongo クライアント層: `pymongo` への薄いアダプター。CRUD・セッション制御・メタデータ取得・DDL（コレクション作成/削除/インデックス）・JOIN/集計用 `$lookup`/`$group` を担当。→ エラー整形層に依存。
- 結果整形層: `pymongo` の返却値を DBAPI 互換の行タプル/カウントに整形。JOIN 時は `$lookup` の結果をフラット化して返す。
- エラー整形層: 例外を Error ID 付きメッセージにマッピングし、仕様で定義した文字列を返す。

依存方向は左から右へのみ（DBAPI → 翻訳 → クライアント → 結果/エラー）。ユーティリティ/定数は下位でのみ共有し、循環を禁止する。SQL パーサーは `SQLGlot` を使用し、`SELECT/INSERT/UPDATE/DELETE`、`CREATE/DROP TABLE/INDEX`、`WHERE`（比較/AND/OR/IN/BETWEEN/LIKE/スカラサブクエリ比較）、`ORDER BY`、`LIMIT/OFFSET`、JOIN（INNER/LEFT は最大 3 段、RIGHT/FULL OUTER は単一 JOIN に限定、投影/alias 対応）、`GROUP BY`+集計+`HAVING`（集計 alias 解決、JOIN を含む集計、簡易 CASE 集計）、`UNION ALL/UNION`（3 項以上の多段連結を含む）、サブクエリ（`WHERE IN/EXISTS`/FROM サブクエリを先行実行）、`WITH`（非再帰 CTE のインライン展開）に対応する。LIKE は `%`/`_` を `$regex` に変換し、大文字小文字は区別（ILIKE/正規表現リテラルは拡張項目）。

### ターゲット方針（MongoDB 4.4）
- 当面の主対象は MongoDB 4.4 とし、**生SQL互換（SQL セマンティクスの正しさ）を最優先**する。
- MongoDB 5+ 専用機能（`$setWindowFields` によるウィンドウ関数など）は「別フェーズの拡張」として扱い、4.4 では `[mdb][E2]` で明示的に拒否する。

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
  - `to_mongo_query(parsed: ParsedQuery, params) -> MongoQueryParts`（未対応構文は [mdb][E2]、JOIN は INNER/LEFT（最大 3 段）と RIGHT/FULL（単一 JOIN 制約）を変換、GROUP BY は `$group`、LIKE/ILIKE は `$regex`、サブクエリは先行実行で結果リスト適用、UNION/UNION ALL は必要に応じて `$unionWith` で連結）
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
- SQLAlchemy 方言を提供し、モジュール属性（apilevel/threadsafety/paramstyle=pyformat、スキーム `mongodb+dbapi://`）を設定する。Core の text()/Table/Column、DDL/Index、ORM 最小 CRUD（単一テーブル）を実通信で通す。async dialect も提供し、当面は sync 実装をスレッドプールでラップする。
- 拡張機能: サブクエリ（WHERE IN/EXISTS/NOT EXISTS（非相関）、FROM サブクエリ先行実行）/非再帰 CTE（`WITH` のインライン展開）/UNION ALL・UNION（連結後 ORDER/LIMIT/OFFSET）/HAVING/多段 JOIN（最大 3 段）/ILIKE・正規表現リテラル/名前付きパラメータを翻訳する。`SELECT DISTINCT`（複数列）や `COUNT DISTINCT` を含む集計を扱う。Decimal/UUID/tz datetime/Binary などの型変換を明示する。ウィンドウ関数は MongoDB 5+ のみ（4.4 では `[mdb][E2]`）。
- 優先実装順（4.4 方針）: 1) 生SQL互換（JOIN/UNION を含む `ORDER BY`、`LIMIT/OFFSET`、NULL/NOT/DISTINCT 等）、2) 高速化（`$match/$project` の押し込み、`$lookup` pipeline 化、`UNION ALL` の `$unionWith` 化）、3) API 拡張（SQLAlchemy/async 等）、4) MongoDB 5+ 専用拡張（ウィンドウ関数など）。

## async 方言の設計方針（概要）
- API: SQLAlchemy 2.0 の async Engine/Connection (`create_async_engine`) から CRUD/DDL/Index を実行できるようにする。翻訳経路は sync と共通。
- 実装方式: 当面は sync 実装をスレッドプールでラップし、非同期アプリから await 可能にする。高負荷時のスレッド数/接続数は利用者側で制御する前提。ネイティブ async（motor など）は将来検討。
- トランザクション: ポリシーは sync と同じ。MongoDB 4.x 以降でのみ begin/commit/rollback を有効化し、3.6 では no-op。README に期待値を明示する。
- 非対応: ORM/relationship、複雑なメタデータ API、statement cache。ウィンドウ関数は Mongo 5+ 前提で `$setWindowFields` に変換、5 未満は [mdb][E2]。

## 設定と環境
- 環境変数（例）: `MONGODB_URI`（接続先 URI）、`MONGODB_DB`（デフォルト DB 名）。`.env.sample` は作成せず、必要なら `.env` を手元で用意する。
- 変換/最適化トグル（環境変数）:
  - `MONGO_DBAPI_OPTIMIZE=0/1`: 互換性を保った範囲で最適化を有効化（既定: 1）
  - `MONGO_DBAPI_LOOKUP_PIPELINE=0/1`: JOIN 条件を `$lookup` pipeline に押し込む実験フラグ（既定: 0、回帰が出たため既定 OFF）
  - `MONGO_DBAPI_JOIN_PROJECT=0/1`: JOIN の `$project` を強制（既定: 0。通常は `LIMIT` が大きい場合のみ自動有効化）
- トランザクションを利用する場合、レプリカセット/トランザクション対応クラスタ（MongoDB 4.x 以降）であることを前提とし、非対応環境では no-op で成功扱いとする（安全ガードとしてはログのみ）。4.x 系で接続した場合は begin/commit/rollback が実際に動作することを担保する。

## データフロー
1. `connect` で MongoClient と DB を初期化し、Connection を返す。  
2. `cursor.execute` が SQL 文字列と params を受け取り、翻訳層で AST 解析・Mongo クエリ部品に変換。  
3. クライアント層が `pymongo` へ CRUD/セッション API を呼び出し、例外はエラー整形層でマッピング。  
4. 結果整形層が `find` 結果をタプル行へ、書き込み結果を件数へ正規化し、フェッチ系メソッドが返す。

## 実装メモ（MongoDB 4.4 互換）
- `EXISTS/NOT EXISTS` は非相関に加えて、単一サブクエリ・単一テーブルの相関条件を `$lookup + $expr` で評価する（複雑な相関は `[mdb][E2]`）。
- JOIN の複合等価条件（`ON a=b AND c=d`）は `$lookup pipeline` で `$expr` を組み立てて結合する（単一等価は `localField/foreignField` を優先）。
- `UNION`（重複除去）は `UNION ALL` の連結結果を重複排除してから `ORDER BY/LIMIT/OFFSET` を適用する。
- `WITH`（非再帰 CTE）は SQL AST 上でテーブル参照をサブクエリへ置換してから既存の SELECT/UNION 変換経路を使う。`WITH RECURSIVE` は `[mdb][E2]` とする。
- 比較式の右辺が非相関サブクエリの場合は先行実行して先頭行先頭列を単一値として置換する（結果 0 件は `None`）。
- `UNION/UNION ALL` は AST を左からフラット化して `union_parts` に展開し、`ORDER BY/LIMIT/OFFSET` は最終結果に適用する。混在連結（`UNION` + `UNION ALL`）は `[mdb][E2]` とする。
- 単一の RIGHT OUTER JOIN は AST で LEFT JOIN へ正規化して既存 JOIN パイプラインを再利用する。複数 JOIN を含む RIGHT JOIN 連鎖は `[mdb][E2]` とする。
- 単一の FULL OUTER JOIN（等価 ON の単一/複合）は `LEFT JOIN` と「反対向き LEFT JOIN の右片側差分」を `UNION ALL` して実行する。複数 JOIN や非等価 ON の FULL JOIN は `[mdb][E2]` とする。
- FULL OUTER JOIN + 集計（A27 パターン）は `COALESCE(left_col, right_col)` をキーに、`$unionWith` で統合した後に `$group/$match(HAVING)/$sort/$limit` を DB 側で適用する。
- `UNION ALL` 実行は `find` だけでなく `aggregate` の連結にも `$unionWith` 最適化を適用し、FULL OUTER JOIN の `ORDER BY/LIMIT/OFFSET` を可能な限りサーバー側で処理する。
- `JOIN + GROUP BY` は `$group` → `$project` → `$match(HAVING)` → `$sort/$skip/$limit` の順で適用し、`COUNT(<column>)` は NULL を件数から除外する。`COUNT(<join-column>)` の単純パターンでは `unwind` を回避し、`$lookup` 配列を `$addFields` で件数化して集計する。
