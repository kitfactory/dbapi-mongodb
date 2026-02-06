# dbapi-mongodb (日本語)

MongoDB に対して限定的な SQL を DBAPI 風に実行するアダプターです。SQL を Mongo クエリに変換し、`pymongo`（MongoDB 3.6 互換のため 3.13.x 系）と `SQLGlot` を利用します。

既存の DB-API / SQLAlchemy Core / FastAPI ベースのコードから、MongoDB を「もうひとつの方言」として扱うことを目的としています。

- PyPI パッケージ名: `dbapi-mongodb`
- モジュール名: `mongo_dbapi`

## リリース方針（Join-first / MongoDB 4.4）
- 本リリースは **JOIN の実運用互換**を最優先に固定し、MongoDB 4.4 をターゲットにします。
- JOIN の正式サポート範囲: INNER/LEFT（最大 3 段、複合 ON、非等価 ON）、RIGHT OUTER（単一 JOIN・等価 ON）、FULL OUTER（単一 JOIN・等価 ON）、JOIN 後の `ORDER BY`/`LIMIT`/`OFFSET`、JOIN + `GROUP BY/HAVING`（A27 形: `COALESCE(...) + COUNT(*)`）。
- JOIN 以外の高度機能は「利用可能なものは維持しつつ、改善は future 優先」で進めます（未対応時は `[mdb][E2]` を返却）。

## 特長
- `connect()` で DBAPI 風の `Connection`/`Cursor` を取得
- SQL→Mongo 変換: `SELECT/INSERT/UPDATE/DELETE`、`CREATE/DROP TABLE/INDEX`（ASC/DESC、UNIQUE、複合）、`WHERE`（比較/`AND`/`OR`/`IN`/`BETWEEN`/`LIKE`/`ILIKE`/正規表現）、`ORDER BY`、`LIMIT/OFFSET`、JOIN（INNER/LEFT は最大 3 段の複合/非等価 ON、RIGHT/FULL OUTER は単一 JOIN の等価 ON）、`GROUP BY` + 集計 + `HAVING`（FULL OUTER の A27 形を含む）、`UNION ALL/UNION`（多段連結）、サブクエリ（`WHERE IN/EXISTS/NOT EXISTS`、比較式スカラサブクエリ、`FROM (SELECT ...)`）、`WITH`（非再帰 CTE）
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
pip install dbapi-mongodb
# （任意）仮想環境を使う場合: python -m venv .venv && . .venv/bin/activate && pip install dbapi-mongodb
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
- JOIN: INNER/LEFT（複合 ON、非等価 ON、最大 3 段、投影/alias 対応）、RIGHT OUTER（単一 JOIN・等価 ON）、FULL OUTER（単一 JOIN・等価 ON）
- 集計: `GROUP BY` + 集計（COUNT/SUM/AVG/MIN/MAX）+ `HAVING`（集計 alias 解決）＋簡易 CASE 集計（`SUM(CASE WHEN ... THEN ... END)`）
- サブクエリ: `WHERE IN/EXISTS/NOT EXISTS`（相関 EXISTS は単純形のみ）、比較式の非相関スカラサブクエリ、`FROM (SELECT ...)`
- 集合: `UNION ALL` / `UNION`（3 項以上の多段連結を含む。混在連結は `[mdb][E2]`）
- CTE: `WITH`（非再帰）
- ウィンドウ: `ROW_NUMBER`/`RANK`/`DENSE_RANK`（MongoDB 5.x+、4.4 では `[mdb][E2]`）
- `ORDER BY`, `LIMIT`, `OFFSET`
- 未対応（4.4）: `WITH RECURSIVE`、非等価 ON を含む RIGHT/FULL OUTER JOIN、RIGHT/FULL JOIN 連鎖、FULL OUTER 集計の複雑形（A27 形以外）、複雑な相関サブクエリ、`UNION` と `UNION ALL` の混在連結、ORM リレーション

