class MongoDbApiError(Exception):
    """Mongo DBAPI error with Error ID prefix / Mongo DBAPI エラー（Error ID 付き）"""

    def __init__(self, code: str, message: str, cause: Exception | None = None):
        full = f"{code} {message}"
        super().__init__(full)
        self.code = code
        self.message = message
        if cause:
            self.__cause__ = cause


ERROR_MESSAGES: dict[str, str] = {
    "[mdb][E1]": "Invalid connection URI",
    "[mdb][E2]": "Unsupported SQL construct: <keyword>",
    "[mdb][E3]": "Unsafe operation without WHERE",
    "[mdb][E4]": "Parameter count mismatch",
    "[mdb][E5]": "Failed to parse SQL",
    "[mdb][E6]": "Transactions not supported on this server",
    "[mdb][E7]": "Connection failed",
    "[mdb][E8]": "Authentication failed",
}


def raise_error(code: str, message: str | None = None, cause: Exception | None = None) -> None:
    """Raise MongoDbApiError with code / コード付き例外を送出"""
    msg = message or ERROR_MESSAGES.get(code, "")
    raise MongoDbApiError(code, msg, cause)
