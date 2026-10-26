"""Bind fake calls against installed async SDK source without importing aiohttp.

Read AST signatures only; no SDK function body is executed or replaced.
"""

import ast
import inspect
import sys
from pathlib import Path

SITE_PACKAGES = Path(sys.prefix) / "Lib" / "site-packages"


def signature(path, class_name, method):
    tree = ast.parse((SITE_PACKAGES / path).read_text(encoding="utf-8"))
    cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    function = next(
        node
        for node in cls.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == method
    )
    args = function.args
    parameters = []
    positional = [*args.posonlyargs, *args.args]
    for i, arg in enumerate(positional):
        default = None if i >= len(positional) - len(args.defaults) else inspect.Parameter.empty
        kind = (
            inspect.Parameter.POSITIONAL_ONLY
            if i < len(args.posonlyargs)
            else inspect.Parameter.POSITIONAL_OR_KEYWORD
        )
        parameters.append(inspect.Parameter(arg.arg, kind, default=default))
    if args.vararg:
        parameters.append(inspect.Parameter(args.vararg.arg, inspect.Parameter.VAR_POSITIONAL))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        parameters.append(
            inspect.Parameter(
                arg.arg,
                inspect.Parameter.KEYWORD_ONLY,
                default=inspect.Parameter.empty if default is None else None,
            )
        )
    if args.kwarg:
        parameters.append(inspect.Parameter(args.kwarg.arg, inspect.Parameter.VAR_KEYWORD))
    return inspect.Signature(parameters)


def web_signature(method):
    return signature("slack_sdk/web/async_client.py", "AsyncWebClient", method)
