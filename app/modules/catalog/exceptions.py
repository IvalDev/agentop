"""Exceptions raised by the module catalog.

``ModuleRegistrationError`` fires while a catalog is built (import time): a
manifest violated a registration invariant, so a broken module never reaches
any tenant. ``UnknownModuleError`` fires on lookup — the router translates
it to a 404, so a request can never activate or inspect a module the code
does not declare. Both share :class:`ModuleCatalogError` for handlers that
want one catch clause.
"""


class ModuleCatalogError(Exception):
    """Base class for every module-catalog error."""


class ModuleRegistrationError(ModuleCatalogError):
    """A ModuleManifest violated an invariant (duplicate/invalid key, blank text, bad step)."""


class UnknownModuleError(ModuleCatalogError):
    """No module with the requested key is registered in this catalog."""
