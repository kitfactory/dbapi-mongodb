# mongo-dbapi

DBAPI-style adapter that lets you execute a limited subset of SQL against MongoDB by translating SQL to Mongo queries. Built on `pymongo` (3.13.x for MongoDB 3.6 compatibility) and `SQLGlot`.

Purpose: let existing DB-API / SQLAlchemy Core / FastAPI code treat MongoDB as “just another dialect.”

- PyPI package name: `mongo-dbapi`
- Module import name: `mongo_dbapi`

## Features
- DBAPI-like `Connection`/`Cursor`
- SQL → Mongo: `SELECT/INSERT/UPDATE/DELETE`, `CREATE/DROP TABLE/INDEX` (ASC/DESC, UNIQUE, composite), `WHERE` (comparisons/`AND`/`OR`/`IN`/`BETWEEN`/`LIKE`→`$regex`/`ILIKE`/regex literal), `ORDER BY`, `LIMIT/OFFSET`, INNER/LEFT JOIN (equijoin, composite keys up to 3 hops), `GROUP BY` + aggregates (COUNT/SUM/AVG/MIN/MAX) + `HAVING`, `UNION ALL`, subqueries (`WHERE IN/EXISTS`, `FROM (SELECT ...)`), `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)` on MongoDB 5.x+
- `%s` positional and `%(name)s` named parameters; unsupported constructs raise Error IDs (e.g. `[mdb][E2]`)
- Error IDs for common failures: invalid URI, unsupported SQL, unsafe DML without WHERE, parse errors, connection/auth failures
- DBAPI fields: `rowcount`, `lastrowid`, `description` (column order: explicit order, or alpha for `SELECT *`; JOIN uses left→right)
- Transactions: `begin/commit/rollback` wrap Mongo sessions; MongoDB 3.6 and other unsupported envs are treated as no-op success
- Async dialect (thread-pool backed) for Core CRUD/DDL/Index with FastAPI-friendly usage; minimal ORM CRUD for single-table entities (relationships out of scope)

- Use cases
  - Swap in Mongo as “another dialect” for existing SQLAlchemy Core–based infra (Engine/Connection + Table/Column)
  - Point existing Core-based batch/report jobs at Mongo data with minimal changes
  - Minimal ORM CRUD for single-table entities (PK → `_id`)
  - Async dialect for FastAPI/async stacks (thread-pool implementation; native async later)

## Requirements
- Python 3.10+
- MongoDB 3.6 (bundled `mongodb-3.6` binary) or later (note: bundled binary is 3.6, so transactions are unsupported)
- Virtualenv at `.venv` (already present); dependencies are managed via `pyproject.toml`

## Installation
```bash
pip install mongo-dbapi
# (optional) with a virtualenv: python -m venv .venv && . .venv/bin/activate && pip install mongo-dbapi
```

## Start local MongoDB (bundled 3.6)
```bash
# Default port 27017; override with PORT
PORT=27018 ./startdb.sh
```

## Start local MongoDB 4.4 (replica set, bundled)
```bash
# Default port 27019; uses bundled libssl1.1. LD_LIBRARY_PATH is set inside the script for mongod.
PORT=27019 ./start4xdb.sh
# Run tests against 4.x
MONGODB_URI=mongodb://127.0.0.1:27019 MONGODB_DB=mongo_dbapi_test .venv/bin/pytest -q
```

## Usage example
```python
from mongo_dbapi import connect

conn = connect("mongodb://127.0.0.1:27018", "mongo_dbapi_test")
cur = conn.cursor()
cur.execute("INSERT INTO users (id, name) VALUES (%s, %s)", (1, "Alice"))

cur.execute("SELECT id, name FROM users WHERE id = %s", (1,))
print(cur.fetchall())  # [(1, 'Alice')]
print(cur.rowcount)    # 1
```

## Supported SQL
- Statements: `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `CREATE/DROP TABLE`, `CREATE/DROP INDEX`
- WHERE: comparisons (`=`, `<>`, `>`, `<`, `<=`, `>=`), `AND`, `OR`, `IN`, `BETWEEN`, `LIKE` (`%`/`_` → `$regex`), `ILIKE`, regex literal `/.../`
- JOIN: INNER/LEFT equijoin (composite keys, up to 3 joins)
- Aggregation: `GROUP BY` with COUNT/SUM/AVG/MIN/MAX and `HAVING`
- Subqueries: `WHERE IN/EXISTS` and `FROM (SELECT ...)` (non-correlated; executed first)
- Set ops: `UNION ALL`
- Window: `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)` on MongoDB 5.x+ (`[mdb][E2]` on earlier versions)
- ORDER/LIMIT/OFFSET
- Unsupported: non-equi joins, FULL/RIGHT OUTER, `UNION` (distinct), window functions other than `ROW_NUMBER`, correlated subqueries, ORM relationships

## SQLAlchemy
- DBAPI module attributes: `apilevel="2.0"`, `threadsafety=1`, `paramstyle="pyformat"`.
- Scheme: `mongodb+dbapi://...` dialect provided (sync + async/thread-pool).
- Scope: Core text()/Table/Column CRUD/DDL/Index、ORM 最小 CRUD（単一テーブル）、JOIN/UNION ALL/HAVING/subquery/ROW_NUMBER を実通信で確認済み。async dialect は Core CRUD/DDL/Index のラップで、ネイティブ async は今後検討。

## Async (FastAPI/Core) - beta
- **Current implementation wraps the sync driver in a thread pool** (native async driver is planned). Provided via `mongo_dbapi.async_dbapi.connect_async`. API mirrors sync Core: awaitable CRUD/DDL/Index, JOIN/UNION ALL/HAVING/IN/EXISTS/FROM subquery.
- Transactions: effective on MongoDB 4.x+ only; 3.6 is no-op. Be mindful that MongoDB transactions differ from RDBMS in locking/perf; avoid heavy transactional workloads.
- Window: `ROW_NUMBER` is available on MongoDB 5.x+; earlier versions return `[mdb][E2] Unsupported SQL construct: WINDOW_FUNCTION`.
- FastAPI example:
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
- Limitations: async ORM/relationship and statement cache are out of scope; heavy concurrency uses a thread pool under the hood. 

## Support levels
- Tested/stable (real Mongo runs): single-collection CRUD, WHERE/ORDER/LIMIT/OFFSET, INNER/LEFT equijoin (up to 3 hops), GROUP BY + aggregates + HAVING, subqueries (WHERE IN/EXISTS, FROM (SELECT ...)), UNION ALL, `ROW_NUMBER()` (MongoDB 5.x+).
- Not supported / constraints: non-equi JOIN, FULL/RIGHT OUTER, distinct `UNION`, window functions other than `ROW_NUMBER`, correlated subqueries, ORM relationships; async is thread-pool based.

## Running tests
```bash
PORT=27018 ./startdb.sh  # if 27017 is taken
MONGODB_URI=mongodb://127.0.0.1:27018 MONGODB_DB=mongo_dbapi_test .venv/bin/pytest -q
```

## Notes
- Transactions on MongoDB 3.6 are treated as no-op; 4.x+ (replica set) uses real sessions and the bundled 4.4 binary passes all tests.
- Error messages are fixed strings per `docs/spec.md`. Keep logs at DEBUG only (default INFO is silent).

## License
MIT License (see `LICENSE`). Provided as-is without warranty; commercial use permitted.

## GitHub Sponsors
Maintained in personal time. If this helps you run MongoDB from DB-API/SQLAlchemy stacks, consider supporting via GitHub Sponsors to keep fixes and version updates coming.
