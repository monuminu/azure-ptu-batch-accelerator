"""Errors translated to the OpenAI HTTP error envelope by the API."""


class BatchError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        code: str = "invalid_request_error",
        param: str | None = None,
        error_type: str = "invalid_request_error",
    ):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code
        self.param, self.error_type = param, error_type

    def envelope(self):
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "param": self.param,
                "code": self.code,
            }
        }


class AuthError(BatchError):
    def __init__(
        self,
        message="Invalid authentication credentials",
        status=401,
        code="invalid_api_key",
        **kwargs,
    ):
        super().__init__(status, message, code, error_type="authentication_error", **kwargs)
