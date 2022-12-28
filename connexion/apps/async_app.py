"""
This module defines a native connexion asynchronous application.
"""

import asyncio
import logging
import pathlib
import pkgutil
import sys
import typing as t

from starlette.responses import JSONResponse as StarletteJSONResponse
from starlette.responses import Response as StarletteResponse
from starlette.routing import Router
from starlette.types import Receive, Scope, Send

from connexion.apis.abstract import AbstractAPI
from connexion.apps.abstract import AbstractApp
from connexion.decorators.lifecycle import RequestResponseDecorator
from connexion.decorators.parameter import parameter_to_arg
from connexion.exceptions import MissingMiddleware, ProblemException
from connexion.lifecycle import ConnexionResponse, MiddlewareRequest, MiddlewareResponse
from connexion.middleware.main import ConnexionMiddleware
from connexion.middleware.routing import ROUTING_CONTEXT
from connexion.operations import AbstractOperation
from connexion.uri_parsing import AbstractURIParser

logger = logging.getLogger("Connexion.app")


class AsyncAsgiApp:
    """Mixin for usage with Abstract Apps & Apis."""

    def __init__(self, *args, base_path="", **kwargs):
        self.apis: t.Dict[str, AsyncApi] = {}
        self.operations: t.Dict[str, AsyncOperation] = {}
        self.router = Router()
        self.base_path = base_path
        super(AsyncAsgiApp, self).__init__(*args, **kwargs)

    def add_api(self, specification: t.Union[pathlib.Path, str, dict], **kwargs):
        api = super(AsyncAsgiApp, self).add_api(specification, **kwargs)  # type: ignore
        self.apis[api.base_path] = api
        return api

    def add_url_rule(self, rule, endpoint=None, view_func=None, **options):
        self.router.add_route(path=rule, endpoint=view_func, name=endpoint, **options)

    def route(self, rule: str, **kwargs):
        """
        A decorator that is used to register a view function for a given URL rule.
        This does the same thing as `add_url_rule` but is intended for decorator usage::

            @app.route('/')
            def index():
                return 'Hello World'

        :param rule: the URL rule as string
        :type rule: str
        :param kwargs: kwargs to be forwarded to the underlying `starlette.routes.Route` object.
        """

        def decorator(func: t.Callable) -> t.Callable:
            self.router.add_route(rule, **kwargs)
            return func

        return decorator

    async def asgi_app(self, scope: Scope, receive: Receive, send: Send) -> None:
        """The actual ASGI application. This is not implemented in
        `__call__` so that middlewares can be applied without
        losing a reference to the app object.

        It tries to route first based on the RoutingMiddleware, otherwise it falls back on routes
        manually registered on the app.
        """
        try:
            connexion_context = scope["extensions"][ROUTING_CONTEXT]
        except KeyError:
            raise MissingMiddleware(
                "Could not find routing information in scope. Please make sure "
                "you have a routing middleware registered upstream. "
            )

        api_base_path = connexion_context.get("api_base_path")
        if (
            api_base_path is not None
            and api_base_path in self.apis
            and not api_base_path == self.base_path
        ):
            api = self.apis[api_base_path]
            return await api(scope, receive, send)

        else:
            operation_id = connexion_context.get("operation_id")
            try:
                # Check if route was registered by middleware
                operation = self.operations[operation_id]
            except KeyError as e:
                if operation_id is None:
                    # Check if route was manually registered
                    await self.router(scope, receive, send)
                else:
                    raise MissingAsyncOperation(
                        "Encountered unknown operation_id."
                    ) from e
            else:
                response = await operation(scope, receive, send)
                while asyncio.iscoroutine(response):
                    response = await response
                return await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        await self.asgi_app(scope, receive, send)


class AsyncApp(AsyncAsgiApp, AbstractApp):
    def __init__(self, *args, **kwargs) -> None:
        super(AsyncApp, self).__init__(*args, api_cls=AsyncApi, **kwargs)

    def _apply_middleware(self, middlewares: list) -> ConnexionMiddleware:
        middleware = ConnexionMiddleware(self.asgi_app, middlewares=middlewares)
        self.asgi_app = middleware  # type: ignore
        return middleware

    def get_root_path(self) -> str:
        # Module already imported and has a file attribute. Use that first.
        mod = sys.modules.get(self.import_name)

        if mod is not None and hasattr(mod, "__file__"):
            return str(pathlib.Path(mod.__file__).resolve().parent)  # type: ignore

        loader = pkgutil.get_loader(self.import_name)

        if hasattr(loader, "get_filename"):
            filepath = loader.get_filename(self.import_name)  # type: ignore
        else:
            raise RuntimeError(f"Invalid import name '{self.import_name}'")

        return str(pathlib.Path(filepath).resolve().parent)

    def set_errors_handlers(self):
        pass


