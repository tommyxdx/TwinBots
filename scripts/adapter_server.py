"""Local GET-JSON field mapper; does not manufacture features or send trades.

python scripts/adapter_server.py --config adapter.example.json
Then configure http://127.0.0.1:8787/features?... or /quote in config.yaml.
Mapping: normalized output path -> actual provider response path.
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse,parse_qs,urlencode
from urllib.request import Request,build_opener,HTTPRedirectHandler


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Upstream redirects are disabled")


def map_fields(raw,fields):
    out = {}
    for target,source in fields.items():
        value = raw
        try:
            for part in source.split("."):
                value = value[int(part)] if isinstance(value,list) else value[part]
        except (KeyError,IndexError,ValueError,TypeError):
            continue  # Missing stays unknown, never becomes a positive check.
        dest = out
        parts = target.split(".")
        for part in parts[:-1]:
            dest = dest.setdefault(part,{})
        dest[parts[-1]] = value
    return out


def handler(config):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass  # Never log URLs containing credentials/query tokens.

        def do_GET(self):
            path = urlparse(self.path)
            kind = path.path.strip("/")
            c = config.get(kind) if kind in ("features","quote") else None
            code = 200
            try:
                if not c or not c.get("upstream_url"):
                    raise ValueError("Configure upstream_url and response field mappings first")
                u = urlparse(c["upstream_url"])
                if u.scheme!="https":
                    raise ValueError("Upstream must use HTTPS")
                incoming = parse_qs(path.query)
                params = {dest:incoming[src][0] for src,dest in c["params"].items() if src in incoming}
                params.update(c.get("fixed_params",{}))
                headers = {"User-Agent":"TwinCryptoBots-Adapter/1.1.1"}
                key = os.getenv(c.get("api_key_env",""),"")
                if key:
                    headers[c.get("header","Authorization")] = c.get("header_prefix","")+key
                url = c["upstream_url"]+("&" if "?" in c["upstream_url"] else "?")+urlencode(params)
                with build_opener(RejectRedirects()).open(Request(url,headers=headers),timeout=15) as response:
                    raw = response.read(1024*1024+1)
                if len(raw)>1024*1024:
                    raise ValueError("Upstream response too large")
                result = map_fields(json.loads(raw),c["fields"])
            except Exception as exc:
                code,result = 502,{"error":"Adapter unavailable or mapping incomplete","type":type(exc).__name__}
            body = json.dumps(result,allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type","application/json")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--config",default="adapter.example.json")
    args=p.parse_args()
    c=json.loads(Path(args.config).read_text(encoding="utf-8"))
    if c.get("listen","127.0.0.1") not in ("127.0.0.1","localhost"):
        raise ValueError("Bind this unauthenticated local adapter to loopback only")
    server=ThreadingHTTPServer((c.get("listen","127.0.0.1"),c.get("port",8787)),handler(c))
    print("Local read-only adapter running. Ctrl+C stops it.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
