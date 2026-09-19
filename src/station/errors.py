class ExternalError(Exception):
    def __init__(self, message, *, status=400, reason="", retryable=None):
        super().__init__(message)
        self.status = status
        self.reason = reason
        self.retryable = retryable


class InternalError(Exception):
    pass
