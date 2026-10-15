"""Dynamic passthrough proxy and shared base class for OPUS function bindings.

`OpusFunctionsProxy` exposes every OPUS function generated in
``opuspy_api.opus_commands.generated.opus_functions`` (401 functions) without
requiring an explicit wrapper, and adds discovery helpers
(:meth:`~OpusFunctionsProxy.list_functions`,
:meth:`~OpusFunctionsProxy.describe`).
"""

from __future__ import annotations

import difflib
from typing import Any, Callable


class _CommandGroup:
    """Base for curated OPUS command namespaces — holds the OpusFunctions handle."""

    def __init__(self, opus_api: Any) -> None:
        self._fn = opus_api.opus_functions
        self._api = opus_api


class OpusFunctionsProxy(_CommandGroup):
    """Dynamic passthrough to all OPUS functions plus discovery helpers.

    Any of the 401 generated OPUS functions can be called with its original
    PascalCase name::

        proxy.Measure(RES=4.0, NSS=32)
        proxy.Baseline(file_id, "AB")

    Discovery::

        proxy.list_functions()      # all available function names
        proxy.describe("Measure")   # input/return parameter codes + descriptions
    """

    def __getattr__(self, name: str) -> Callable[..., Any]:
        # Only reached when `name` is not found on the proxy itself
        target = getattr(self._fn, name, None)
        if target is not None and callable(target):
            return target

        suggestions = difflib.get_close_matches(name, self.list_functions(), n=3)
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        raise AttributeError(
            f"'{name}' is not an OPUS function.{hint} "
            "Use list_functions() to see all available functions."
        )

    def list_functions(self) -> list[str]:
        """Return all callable OPUS function names (excluding metadata helpers)."""
        return sorted(
            name
            for name in dir(type(self._fn))
            if not name.startswith("_")
            and not name.endswith("_parameters")
            and callable(getattr(self._fn, name))
        )

    def describe(self, function_name: str) -> dict[str, Any]:
        """Return parameter metadata for an OPUS function.

        Args:
            function_name: PascalCase OPUS function name, e.g. ``"Measure"``.

        Returns:
            Dict with keys:
              - ``function``: the function name
              - ``input_parameters``: list of 3-letter input parameter codes
              - ``return_parameters``: list of return parameter codes
              - ``parameter_details``: code → {description, dtype, default} for
                every input parameter that has registered metadata

        Raises:
            AttributeError: If the function (or its metadata) does not exist.
        """
        meta_fn = getattr(self._fn, f"{function_name}_parameters", None)
        if meta_fn is None:
            # Reuse __getattr__'s suggestion logic for a helpful error
            getattr(self, function_name)
            raise AttributeError(
                f"OPUS function '{function_name}' has no parameter metadata."
            )

        input_params, return_params = meta_fn()

        details: dict[str, dict[str, Any]] = {}
        try:
            infos = self._api.parameters.get_parameter_info(input_params)
            for info in infos:
                details[info.name] = {
                    "description": info.description,
                    "dtype": getattr(info.dtype, "__name__", str(info.dtype)),
                    "default": info.default_value,
                }
        except Exception:
            # Metadata enrichment is best-effort; codes alone are still useful
            pass

        return {
            "function": function_name,
            "input_parameters": input_params,
            "return_parameters": return_params,
            "parameter_details": details,
        }
