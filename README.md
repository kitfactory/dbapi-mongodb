# mongo-dbapi

DBAPI-style adapter that lets you execute a limited subset of SQL against MongoDB by translating SQL to Mongo queries. Built on `pymongo` (3.13.x for MongoDB 3.6 compatibility) and `SQLGlot`.

## Features
- DBAPI-like `Connection`/`Cursor`
- SQL → Mongo: `SELECT/INSERT/UPDATE/DELETE`, `CREATE/DROP TABLE/INDEX` (ASC/DESC, UNIQUE, composite), `WHERE` (comparisons/`AND`/`OR`/`IN`/`BETWEEN`/`LIKE`→`$regex`), `ORDER BY`, `LIMIT/OFFSET`, INNER/LEFT JOIN (equijoin, composite keys up to 2 hops), `GROUP BY` + aggregates (COUNT/SUM/AVG/MIN/MAX)
- `%s` positional parameters only; unsupported constructs raise Error IDs (e.g. `[mdb][E2]`)
- Error IDs for common failures: invalid URI, unsupported SQL, unsafe DML without WHERE, parse errors, connection/auth failures
- DBAPI fields: `rowcount`, `lastrowid`, `description` (column order: explicit order, or alpha for `SELECT *`; JOIN uses left→right)
- Transactions: `begin/commit/rollback` wrap Mongo sessions; MongoDB 3.6 and other unsupported envs are treated as no-op success

- Use cases
  - Swap in Mongo as “another dialect” for existing SQLAlchemy Core–based infra (Engine/Connection + Table/Column)
  - Point existing Core-based batch/report jobs at Mongo data with minimal changes
  - (Future) Minimal ORM CRUD for single-table entities; relationships out of scope initially
  - (Future) Async dialect for FastAPI/async stacks; currently roadmap only

## Requirements
- Python 3.10+
- MongoDB 3.6 (bundled `mongodb-3.6` binary) or later (note: bundled binary is 3.6, so transactions are unsupported)
- Virtualenv at `.venv` (already present); dependencies are managed via `pyproject.toml`

## Installation
```bash
pip install mongodb-dbapi
# (optional) with a virtualenv: python -m venv .venv && . .venv/bin/activate && pip install mongodb-dbapi
```

## Start local MongoDB (bundled 3.6)
```bash
# Default port 27017; override with PORT
PORT=27018 ./startdb.sh
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
- WHERE: comparisons (`=`, `<>`, `>`, `<`, `<=`, `>=`), `AND`, `OR`, `IN`, `BETWEEN`, `LIKE` (`%`/`_` → `$regex`)
- JOIN: INNER/LEFT equijoin (composite keys, up to 2 joins)
- Aggregation: `GROUP BY` with COUNT/SUM/AVG/MIN/MAX
- ORDER/LIMIT/OFFSET
- Unsupported: subqueries (future), non-equi joins, FULL/RIGHT OUTER, HAVING, window functions, UNION, regex literals, named params

## SQLAlchemy
- DBAPI module attributes: `apilevel="2.0"`, `threadsafety=1`, `paramstyle="pyformat"`.
- Intended scheme: `mongodb+dbapi://...` (dialect implementation planned).
- Scope: Core text() を中心に動作確認済み。Core の Table/Column 対応強化と ORM CRUD は今後拡張予定。async dialect はロードマップ上で検討中。

## Running tests
```bash
PORT=27018 ./startdb.sh  # if 27017 is taken
MONGODB_URI=mongodb://127.0.0.1:27018 MONGODB_DB=mongo_dbapi_test .venv/bin/pytest -q
```

## Notes
- Transactions on MongoDB 3.6 are treated as no-op; 4.x+ uses real sessions.
- Error messages are fixed strings per `docs/spec.md`. Keep logs at DEBUG only (default INFO is silent).
