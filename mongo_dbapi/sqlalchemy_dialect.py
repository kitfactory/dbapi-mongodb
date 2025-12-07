from __future__ import annotations

from typing import Any, Dict, Tuple

from sqlalchemy.engine import default, url
from sqlalchemy import pool

import mongo_dbapi

# Register dialect entry point style
from sqlalchemy.dialects import registry  # noqa: E402

registry.register("mongodb+dbapi", "mongo_dbapi.sqlalchemy_dialect", "MongoDBAPIDialect")
registry.register("mongodb.dbapi", "mongo_dbapi.sqlalchemy_dialect", "MongoDBAPIDialect")


class MongoDBAPIDialect(default.DefaultDialect):
    name = "mongodb+dbapi"
    driver = "dbapi"
    paramstyle = "pyformat"
    supports_native_boolean = True
    supports_sane_rowcount = False
    supports_native_decimal = False
    default_paramstyle = "pyformat"
    poolclass = pool.SingletonThreadPool
    supports_statement_cache = False
    requires_name_normalize = True
    driver = "mongo-dbapi"

    @classmethod
    def dbapi(cls):
        return mongo_dbapi

    @classmethod
    def import_dbapi(cls):
        return mongo_dbapi

    def has_table(self, connection, table_name, schema=None, **kw):
        db = connection.connection._db  # pymongo database
        return table_name in db.list_collection_names()

    def get_driver_connection(self, connection):
        return connection.connection

    def create_connect_args(self, url_obj: url.URL) -> Tuple[tuple, Dict[str, Any]]:
        host = url_obj.host or "127.0.0.1"
        uri = f"mongodb://{host}"
        if url_obj.port:
            uri += f":{url_obj.port}"
        if url_obj.username:
            uri = uri.replace("mongodb://", f"mongodb://{url_obj.username}:{url_obj.password or ''}@")
        db_name = url_obj.database or ""
        return (), {"uri": uri, "db_name": db_name}


def dialect(**kwargs):
    return MongoDBAPIDialect(**kwargs)
