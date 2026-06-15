# routes/__init__.py
from .analyze import router as analyze_router
from .generate import router as generate_router
from .status import router as status_router
from .download import router as download_router

__all__ = ["analyze_router", "generate_router", "status_router", "download_router"]
