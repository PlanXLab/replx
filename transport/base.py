from abc import ABC, abstractmethod


class Transport(ABC):
    @abstractmethod
    def write(self, data: bytes) -> int:
        pass

    @abstractmethod
    def read(self, size: int = 1) -> bytes:
        pass

    def read_byte(self, timeout: float = None) -> bytes:
        return self.read(1)

    @abstractmethod
    def read_available(self, timeout_ms: int = 10) -> bytes:
        pass

    @abstractmethod
    def read_all(self) -> bytes:
        pass

    @abstractmethod
    def in_waiting(self) -> int:
        pass

    @abstractmethod
    def close(self) -> None:
        pass

    @abstractmethod
    def reset_input_buffer(self) -> None:
        pass

    @abstractmethod
    def reset_output_buffer(self) -> None:
        pass

    @property
    @abstractmethod
    def is_open(self) -> bool:
        pass
