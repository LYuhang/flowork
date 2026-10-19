"""Path-prefix adaptation for sandbox services hosted beside the application.

The gateway enforces an opaque browser origin with CSP sandbox. This module
only adapts resource addresses; it is not an authorization boundary.
"""

import json
import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


def resource_url(value: str, prefix: str) -> str:
    if value.startswith("/") and not value.startswith("//"):
        return prefix + value.lstrip("/")
    return value


def css_urls(source: str, prefix: str) -> str:
    return re.sub(
        r"(url\(\s*['\"]?)(/[^/][^)'\"]*)",
        lambda match: match[1] + resource_url(match[2], prefix),
        source,
    )


def bridge_script(prefix: str) -> str:
    # An opaque-origin frame must explicitly omit the platform's credentials.
    # Resolve relative requests against the document, root-relative requests
    # against the service. No command execution or automatic request retries.
    return """<script>(()=>{
const base=PREFIX;
const map=value=>{const u=new URL(String(value),document.baseURI);
if(u.host===location.host && ['http:','https:','ws:','wss:'].includes(u.protocol) && !u.pathname.startsWith(base))u.pathname=base+u.pathname.replace(/^\\//,'');
return u.href;};
const fetchOriginal=window.fetch;
window.fetch=(input,init)=>input instanceof Request
?fetchOriginal(new Request(map(input.url),input),{...init,credentials:'omit'})
:fetchOriginal(map(input),{...init,credentials:'omit'});
const open=XMLHttpRequest.prototype.open;
XMLHttpRequest.prototype.open=function(method,url,...rest){return open.call(this,method,map(url),...rest)};
for(const name of ['WebSocket','EventSource']){const Original=window[name];
window[name]=class extends Original{constructor(url,options){super(map(url),options)}};}
})();</script>""".replace("PREFIX", json.dumps(prefix))


class ServiceHTML(HTMLParser):
    def __init__(self, prefix: str, path: str):
        super().__init__(convert_charrefs=False)
        self.prefix = prefix
        self.output = []
        self.injected = False
        directory = urljoin(path, ".")
        self.bootstrap = (
            '<base href="' + escape(prefix + directory.lstrip("/"), quote=True) + '">' + bridge_script(prefix)
        )

    def inject(self):
        if not self.injected:
            self.output.append(self.bootstrap)
            self.injected = True

    def handle_starttag(self, tag, attrs):
        if tag not in {"html", "head"}:
            self.inject()
        if tag == "base":
            return
        rendered = []
        for name, value in attrs:
            if value is not None:
                if name in {"src", "href", "action", "poster", "data"}:
                    value = resource_url(value, self.prefix)
                elif name == "srcset":
                    value = ", ".join(resource_url(part.strip(), self.prefix) for part in value.split(","))
                elif name == "style":
                    value = css_urls(value, self.prefix)
            rendered.append(name if value is None else f'{name}="{escape(value, quote=True)}"')
        self.output.append("<" + tag + (" " + " ".join(rendered) if rendered else "") + ">")
        if tag == "head":
            self.inject()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag != "base":
            self.output.append(f"</{tag}>")

    def handle_data(self, data):
        self.output.append(data)

    def handle_entityref(self, name):
        self.output.append(f"&{name};")

    def handle_charref(self, name):
        self.output.append(f"&#{name};")

    def handle_comment(self, data):
        self.output.append(f"<!--{data}-->")

    def handle_decl(self, decl):
        self.output.append(f"<!{decl}>")


def rewrite_document(body: bytes, content_type: str, prefix: str, path: str) -> bytes:
    source = body.decode("utf-8")
    if "text/html" in content_type:
        parser = ServiceHTML(prefix, path)
        parser.feed(source)
        parser.close()
        parser.inject()
        return "".join(parser.output).encode()
    if "text/css" in content_type:
        return css_urls(source, prefix).encode()
    return body


def redirect_url(value: str, prefix: str, port: int) -> str:
    parsed = urlsplit(value)
    if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        actual_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if actual_port == port:
            value = (
                (parsed.path or "/")
                + ("?" + parsed.query if parsed.query else "")
                + ("#" + parsed.fragment if parsed.fragment else "")
            )
    return resource_url(value, prefix)