## テスト実行
```bash
PORT=27018 ./startdb.sh  # 27017 使用中の場合
MONGODB_URI=mongodb://127.0.0.1:27018 MONGODB_DB=mongo_dbapi_test .venv/bin/pytest -q
```

## SQLAlchemy
- DBAPI モジュール属性: `apilevel="2.0"`, `threadsafety=1`, `paramstyle="pyformat"`（方言実装を前提）
- 接続スキーム: `mongodb+dbapi://...` の dialect を提供（sync/async スレッドプール）
- スコープ: Core text()/Table/Column の CRUD/DDL/Index、ORM 最小 CRUD（単一テーブル）、JOIN/UNION ALL/HAVING/サブクエリ/ROW_NUMBER/RANK/DENSE_RANK を実通信で確認済み。async dialect は Core CRUD/DDL/Index をラップ（ネイティブ async は将来検討）。

## Async (FastAPI/Core) - ベータ
- `mongo_dbapi.async_dbapi.connect_async` で非同期ラッパーを提供。**現時点では sync をスレッドプールでラップする実装（ネイティブ async は将来検討）**。同期と同じ Core 機能（CRUD/DDL/Index、JOIN/UNION ALL/HAVING/IN/EXISTS/FROM サブクエリ）を await で実行可能。
- トランザクション: MongoDB 4.x 以降で有効。3.6 は no-op。RDB とロック/性能が異なるため重いトランザクション用途は非推奨。
- ウィンドウ: `ROW_NUMBER`/`RANK`/`DENSE_RANK` は MongoDB 5.x+ のみ対応。それ未満は `[mdb][E2] Unsupported SQL construct: WINDOW_FUNCTION`。
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
- 安定保証（Join-first）: JOIN 系（INNER/LEFT 最大 3 段、RIGHT/FULL の単一 JOIN 制約、JOIN 後 `ORDER BY/LIMIT/OFFSET`、対応形での JOIN + 集計/HAVING）と単一コレクション CRUD。
- 併用可能（best effort）: `UNION/UNION ALL`、CTE（非再帰）、スカラサブクエリ、相関 EXISTS（単純形）、MongoDB 5.x+ のウィンドウ関数。
- 非対応/制約: `WITH RECURSIVE`、非等価 ON の RIGHT/FULL OUTER、RIGHT/FULL JOIN 連鎖、FULL OUTER 集計の複雑形、複雑相関サブクエリ、`UNION` と `UNION ALL` の混在連結、ORM リレーション。async はスレッドプール実装。

## 補足
- MongoDB 3.6 などトランザクション未対応環境では `begin/commit/rollback` を no-op の成功扱いとします。4.x 以降（レプリカセット）ではセッションが有効で、同梱 4.4 で全テスト通過済みです。
- エラーメッセージは `docs/spec.md` に定義された固定文字列です。ログは DEBUG 時のみ出力し、INFO では出しません。

## 今後の対応優先度（SQL）
1) 複雑相関サブクエリと FULL OUTER 集計の一般化（A27 形の拡張）  
2) `WITH RECURSIVE`、`UNION`/`UNION ALL` 混在連結  
3) ROW_NUMBER 系以外のウィンドウ関数（`LAG/LEAD/NTILE` など）  
4) 大規模 JOIN 向けの性能ガイド（推奨インデックス、スロークエリ判定基準。特に FULL OUTER）  
必要な項目があれば Issue などでユースケースを共有してください。
## チュートリアル
- English: `docs/tutorial.md`
- 日本語: `docs/tutorial_ja.md`

## License
MIT License（`LICENSE` を参照）。無保証ですが商用利用を含め自由に利用できます。

## GitHub Sponsors
本プロジェクトは個人の空き時間でメンテナンスしています。DB-API / SQLAlchemy 経由で MongoDB を扱う用途で役立っている場合、GitHub Sponsors などで支援いただけると、バグ修正やバージョン追随のモチベーションになります。
