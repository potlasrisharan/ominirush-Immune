"""Terminal user interface helpers for OmniResilience."""

__all__ = ["OmniHUD"]


def __getattr__(name: str):
    """Load the terminal HUD only when it is requested.

    The browser dashboard does not need Rich, so keeping this import lazy lets
    the standalone web command start in a minimal runtime as well.
    """
    if name == "OmniHUD":
        from .hud import OmniHUD

        return OmniHUD
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
