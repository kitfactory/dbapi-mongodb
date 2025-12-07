from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Iterable, List, Mapping, Sequence
import base64
import decimal
import uuid
import datetime

from bson import ObjectId
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, OperationFailure

from .errors import raise_error
from .translation import QueryParts, parse_sql

logger = logging.getLogger("mongo_dbapi")


def _convert_value(value: Any) -> Any:
    """Convert Mongo value to Python-friendly value / Mongo の値を Python 向けに変換"""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict) and "$binary" in value:
        return value  # leave as-is for now
    return value


@dataclass
class CursorState:
    rows: List[tuple] | None = None
    rowcount: int = -1
    lastrowid: Any | None = None
    description: List[tuple] | None = None


class Cursor:
    """DBAPI-like cursor / DBAPI 風カーソル"""

    def __init__(self, connection: "Connection"):
        self.connection = connection
        self._state = CursorState()
        self._closed = False

    def execute(self, sql: str, params: Sequence | Mapping | None = None) -> "Cursor":
        if self._closed:
            raise_error("[mdb][E5]", "Failed to parse SQL")
        parts = parse_sql(sql, params)
        self._state = self.connection._execute_parts(parts)  # noqa: SLF001
        return self

    def executemany(self, sql: str, seq_of_params: Sequence[Sequence | Mapping]) -> "Cursor":
        if self._closed:
            raise_error("[mdb][E5]", "Failed to parse SQL")
        total_rows = 0
        lastrowid = None
        for params in seq_of_params:
            parts = parse_sql(sql, params)
            state = self.connection._execute_parts(parts)  # noqa: SLF001
            total_rows += state.rowcount
            lastrowid = state.lastrowid
        self._state = CursorState(rows=[], rowcount=total_rows, lastrowid=lastrowid, description=None)
        return self

    def fetchone(self) -> tuple | None:
        if not self._state.rows:
            return None
        return self._state.rows.pop(0)

    def fetchall(self) -> List[tuple]:
        rows = self._state.rows or []
        self._state.rows = []
        return rows

    @property
    def rowcount(self) -> int:
        return self._state.rowcount

    @property
    def lastrowid(self) -> Any:
        return self._state.lastrowid

    @property
    def description(self) -> List[tuple] | None:
        return self._state.description

    def close(self) -> None:
        self._closed = True


