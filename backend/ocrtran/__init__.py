"""ocrtran — vision-model OCR + translation pipeline."""

from .config import ConfigError, PipelineConfig
from .events import Canceled, CancelToken, Event
from .pipeline import Pipeline, PipelineResult
from .providers import OpenAICompatibleProvider, ProviderError, build_provider

__version__ = "0.1.0"

__all__ = [
    "PipelineConfig",
    "ConfigError",
    "Pipeline",
    "PipelineResult",
    "Event",
    "CancelToken",
    "Canceled",
    "OpenAICompatibleProvider",
    "ProviderError",
    "build_provider",
]
