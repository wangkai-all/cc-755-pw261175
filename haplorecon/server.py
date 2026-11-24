"""Minimal WSGI application: JSON API plus the single-page UI.

Standard library only.  Run with::

    python3 -m haplorecon.server --port 8765

Routes
    GET  /                      -> the UI
    GET  /static/<file>         -> assets
    GET  /api/samples           -> bundled synthetic fixtures
    GET  /api/samples/<name>    -> fixture text
    GET  /api/datasets          -> list datasets
    POST /api/import            -> import fragment text, solve, persist
    GET  /api/datasets/<id>     -> current input + versions
    GET  /api/datasets/<id>/results            -> result version list
    GET  /api/datasets/<id>/results/<version>  -> full result document
    POST /api/datasets/<id>/connectors         -> add connector fragment, re-solve
    POST /api/datasets/<id>/locks              -> pin a position, re-solve
    POST /api/datasets/<id>/unlock             -> remove a pin, re-solve
    GET  /api/datasets/<id>/hints              -> connector suggestions
    GET  /api/datasets/<id>/compare?result=..&a=..&b=..  -> compare solution classes
    GET  /api/datasets/<id>/versions/compare?a=..&b=..   -> compare result versions
    GET  /api/datasets/<id>/export/input       -> text download
    GET  /api/datasets/<id>/export/result/<v>  -> JSON download
    GET  /api/samples/<name>/download          -> text download
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import traceback
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from .samples import SAMPLES
from .service import Service, ServiceError
from .store import Store

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_ROOT = os.path.join(HERE, "static")
MAX_BODY = 8 * 1024 * 1024


class Request:
    def __init__(self, environ: Dict[str, object], body: bytes):
        self.environ = environ
        self.method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        self.path = str(environ.get("PATH_INFO", "/")) or "/"
        self.query = parse_qs(str(environ.get("QUERY_STRING", "")))
        self.body = body
        self._json: Optional[Dict[str, object]] = None

    def json(self) -> Dict[str, object]:
        if self._json is None:
            if not self.body:
                self._json = {}
            else:
                try:
                    parsed = json.loads(self.body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ServiceError("bad_json", f"request body is not valid JSON: {exc}")
                if not isinstance(parsed, dict):
                    raise ServiceError("bad_json", "request body must be a JSON object")
                self._json = parsed
        return self._json

    def arg(self, name: str, default: Optional[str] = None) -> Optional[str]:
        vals = self.query.get(name)
        return vals[0] if vals else default

    def int_arg(self, name: str, default: Optional[int] = None) -> Optional[int]:
        raw = self.arg(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            raise ServiceError("bad_argument", f"query argument {name!r} must be an integer")


class Response:
    def __init__(self, status: int, body: bytes, content_type: str,
                 headers: Optional[List[Tuple[str, str]]] = None):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or []


def json_response(payload: object, status: int = 200) -> Response:
    return Response(
        status,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        "application/json; charset=utf-8",
    )


def _status_line(code: int) -> str:
    return {
        200: "200 OK",
        201: "201 Created",
        400: "400 Bad Request",
        404: "404 Not Found",
        405: "405 Method Not Allowed",
        413: "413 Payload Too Large",
        500: "500 Internal Server Error",
    }.get(code, f"{code} Unknown")


class App:
    def __init__(self, store: Store):
        self.store = store
        self.service = Service(store)
        self.routes: List[Tuple[str, re.Pattern, Callable[[Request, Tuple[str, ...]], Response], str]] = []
        self._register()

    def _route(self, method: str, pattern: str, fn, *methods: str) -> None:
        self.routes.append((method, re.compile("^" + pattern + "$"), fn, method))

    def _register(self) -> None:
        self._route("GET", r"/", self.index)
        self._route("GET", r"/static/(?P<file>[A-Za-z0-9_.\-]+)", self.static)
        self._route("GET", r"/api/samples", self.samples)
        self._route("GET", r"/api/samples/(?P<name>[A-Za-z0-9_\-]+)", self.sample_text)
        self._route("GET", r"/api/samples/(?P<name>[A-Za-z0-9_\-]+)/download", self.sample_download)
        self._route("GET", r"/api/datasets", self.list_datasets)
        self._route("POST", r"/api/import", self.import_data)
        self._route("GET", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)", self.get_dataset)
        self._route("GET", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/jobs", self.list_jobs)
        self._route("GET", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/results", self.list_results)
        self._route("GET", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/results/(?P<version>\d+)", self.get_result)
        self._route("POST", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/connectors", self.add_connector)
        self._route("POST", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/locks", self.add_lock)
        self._route("POST", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/unlock", self.remove_lock)
        self._route("POST", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/resolve", self.resolve)
        self._route("GET", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/hints", self.hints)
        self._route("GET", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/compare", self.compare_solutions)
        self._route("GET", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/versions/compare", self.compare_versions)
        self._route("GET", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/export/input", self.export_input)
        self._route("GET", r"/api/datasets/(?P<dataset_id>[A-Za-z0-9_\-]+)/export/result", self.export_result)

    # -- dispatch ---------------------------------------------------------

    def __call__(self, environ, start_response):
        try:
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length > MAX_BODY:
                raise ServiceError("too_large", "request body exceeds 8 MB")
            body = environ["wsgi.input"].read(length) if length else b""
            request = Request(environ, body)
            response = self.dispatch(request)
        except ServiceError as exc:
            response = json_response(exc.to_dict(), 400)
        except KeyError as exc:
            response = json_response({"error": "not_found", "message": str(exc)}, 404)
        except Exception:  # noqa: BLE001 - keep the server alive, log the trace
            traceback.print_exc()
            response = json_response(
                {"error": "internal", "message": "unexpected server error"}, 500
            )
        start_response(_status_line(response.status), [
            ("Content-Type", response.content_type),
            ("Content-Length", str(len(response.body))),
            ("Cache-Control", "no-store"),
        ] + response.headers)
        return [response.body]

    def dispatch(self, request: Request) -> Response:
        allowed_methods = set()
        for method, pattern, fn, _ in self.routes:
            match = pattern.match(request.path)
            if not match:
                continue
            allowed_methods.add(method)
            if method == request.method:
                return fn(request, match.groupdict())
        if allowed_methods:
            return json_response(
                {"error": "method_not_allowed",
                 "message": f"{request.method} not allowed on {request.path}"},
                405,
            )
        return json_response({"error": "not_found", "message": f"no route for {request.path}"}, 404)

    # -- static -----------------------------------------------------------

    def index(self, request: Request, params) -> Response:
        path = os.path.join(STATIC_ROOT, "index.html")
        with open(path, "rb") as fh:
            return Response(200, fh.read(), "text/html; charset=utf-8")

    def static(self, request: Request, params) -> Response:
        name = params["file"]
        path = os.path.join(STATIC_ROOT, name)
        if not os.path.isfile(path):
            raise ServiceError("not_found", f"no such asset {name}")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        with open(path, "rb") as fh:
            return Response(200, fh.read(), ctype)

    # -- samples ----------------------------------------------------------

    def samples(self, request: Request, params) -> Response:
        return json_response(
            [
                {
                    "name": s.name,
                    "title": s.title,
                    "description": s.description,
                    "expectation": s.expectation,
                }
                for s in SAMPLES
            ]
        )

    def _sample(self, name: str):
        for s in SAMPLES:
            if s.name == name:
                return s
        raise ServiceError("not_found", f"no sample named {name!r}")

    def sample_text(self, request: Request, params) -> Response:
        s = self._sample(params["name"])
        return json_response({"name": s.name, "title": s.title, "text": s.text,
                              "description": s.description, "expectation": s.expectation})

    def sample_download(self, request: Request, params) -> Response:
        s = self._sample(params["name"])
        return Response(200, s.text.encode("utf-8"), "text/plain; charset=utf-8",
                        [("Content-Disposition", f'attachment; filename="sample-{s.name}.txt"')])

    # -- datasets ---------------------------------------------------------

    def list_datasets(self, request: Request, params) -> Response:
        rows = self.store.list_datasets()
        for r in rows:
            try:
                res = self.store.get_result(str(r["id"]))
                r["latest_result"] = {
                    "version": res.version,
                    "status": res.summary.get("status"),
                    "cost": res.summary.get("cost"),
                    "proven": res.summary.get("proven"),
                }
            except KeyError:
                r["latest_result"] = None
        return json_response({"datasets": rows})

    def import_data(self, request: Request, params) -> Response:
        body = request.json()
        text = body.get("text")
        if not isinstance(text, str):
            raise ServiceError("bad_request", "'text' must be a string")
        payload = self.service.import_text(
            text,
            name=body.get("name") if isinstance(body.get("name"), str) else None,
            notes=str(body.get("notes") or ""),
            options=body.get("options") if isinstance(body.get("options"), dict) else None,
        )
        return json_response(payload, 201)

    def get_dataset(self, request: Request, params) -> Response:
        view = self.service.dataset_view(params["dataset_id"])
        return json_response(view)

    def list_jobs(self, request: Request, params) -> Response:
        jobs = self.store.list_jobs(params["dataset_id"])
        return json_response({"jobs": jobs})

    # -- results ----------------------------------------------------------

    def list_results(self, request: Request, params) -> Response:
        did = params["dataset_id"]
        self.service.dataset_view(did)  # 404 when unknown
        return json_response({"results": self.store.list_results(did)})

    def get_result(self, request: Request, params) -> Response:
        did = params["dataset_id"]
        version = int(params["version"])
        return json_response(self.service.result_payload(did, version))

    def add_connector(self, request: Request, params) -> Response:
        body = request.json()
        fid = body.get("fid")
        if not isinstance(fid, str) or not fid.strip():
            raise ServiceError("bad_request", "'fid' must be a non-empty string")
        raw = body.get("alleles")
        if not isinstance(raw, dict) or not raw:
            raise ServiceError(
                "bad_request", "'alleles' must map positions to 0/1, e.g. {\"12\": 0, \"13\": 1}"
            )
        alleles: Dict[int, int] = {}
        for k, v in raw.items():
            try:
                p = int(k)
            except (TypeError, ValueError):
                raise ServiceError("bad_request", f"position key {k!r} is not an integer")
            if v not in (0, 1):
                raise ServiceError("bad_request", f"allele at position {p} must be 0 or 1")
            alleles[p] = int(v)
        return json_response(
            self.service.add_connector(
                params["dataset_id"], fid.strip(), alleles,
                options=body.get("options") if isinstance(body.get("options"), dict) else None,
            ),
            201,
        )

    def add_lock(self, request: Request, params) -> Response:
        body = request.json()
        try:
            position = int(body.get("position"))
        except (TypeError, ValueError):
            raise ServiceError("bad_request", "'position' must be an integer")
        hap0 = body.get("hap0")
        hap1 = body.get("hap1")
        for name, v in (("hap0", hap0), ("hap1", hap1)):
            if v is not None and v not in (0, 1):
                raise ServiceError("bad_request", f"'{name}' must be 0, 1 or null")
        return json_response(
            self.service.lock_position(
                params["dataset_id"], position,
                hap0=None if hap0 is None else int(hap0),
                hap1=None if hap1 is None else int(hap1),
                distinct=bool(body.get("distinct", False)),
                note=str(body.get("note") or ""),
                options=body.get("options") if isinstance(body.get("options"), dict) else None,
            ),
            201,
        )

    def remove_lock(self, request: Request, params) -> Response:
        body = request.json()
        try:
            position = int(body.get("position"))
        except (TypeError, ValueError):
            raise ServiceError("bad_request", "'position' must be an integer")
        return json_response(
            self.service.unlock_position(
                params["dataset_id"], position,
                options=body.get("options") if isinstance(body.get("options"), dict) else None,
            ),
            201,
        )

    def resolve(self, request: Request, params) -> Response:
        body = request.json()
        options = body.get("options") if isinstance(body.get("options"), dict) else None
        return json_response(self.service.resolve(params["dataset_id"], options), 201)

    def hints(self, request: Request, params) -> Response:
        version = request.int_arg("result")
        return json_response(
            self.service.hints(params["dataset_id"], version)
        )

    def compare_solutions(self, request: Request, params) -> Response:
        version = request.int_arg("result")
        a = request.int_arg("a")
        b = request.int_arg("b")
        if a is None or b is None:
            raise ServiceError("bad_argument", "needs a= and b= solution ids")
        return json_response(self.service.compare_solutions(params["dataset_id"], version, a, b))

    def compare_versions(self, request: Request, params) -> Response:
        a = request.int_arg("a")
        b = request.int_arg("b")
        if a is None or b is None:
            raise ServiceError("bad_argument", "needs a= and b= result versions")
        return json_response(self.service.compare_results(params["dataset_id"], a, b))

    def export_input(self, request: Request, params) -> Response:
        text, filename = self.service.export_input_text(params["dataset_id"])
        return Response(200, text.encode("utf-8"), "text/plain; charset=utf-8",
                        [("Content-Disposition", f'attachment; filename="{filename}"')])

    def export_result(self, request: Request, params) -> Response:
        version = request.int_arg("version")
        text, filename = self.service.export_result_json(params["dataset_id"], version)
        return Response(200, text.encode("utf-8"), "application/json; charset=utf-8",
                        [("Content-Disposition", f'attachment; filename="{filename}"')])


class QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):  # noqa: D401 - silence per-request logging
        pass


def create_app(db_path: str) -> Tuple[App, Store]:
    store = Store(db_path)
    return App(store), store


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    app, store = create_app(db_path)
    httpd = make_server(host, port, app, handler_class=QuietHandler)
    print(f"haplorecon listening on http://{host}:{port}/  (db: {db_path})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        store.close()
