# mongo-dbapi (日本語)

MongoDB に対して限定的な SQL を DBAPI 風に実行するアダプターです。SQL を Mongo クエリに変換し、`pymongo`（MongoDB 3.6 互換のため 3.13.x 系）と `SQLGlot` を利用します。

## 特長
- `connect()` で DBAPI 風の `Connection`/`Cursor` を取得
- SQL→Mongo 変換: `SELECT/INSERT/UPDATE/DELETE`、`CREATE/DROP TABLE/INDEX`（インデックスは ASC/DESC、UNIQUE、複合に対応）、`WHERE`（比較/`AND`/`OR`/`IN`/`BETWEEN`/`LIKE`→`$regex`）、`ORDER BY`、`LIMIT/OFFSET`、INNER/LEFT JOIN（等価結合、複合キー/2 段まで）、`GROUP BY` + 集計（COUNT/SUM/AVG/MIN/MAX）
- プレースホルダーは `%s` のみ対応。未対応構文は Error ID（例: `[mdb][E2]`）を返す
- 代表的なエラー ID: URI 無効、未対応 SQL、WHERE なしの危険 DML、解析失敗、接続/認証失敗、トランザクション非対応など
- DBAPI 項目: `rowcount`、`lastrowid`、`description`（列順は明示順、`SELECT *` はアルファベット順。JOIN 時は左→右）
- トランザクション: `begin/commit/rollback` をセッションでラップ。MongoDB 3.6 など未対応環境では no-op の成功扱い
- ユースケース例:
  - SQLAlchemy Core ベースの社内基盤に「Mongo 方言」を差し込む（Engine/Connection + Table/Column）
  - Core ベースのバッチ/レポートを最小変更で Mongo データに向ける
  - （将来）単一テーブル相当の ORM CRUD を最小サポートする実験
  - （将来）async dialect で FastAPI/async アプリから同じ API で扱う（ロードマップ上）

## 要件
- Python 3.10+
- MongoDB 3.6（同梱バイナリ `mongodb-3.6`）以降を想定。ただし同梱は 3.6 のためトランザクション不可
- `.venv` 環境が存在（依存は `pyproject.toml` で管理）

## インストール
```bash
pip install mongodb-dbapi
# （任意）仮想環境を使う場合: python -m venv .venv && . .venv/bin/activate && pip install mongodb-dbapi
```

## 同梱 MongoDB 3.6 の起動
```bash
# 既定ポート 27017。使用中なら PORT で上書き
PORT=27018 ./startdb.sh
```

## 利用例
```python
from mongo_dbapi import connect

conn = connect("mongodb://127.0.0.1:27018", "mongo_dbapi_test")
cur = conn.cursor()
cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "Alice"))

cur.execute("SELECT id, name FROM users WHERE id = %s", (1,))
print(cur.fetchall())  # [(1, 'Alice')]
print(cur.rowcount)    # 1
```

## 対応している SQL
- ステートメント: `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `CREATE/DROP TABLE`, `CREATE/DROP INDEX`
- WHERE: 比較演算子（`=`, `<>`, `>`, `<`, `<=`, `>=`）、`AND`、`OR`、`IN`、`BETWEEN`、`LIKE`（`%`/`_` を `$regex` に変換）
- JOIN: INNER/LEFT 等価結合（複合キー、最大 2 段）
- 集計: `GROUP BY` + 集計（COUNT/SUM/AVG/MIN/MAX）
- `ORDER BY`, `LIMIT`, `OFFSET`
- 未対応: サブクエリ（将来対応予定）、非等価 JOIN、FULL/RIGHT OUTER、HAVING、ウィンドウ関数、UNION、正規表現リテラル、名前付きパラメータ

## テスト実行
```bash
PORT=27018 ./startdb.sh  # 27017 使用中の場合
MONGODB_URI=mongodb://127.0.0.1:27018 MONGODB_DB=mongo_dbapi_test .venv/bin/pytest -q
```

## SQLAlchemy
- DBAPI モジュール属性: `apilevel="2.0"`, `threadsafety=1`, `paramstyle="pyformat"`（方言実装を前提）
- 接続スキーム: `mongodb+dbapi://...` を想定（dialect 実装予定）
- スコープ: Core の text() ベースで動作確認済み。Core の Table/Column 互換強化と ORM CRUD は今後拡張予定。async dialect はロードマップで検討中。

## 補足
- MongoDB 3.6 などトランザクション未対応環境では `begin/commit/rollback` を no-op の成功扱いとします。
- エラーメッセージは `docs/spec.md` に定義された固定文字列です。ログは DEBUG 時のみ出力し、INFO では出しません。
