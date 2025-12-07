# mongo-dbapi (日本語)

MongoDB に対して限定的な SQL を DBAPI 風に実行するアダプターです。SQL を Mongo クエリに変換し、`pymongo`（MongoDB 3.6 互換のため 3.13.x 系）と `SQLGlot` を利用します。

既存の DB-API / SQLAlchemy Core / FastAPI ベースのコードから、MongoDB を「もうひとつの方言」として扱うことを目的としています。

- PyPI パッケージ名: `mongo-dbapi`
- モジュール名: `mongo_dbapi`

## 特長
- `connect()` で DBAPI 風の `Connection`/`Cursor` を取得
- SQL→Mongo 変換: `SELECT/INSERT/UPDATE/DELETE`、`CREATE/DROP TABLE/INDEX`（インデックスは ASC/DESC、UNIQUE、複合に対応）、`WHERE`（比較/`AND`/`OR`/`IN`/`BETWEEN`/`LIKE`→`$regex`、`ILIKE`、正規表現リテラル）、`ORDER BY`、`LIMIT/OFFSET`、INNER/LEFT JOIN（等価結合、複合キー/最大 3 段）、`GROUP BY` + 集計（COUNT/SUM/AVG/MIN/MAX）+ `HAVING`、`UNION ALL`、サブクエリ（`WHERE IN/EXISTS`、`FROM (SELECT ...)`）、`ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)`（MongoDB 5.x+）
- プレースホルダーは `%s` と `%(name)s` をサポート。未対応構文は Error ID（例: `[mdb][E2]`）を返す
- 代表的なエラー ID: URI 無効、未対応 SQL、WHERE なしの危険 DML、解析失敗、接続/認証失敗、トランザクション非対応など
- DBAPI 項目: `rowcount`、`lastrowid`、`description`（列順は明示順、`SELECT *` はアルファベット順。JOIN 時は左→右）
- トランザクション: `begin/commit/rollback` をセッションでラップ。MongoDB 3.6 など未対応環境では no-op の成功扱い
- async dialect（スレッドプールラップ）で Core CRUD/DDL/Index を FastAPI などから await 利用可能。ORM は単一テーブル CRUD を最小サポート（リレーション非対応）
- ユースケース例:
  - SQLAlchemy Core ベースの社内基盤に「Mongo 方言」を差し込む（Engine/Connection + Table/Column）
  - Core ベースのバッチ/レポートを最小変更で Mongo データに向ける
  - 単一テーブル相当の ORM CRUD を最小サポートする実験
  - async dialect で FastAPI/async アプリから同じ API で扱う（内部はスレッドプール、ネイティブ async は将来検討）

## 要件
- Python 3.10+
- MongoDB 3.6（同梱バイナリ `mongodb-3.6`）以降を想定。ただし同梱は 3.6 のためトランザクション不可
- `.venv` 環境が存在（依存は `pyproject.toml` で管理）

## インストール
```bash
pip install mongo-dbapi
# （任意）仮想環境を使う場合: python -m venv .venv && . .venv/bin/activate && pip install mongo-dbapi
```

## 同梱 MongoDB 3.6 の起動
```bash
# 既定ポート 27017。使用中なら PORT で上書き
PORT=27018 ./startdb.sh
```

