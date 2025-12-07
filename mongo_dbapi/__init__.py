from .dbapi import Connection, Cursor, connect
from .errors import MongoDbApiError

# DB-API style exception hook for SQLAlchemy / DB-API 仕様の Error エイリアス
Error = MongoDbApiError

apilevel = "2.0"
threadsafety = 1
paramstyle = "pyformat"

from . import sqlalchemy_dialect  # noqa: E402,F401 register dialect

__all__ = ["connect", "Connection", "Cursor", "MongoDbApiError", "Error", "apilevel", "threadsafety", "paramstyle"]
