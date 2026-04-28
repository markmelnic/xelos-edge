from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("xelos-edge")
except PackageNotFoundError:
    __version__ = "0.0.0+local"