## MongoDB 4.4 (レプリカセット) の起動
```bash
# 既定ポート 27019。バンドル済み libssl1.1 を使い、スクリプト内で LD_LIBRARY_PATH を設定して起動します。
PORT=27019 ./start4xdb.sh
# 4.x でテストを走らせる場合
MONGODB_URI=mongodb://127.0.0.1:27019 MONGODB_DB=mongo_dbapi_test .venv/bin/pytest -q
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
- WHERE: 比較演算子（`=`, `<>`, `>`, `<`, `<=`, `>=`）、`AND`、`OR`、`IN`、`BETWEEN`、`LIKE`（`%`/`_` → `$regex`）、`ILIKE`、正規表現リテラル `/.../`
- JOIN: INNER/LEFT 等価結合（複合キー、最大 3 段）
- 集計: `GROUP BY` + 集計（COUNT/SUM/AVG/MIN/MAX）+ `HAVING`
- サブクエリ: `WHERE IN/EXISTS` と `FROM (SELECT ...)`（非相関のみ、先行実行）
- 集合: `UNION ALL`
- ウィンドウ: `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)`（MongoDB 5.x+、それ未満は `[mdb][E2]`）
- `ORDER BY`, `LIMIT`, `OFFSET`
- 未対応: 非等価 JOIN、FULL/RIGHT OUTER、`UNION`（重複除去）、`ROW_NUMBER` 以外のウィンドウ関数、相関サブクエリ、ORM リレーション

## テスト実行
```bash
PORT=27018 ./startdb.sh  # 27017 使用中の場合
MONGODB_URI=mongodb://127.0.0.1:27018 MONGODB_DB=mongo_dbapi_test .venv/bin/pytest -q
```

## SQLAlchemy
- DBAPI モジュール属性: `apilevel="2.0"`, `threadsafety=1`, `paramstyle="pyformat"`（方言実装を前提）
- 接続スキーム: `mongodb+dbapi://...` の dialect を提供（sync/async スレッドプール）
- スコープ: Core text()/Table/Column の CRUD/DDL/Index、ORM 最小 CRUD（単一テーブル）、JOIN/UNION ALL/HAVING/サブクエリ/ROW_NUMBER を実通信で確認済み。async dialect は Core CRUD/DDL/Index をラップ（ネイティブ async は将来検討）。

## Async (FastAPI/Core) - ベータ
- `mongo_dbapi.async_dbapi.connect_async` で非同期ラッパーを提供。**現時点では sync をスレッドプールでラップする実装（ネイティブ async は将来検討）**。同期と同じ Core 機能（CRUD/DDL/Index、JOIN/UNION ALL/HAVING/IN/EXISTS/FROM サブクエリ）を await で実行可能。
- トランザクション: MongoDB 4.x 以降で有効。3.6 は no-op。RDB とロック/性能が異なるため重いトランザクション用途は非推奨。
- ウィンドウ: `ROW_NUMBER` は MongoDB 5.x+ のみ対応。それ未満は `[mdb][E2] Unsupported SQL construct: WINDOW_FUNCTION`。
- FastAPI 例:
```python
from fastapi import FastAPI, Depends
from sqlalchemy.ext.asyncio import create_async_engine, AsyncConnection
from sqlalchemy import text

engine = create_async_engine("mongodb+dbapi://127.0.0.1:27019/mongo_dbapi_test")
app = FastAPI()

async def get_conn() -> AsyncConnection:
    async with engine.connect() as conn:
        yield conn

@app.get("/users/{user_id}")
async def get_user(user_id: str, conn: AsyncConnection = Depends(get_conn)):
    rows = await conn.execute(text("SELECT id, name FROM users WHERE id = :id"), {"id": user_id})
    row = rows.fetchone()
    return dict(row) if row else {}
```
- 制限: async ORM/relationship、statement cache は対象外。内部はスレッドプールのため高負荷時はスレッド/接続数に注意。

## 保証範囲と制約
- 実機で安定確認済み: 単一コレクション CRUD、WHERE/ORDER/LIMIT/OFFSET、INNER/LEFT 等価 JOIN（最大 3 段）、GROUP BY + 集計 + HAVING、サブクエリ（WHERE IN/EXISTS, FROM (SELECT ...)), UNION ALL、`ROW_NUMBER()`（MongoDB 5.x+）
- 非対応/制約: 非等価 JOIN、FULL/RIGHT OUTER、重複除去 `UNION`、`ROW_NUMBER` 以外のウィンドウ関数、相関サブクエリ、ORM リレーション。async はスレッドプール実装。

## 補足
- MongoDB 3.6 などトランザクション未対応環境では `begin/commit/rollback` を no-op の成功扱いとします。4.x 以降（レプリカセット）ではセッションが有効で、同梱 4.4 で全テスト通過済みです。
- エラーメッセージは `docs/spec.md` に定義された固定文字列です。ログは DEBUG 時のみ出力し、INFO では出しません。

## License
MIT License（`LICENSE` を参照）。無保証ですが商用利用を含め自由に利用できます。

## GitHub Sponsors
本プロジェクトは個人の空き時間でメンテナンスしています。DB-API / SQLAlchemy 経由で MongoDB を扱う用途で役立っている場合、GitHub Sponsors などで支援いただけると、バグ修正やバージョン追随のモチベーションになります。
