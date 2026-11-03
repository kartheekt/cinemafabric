"""CinemaFabric: a one-line idea to a trailer or short film, on a hybrid local/cloud inference fabric."""

try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # pragma: no cover - optional at runtime
    pass

__version__ = "0.1.0"