class Connection:
    """DBAPI-like connection / DBAPI 風接続"""

    def __init__(self, uri: str, db_name: str):
        if not uri:
            raise_error("[mdb][E1]")
        self._uri = uri
        self._db_name = db_name
        try:
            self._client = MongoClient(uri)
            self._db = self._client[db_name]
        except ConnectionFailure as exc:
            raise_error("[mdb][E7]", cause=exc)
        except OperationFailure as exc:
            raise_error("[mdb][E8]", cause=exc)
        self._session = None
        self._transactions_supported = self._detect_transactions()
        self._window_supported = self._detect_window()

    def _detect_transactions(self) -> bool:
        try:
            info = self._client.server_info()
            version_str = info.get("version", "0.0")
            major = int(version_str.split(".")[0])
            return major >= 4
        except Exception:
            return False

    def _detect_window(self) -> bool:
        try:
            info = self._client.server_info()
            version_str = info.get("version", "0.0")
            major = int(version_str.split(".")[0])
            minor = int(version_str.split(".")[1])
            return major > 5 or (major == 5 and minor >= 0)
        except Exception:
            return False

    def cursor(self) -> Cursor:
        return Cursor(self)

    def begin(self) -> None:
        if not self._transactions_supported:
            logger.debug("Transaction not supported; no-op / トランザクション非対応のため no-op")
            return
        logger.debug("Starting transaction session / トランザクションセッション開始")
        self._session = self._client.start_session()
        self._session.start_transaction()

    def commit(self) -> None:
        if self._session:
            logger.debug("Commit transaction / トランザクションコミット")
            self._session.commit_transaction()
            self._session.end_session()
            self._session = None

    def rollback(self) -> None:
        if self._session:
            logger.debug("Abort transaction / トランザクションアボート")
            self._session.abort_transaction()
            self._session.end_session()
            self._session = None

    def close(self) -> None:
        self._client.close()

    def list_tables(self) -> list[str]:
        return self._db.list_collection_names()

    def _execute_parts(self, parts: QueryParts) -> CursorState:
        if parts.subqueries:
            parts = self._materialize_subqueries(parts)
        if parts.uses_window and not self._window_supported:
            raise_error("[mdb][E2]", "Unsupported SQL construct: WINDOW_FUNCTION")
        if parts.operation == "from_subquery":
            return self._execute_from_subquery(parts)
        if parts.operation == "find":
            return self._execute_find(parts)
        if parts.operation == "insert":
            return self._execute_insert(parts)
        if parts.operation == "update":
            return self._execute_update(parts)
        if parts.operation == "delete":
            return self._execute_delete(parts)
        if parts.operation == "aggregate":
            return self._execute_aggregate(parts)
        if parts.operation == "create":
            return self._execute_create(parts)
        if parts.operation == "drop":
            return self._execute_drop(parts)
        if parts.operation == "create_index":
            return self._execute_create_index(parts)
        if parts.operation == "drop_index":
            return self._execute_drop_index(parts)
        if parts.operation == "union_all":
            return self._execute_union_all(parts)
        raise_error("[mdb][E2]")

    def _materialize_subqueries(self, parts: QueryParts) -> QueryParts:
        """Execute subqueries and substitute placeholders / サブクエリを実行し置換"""
        resolved: dict[str, Any] = {}
        for token, spec in (parts.subqueries or {}).items():
            sub_parts: QueryParts = spec["parts"]
            mode = spec.get("mode")
            state = self._execute_parts(sub_parts)
            if mode == "values":
                resolved[token] = [row[0] for row in (state.rows or [])]
            elif mode == "exists":
                resolved[token] = bool(state.rows)
            elif mode == "from":
                cols = [c[0] for c in state.description or []] if state.description else []
                rows_dicts = []
                for row in state.rows or []:
                    rows_dicts.append({cols[i]: row[i] for i in range(len(cols))})
                resolved[token] = rows_dicts
            else:
                resolved[token] = state.rows or []

        def _replace(obj: Any) -> Any:
            if isinstance(obj, str) and obj in resolved:
                return resolved[obj]
            if isinstance(obj, list):
                return [_replace(v) for v in obj]
            if isinstance(obj, dict):
                return {k: _replace(v) for k, v in obj.items()}
            return obj

        return replace(
            parts,
            filter=_replace(parts.filter),
            pipeline=_replace(parts.pipeline),
            values=_replace(parts.values),
            update=_replace(parts.update),
            subqueries=None,
            inline_token=None if (parts.inline_token and parts.inline_token in resolved) else parts.inline_token,
            collection=_replace(parts.collection) if isinstance(parts.collection, dict) else parts.collection,
            inline_rows=_replace(resolved.get(parts.inline_token)) if parts.inline_token else None,
        )

    def _match_filter(self, doc: dict, flt: Any) -> bool:
        if flt is None:
            return True
        if isinstance(flt, dict):
            for key, val in flt.items():
                if key == "$and":
                    if not all(self._match_filter(doc, f) for f in val):
                        return False
                    continue
                if key == "$or":
                    if not any(self._match_filter(doc, f) for f in val):
                        return False
                    continue
                if key == "$expr":
                    # already reduced to literal truthy/falsey
                    return bool(val.get("$literal"))
                actual = doc.get(key)
                if isinstance(val, dict):
                    for op, expected in val.items():
                        if op == "$in":
                            if actual not in expected:
                                return False
                        elif op == "$gte":
                            if not (actual >= expected):
                                return False
                        elif op == "$lte":
                            if not (actual <= expected):
                                return False
                        elif op == "$gt":
                            if not (actual > expected):
                                return False
                        elif op == "$lt":
                            if not (actual < expected):
                                return False
                        elif op == "$ne":
                            if actual == expected:
                                return False
                        elif op == "$regex":
                            import re

                            if not isinstance(actual, str):
                                return False
                            flags = re.I if val.get("$options") == "i" else 0
                            if not re.match(expected, actual, flags):
                                return False
                        else:
                            return False
                else:
                    if actual != val:
                        return False
            return True
        return False

    def _execute_from_subquery(self, parts: QueryParts) -> CursorState:
        rows = parts.inline_rows or []
        filtered = [r for r in rows if self._match_filter(r, parts.filter)]
        if parts.inline_aggregates:
            agg_result: dict[str, Any] = {}
            for alias, op, field in parts.inline_aggregates:
                if op == "count":
                    agg_result[alias] = len(filtered)
                elif op == "sum":
                    agg_result[alias] = sum((r.get(field) or 0) for r in filtered)
                elif op == "avg":
                    vals = [r.get(field) for r in filtered if r.get(field) is not None]
                    agg_result[alias] = (sum(vals) / len(vals)) if vals else None
                elif op == "min":
                    vals = [r.get(field) for r in filtered if r.get(field) is not None]
                    agg_result[alias] = min(vals) if vals else None
                elif op == "max":
                    vals = [r.get(field) for r in filtered if r.get(field) is not None]
                    agg_result[alias] = max(vals) if vals else None
            result_rows = [tuple(agg_result.get(alias) for alias, _, _ in parts.inline_aggregates)]
            description = [(alias, None, None, None, None, None, None) for alias, _, _ in parts.inline_aggregates]
            return CursorState(rows=result_rows, rowcount=len(result_rows), description=description)
        if parts.sort:
            for field, direction in reversed(parts.sort):
                filtered.sort(key=lambda r, f=field: r.get(f), reverse=direction == -1)
        if parts.skip:
            filtered = filtered[parts.skip :]
        if parts.limit:
            filtered = filtered[: parts.limit]
        columns = parts.projection or (sorted(filtered[0].keys()) if filtered else [])
        result_rows = [tuple(_convert_value(r.get(c)) for c in columns) for r in filtered]
        description = [(c, None, None, None, None, None, None) for c in columns] if columns else None
        return CursorState(rows=result_rows, rowcount=len(result_rows), description=description)

    def _execute_find(self, parts: QueryParts) -> CursorState:
        proj = None
        columns: list[str] | None = None
        if parts.projection_paths:
            proj = {path: 1 for path, _ in parts.projection_paths}
            columns = [alias for _, alias in parts.projection_paths]
        elif parts.projection:
            proj = {field: 1 for field in parts.projection}
            columns = parts.projection
        logger.debug(
            "Executing find / find 実行: collection=%s filter=%s projection=%s sort=%s limit=%s",
            parts.collection,
            parts.filter,
            proj,
            parts.sort,
            parts.limit,
        )
        cursor = self._db[parts.collection].find(parts.filter or {}, projection=proj, session=self._session)
        if parts.sort:
            cursor = cursor.sort(parts.sort)
        if parts.skip:
            cursor = cursor.skip(parts.skip)
        if parts.limit:
            cursor = cursor.limit(parts.limit)
        docs = list(cursor)
        if columns is None and docs:
            columns = sorted(docs[0].keys())
        rows = []
        for doc in docs:
            if parts.projection_paths:
                def _get_path(doc: dict, path: str) -> Any:
                    current = doc
                    for seg in path.split("."):
                        if isinstance(current, dict):
                            current = current.get(seg)
                        else:
                            current = None
                    return _convert_value(current)

                row = tuple(_get_path(doc, path) for path, _ in parts.projection_paths)
            else:
                row = tuple(_convert_value(doc.get(col)) for col in columns or [])
            rows.append(row)
        description = None
        if columns:
            description = [(col, None, None, None, None, None, None) for col in columns]
        return CursorState(rows=rows, rowcount=len(rows), description=description)

    def _execute_insert(self, parts: QueryParts) -> CursorState:
        logger.debug("Executing insert_one / insert_one 実行: collection=%s values=%s", parts.collection, parts.values)
        doc = {k: _convert_value(v) for k, v in (parts.values or {}).items()}
        result = self._db[parts.collection].insert_one(doc, session=self._session)
        return CursorState(rows=[], rowcount=1, lastrowid=_convert_value(result.inserted_id))

    def _execute_update(self, parts: QueryParts) -> CursorState:
        logger.debug(
            "Executing update_many / update_many 実行: collection=%s filter=%s update=%s",
            parts.collection,
            parts.filter,
            parts.update,
        )
        result = self._db[parts.collection].update_many(parts.filter or {}, parts.update or {}, session=self._session)
        return CursorState(rows=[], rowcount=result.modified_count)

    def _execute_delete(self, parts: QueryParts) -> CursorState:
        logger.debug(
            "Executing delete_many / delete_many 実行: collection=%s filter=%s",
            parts.collection,
            parts.filter,
        )
        result = self._db[parts.collection].delete_many(parts.filter or {}, session=self._session)
        return CursorState(rows=[], rowcount=result.deleted_count)

    def _execute_create_index(self, parts: QueryParts) -> CursorState:
        logger.debug(
            "Executing create_index / インデックス作成: collection=%s name=%s keys=%s unique=%s",
            parts.collection,
            parts.index_name,
            parts.index_keys,
            parts.unique,
        )
        try:
            self._db[parts.collection].create_index(parts.index_keys or [], name=parts.index_name, unique=parts.unique)
        except Exception:
            pass
        return CursorState(rows=[], rowcount=0)

    def _execute_drop_index(self, parts: QueryParts) -> CursorState:
        logger.debug("Executing drop_index / インデックス削除: collection=%s name=%s", parts.collection, parts.index_name)
        try:
            self._db[parts.collection].drop_index(parts.index_name)
        except Exception:
            pass
        return CursorState(rows=[], rowcount=0)

    def _execute_union_all(self, parts: QueryParts) -> CursorState:
        rows: list[tuple] = []
        description = None
        for sub in parts.union_parts or []:
            state = self._execute_parts(sub)
            if description is None:
                description = state.description
            rows.extend(state.rows or [])
        if parts.sort:
            rows.sort(key=lambda r: tuple(r[0:len(parts.sort)]), reverse=False)
        if parts.limit is not None:
            rows = rows[: parts.limit]
        return CursorState(rows=rows, rowcount=len(rows), description=description)

    def _execute_aggregate(self, parts: QueryParts) -> CursorState:
        logger.debug("Executing aggregate / aggregate 実行: collection=%s pipeline=%s", parts.collection, parts.pipeline)
        cursor = self._db[parts.collection].aggregate(parts.pipeline or [], session=self._session)
        docs = list(cursor)
        projection_paths = parts.projection_paths
        rows: list[tuple] = []

        def _get_path(doc: dict, path: str) -> Any:
            current = doc
            for seg in path.split("."):
                if isinstance(current, dict):
                    current = current.get(seg)
                else:
                    current = None
            return _convert_value(current)

        if projection_paths:
            columns = [out for _, out in projection_paths]
            for doc in docs:
                row = tuple(_get_path(doc, path) for path, _ in projection_paths)
                rows.append(row)
            description = [(col, None, None, None, None, None, None) for col in columns]
        else:
            if docs:
                join_keys = sorted([k for k in docs[0].keys() if k.startswith("__join")])
                columns_left = sorted([k for k in docs[0].keys() if not k.startswith("__join")])
                columns_join: list[str] = []
                for idx, jk in enumerate(join_keys):
                    if isinstance(docs[0].get(jk), dict):
                        columns_join.extend([f"{jk}.{k}" for k in sorted(docs[0][jk].keys())])
                columns = columns_left + columns_join
                for doc in docs:
                    left_vals = tuple(_convert_value(doc.get(k)) for k in columns_left)
                    join_vals_list = []
                    for jk in join_keys:
                        join_doc = doc.get(jk) or {}
                        for col in sorted(join_doc.keys()) if isinstance(join_doc, dict) else []:
                            join_vals_list.append(_convert_value(join_doc.get(col)))
                    rows.append(left_vals + tuple(join_vals_list))
                description = [(col, None, None, None, None, None, None) for col in columns] if columns else None
            else:
                description = None
        return CursorState(rows=rows, rowcount=len(rows), description=description)

    def _execute_create(self, parts: QueryParts) -> CursorState:
        logger.debug("Executing create_collection / コレクション作成: %s", parts.collection)
        try:
            self._db.create_collection(parts.collection)
        except Exception:
            pass
        return CursorState(rows=[], rowcount=0)

    def _execute_drop(self, parts: QueryParts) -> CursorState:
        logger.debug("Executing drop_collection / コレクション削除: %s", parts.collection)
        self._db.drop_collection(parts.collection)
        return CursorState(rows=[], rowcount=0)


def connect(uri: str, db_name: str, **_: Any) -> Connection:
    """DBAPI entry point / DBAPI エントリーポイント"""
    return Connection(uri, db_name)
