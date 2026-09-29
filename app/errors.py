from fastapi import HTTPException


def api_error(status_code: int, code: str, message: str, **extra) -> HTTPException:
    """Build an HTTPException with a structured, machine-readable detail body."""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, **extra},
    )
