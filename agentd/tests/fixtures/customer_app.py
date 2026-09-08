#!/usr/bin/env python3
"""Fixture web app for Browser QA tests: a tiny customer CRUD portal.

Standard library only, so agentd's browser tests need no dependencies in
the workspace under test. Routes:

    GET  /                       login form
    POST /login                  admin/secret → /customers, else error
    GET  /customers              welcome + customer list + create form
    POST /customers              create customer
    GET  /customers/<id>/edit    edit form
    POST /customers/<id>/edit    update customer
    POST /customers/<id>/delete  delete customer

Test knobs via environment variables:
    PORT                  listen port (default 8000; agentd passes it)
    INJECT_CONSOLE_ERROR  when set, every page emits console.error(...)
"""

import html
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CUSTOMERS: dict[int, str] = {}
NEXT_ID = [1]

CONSOLE_ERROR_SNIPPET = (
    '<script>console.error("fixture console error: boom");</script>'
    if os.environ.get("INJECT_CONSOLE_ERROR")
    else ""
)

PAGE = """<!doctype html>
<html><head><title>{title}</title></head>
<body>
{snippet}
{body}
</body></html>"""


def render(title: str, body: str) -> bytes:
    return PAGE.format(title=title, snippet=CONSOLE_ERROR_SNIPPET,
                       body=body).encode("utf-8")


def login_page(error: str = "") -> bytes:
    error_html = f'<p id="login-error">{html.escape(error)}</p>' if error else ""
    return render("Customer Portal — Login", f"""
<h1 id="app-title">Customer Portal</h1>
{error_html}
<form method="post" action="/login">
  <input id="username" name="username" placeholder="username">
  <input id="password" name="password" type="password" placeholder="password">
  <button id="login-btn" type="submit">Log in</button>
</form>""")


def customers_page() -> bytes:
    items = "\n".join(
        f'<li id="customer-{cid}">{html.escape(name)}'
        f' <a id="edit-{cid}" href="/customers/{cid}/edit">edit</a>'
        f' <form method="post" action="/customers/{cid}/delete"'
        f' style="display:inline">'
        f'<button id="delete-{cid}" type="submit">delete</button></form></li>'
        for cid, name in sorted(CUSTOMERS.items())
    )
    return render("Customers", f"""
<h1 id="welcome">Welcome, admin</h1>
<ul id="customer-list">
{items}
</ul>
<form id="create-form" method="post" action="/customers">
  <input id="name" name="name" placeholder="customer name">
  <button id="create-btn" type="submit">Create</button>
</form>""")


def edit_page(cid: int) -> bytes:
    name = html.escape(CUSTOMERS.get(cid, ""))
    return render("Edit customer", f"""
<h1 id="edit-title">Edit customer</h1>
<form method="post" action="/customers/{cid}/edit">
  <input id="edit-name" name="name" value="{name}">
  <button id="save-btn" type="submit">Save</button>
</form>""")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: A003 — quiet server
        pass

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        parsed = urllib.parse.parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}

    def do_GET(self):  # noqa: N802 — http.server API
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        elif self.path == "/":
            self._send(login_page())
        elif self.path == "/customers":
            self._send(customers_page())
        else:
            match = re.fullmatch(r"/customers/(\d+)/edit", self.path)
            if match and int(match.group(1)) in CUSTOMERS:
                self._send(edit_page(int(match.group(1))))
            else:
                self._send(render("Not found", "<h1>404</h1>"), status=404)

    def do_POST(self):  # noqa: N802 — http.server API
        form = self._form()
        if self.path == "/login":
            if form.get("username") == "admin" and form.get("password") == "secret":
                self._redirect("/customers")
            else:
                self._send(login_page("Invalid credentials"), status=401)
        elif self.path == "/customers":
            name = form.get("name", "").strip()
            if name:
                CUSTOMERS[NEXT_ID[0]] = name
                NEXT_ID[0] += 1
            self._redirect("/customers")
        else:
            edit = re.fullmatch(r"/customers/(\d+)/edit", self.path)
            delete = re.fullmatch(r"/customers/(\d+)/delete", self.path)
            if edit and int(edit.group(1)) in CUSTOMERS:
                CUSTOMERS[int(edit.group(1))] = form.get("name", "").strip()
                self._redirect("/customers")
            elif delete and int(delete.group(1)) in CUSTOMERS:
                del CUSTOMERS[int(delete.group(1))]
                self._redirect("/customers")
            else:
                self._send(render("Not found", "<h1>404</h1>"), status=404)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"customer_app listening on {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