class AsyncApi(AsyncAsgiApp, AbstractAPI):
    def __init__(self, *args, **kwargs) -> None:
        super(AsyncApi, self).__init__(*args, **kwargs)
        self.add_paths()

    def add_operation(self, path: str, method: str) -> None:
        operation_cls = self.specification.operation_cls
        operation = operation_cls.from_spec(
            self.specification, self, path, method, self.resolver
        )
        async_operation = AsyncOperation.from_operation(operation, self.pythonic_params)
        self._add_operation_internal(method, path, async_operation)

    def _add_operation_internal(
        self, method: str, path: str, operation: "AsyncOperation"
    ) -> None:
        self.operations[operation.operation_id] = operation

    @classmethod
    def get_request(cls, **kwargs) -> MiddlewareRequest:
        scope = kwargs.pop("scope")
        receive = kwargs.pop("receive")
        return MiddlewareRequest(scope, receive)

    @classmethod
    async def get_response(cls, response, mimetype=None):
        while asyncio.iscoroutine(response):
            response = await response

        return cls._get_response(response, mimetype=mimetype)

    @classmethod
    def _is_framework_response(cls, response):
        return isinstance(response, StarletteResponse) and not isinstance(
            response, MiddlewareResponse
        )

    @classmethod
    def _framework_to_connexion_response(cls, response, mimetype):
        # FileResponse and StreamingResponse do not a `body` (yet)
        body = None
        if hasattr(response, "body"):
            body = response.body

        return ConnexionResponse(
            status_code=response.status_code,
            mimetype=mimetype,
            content_type=response.media_type,
            headers=response.headers,
            body=body,
        )

    @classmethod
    def _connexion_to_framework_response(cls, response, mimetype):
        return cls._build_response(
            mimetype=response.mimetype or mimetype,
            status_code=response.status_code,
            content_type=response.content_type,
            headers=response.headers,
            data=response.body,
        )

    @classmethod
    def _build_response(
        cls, data, mimetype, content_type=None, status_code=None, headers=None
    ):
        if cls._is_framework_response(data):
            raise TypeError(
                "Cannot return starlette.responses.Response in tuple. Only raw data can be returned in tuple."
            )
        data, status_code, serialized_mimetype = cls._prepare_body_and_status_code(
            data=data,
            mimetype=mimetype,
            status_code=status_code,
        )

        content_type = content_type or mimetype or serialized_mimetype

        if content_type is None:
            if isinstance(data, str):
                content_type = "text/plain"
            elif isinstance(data, bytes):
                content_type = "application/octet-stream"

        if isinstance(data, dict) or isinstance(data, list):
            response_cls = StarletteJSONResponse
        else:
            response_cls = StarletteResponse

        return response_cls(
            content=data,
            status_code=status_code,
            media_type=content_type,
            headers=headers,
        )


class AsyncOperation:
    def __init__(
        self,
        operation: AbstractOperation,
        fn: t.Callable,
        uri_parser: AbstractURIParser,
        api: AbstractAPI,
        mimetype: str,
        operation_id: str,
        pythonic_params: bool,
    ) -> None:
        self._operation = operation
        self._fn = fn
        self.uri_parser = uri_parser
        self.api = api
        self.mimetype = mimetype
        self.operation_id = operation_id
        self.pythonic_params = pythonic_params

    @classmethod
    def from_operation(
        cls, operation: AbstractOperation, pythonic_params: bool
    ) -> "AsyncOperation":
        return cls(
            operation,
            fn=operation._resolution.function,
            uri_parser=operation._uri_parsing_decorator,
            api=operation.api,
            # TODO: this should be the response mimetype
            mimetype=operation.get_mimetype(),
            operation_id=operation.operation_id,
            pythonic_params=pythonic_params,
        )

    @property
    def fn(self) -> t.Callable:
        fn = parameter_to_arg(self._operation, self._fn, self.pythonic_params)
        return RequestResponseDecorator(self.api, self.mimetype)(
            fn, uri_parser=self.uri_parser
        )

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> StarletteResponse:
        if asyncio.iscoroutinefunction(self.fn):
            return await self.fn(scope=scope, receive=receive, send=send)
        else:
            return self.fn(scope=scope, receive=receive, send=send)


class MissingAsyncOperation(ProblemException):
    def __init__(self, *args, **kwargs) -> None:
        super(MissingAsyncOperation, self).__init__(500, *args, **kwargs)
