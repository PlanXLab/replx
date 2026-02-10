class ReplxException(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return f"{self.__class__.__name__}: {self.message}"


class TransportError(ReplxException):
    pass


class SerialError(TransportError):
    pass


class ProtocolError(ReplxException):
    pass


class RawReplError(ProtocolError):
    pass


class RawPasteError(ProtocolError):
    pass


class ExecutionError(ProtocolError):
    pass


class FileSystemError(ReplxException):
    pass


class DownloadError(FileSystemError):
    pass


class UploadError(FileSystemError):
    pass


class CLIError(ReplxException):
    pass


class ValidationError(CLIError):
    pass


class CompilationError(CLIError):
    pass
