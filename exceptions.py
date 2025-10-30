"""
Custom exceptions for replx.
"""

class ReplxError(Exception):
    """
    Custom exception for Replx operations.
    """
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return f"ReplxError: {self.message}"
