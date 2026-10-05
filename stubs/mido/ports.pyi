class BaseInput:
    name: str
    closed: bool
    def close(self) -> None: ...
