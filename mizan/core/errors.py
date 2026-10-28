class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str = "", **extra):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.extra = extra


class NotFound(AppError):
    status, code = 404, "not_found"


class Conflict(AppError):
    status, code = 409, "conflict"


class Forbidden(AppError):
    status, code = 403, "forbidden"


class Invalid(AppError):
    status, code = 422, "invalid"
