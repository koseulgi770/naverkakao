"""
세 플랫폼(네이버 블로그, 블로그스팟, 워드프레스)에 블로그 글을 업로드합니다.
"""
import base64
import json
import os
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import urlencode, urlparse, parse_qs

import requests


# ──────────────────────────────────────────────
# 네이버 블로그 (Naver Open API)
# ──────────────────────────────────────────────
NAVER_BLOG_AUTH_URL = "https://nid.naver.com/oauth2.0/authorize"
NAVER_BLOG_TOKEN_URL = "https://nid.naver.com/oauth2.0/token"
NAVER_BLOG_POST_URL = "https://openapi.naver.com/blog/writePost.json"
_NAVER_CALLBACK_PORT = 8765
_naver_auth_code: str | None = None


class _NaverCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _naver_auth_code
        qs = parse_qs(urlparse(self.path).query)
        _naver_auth_code = qs.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write("네이버 로그인 완료! 창을 닫아주세요.".encode("utf-8"))

    def log_message(self, *args):
        pass


def naver_get_access_token(client_id: str, client_secret: str, redirect_uri: str = None) -> str:
    """브라우저 OAuth 흐름으로 네이버 액세스 토큰 획득."""
    global _naver_auth_code
    _naver_auth_code = None
    redirect_uri = redirect_uri or f"http://localhost:{_NAVER_CALLBACK_PORT}/callback"

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": "blog_uploader",
    }
    auth_url = NAVER_BLOG_AUTH_URL + "?" + urlencode(params)

    server = HTTPServer(("localhost", _NAVER_CALLBACK_PORT), _NaverCallbackHandler)
    t = Thread(target=server.handle_request, daemon=True)
    t.start()
    webbrowser.open(auth_url)
    t.join(timeout=120)
    server.server_close()

    if not _naver_auth_code:
        raise RuntimeError("네이버 OAuth 코드를 받지 못했습니다.")

    resp = requests.post(NAVER_BLOG_TOKEN_URL, params={
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": _naver_auth_code,
        "state": "blog_uploader",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def post_to_naver_blog(access_token: str, title: str, content_html: str) -> str:
    """네이버 블로그에 글 포스팅 → 포스트 URL 반환."""
    headers = {"Authorization": f"Bearer {access_token}"}
    data = {"title": title, "contents": content_html}
    resp = requests.post(NAVER_BLOG_POST_URL, headers=headers, data=data, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    return result.get("result", {}).get("postUrl", "게시 완료")


# ──────────────────────────────────────────────
# 블로그스팟 / Blogger API v3 (Google)
# ──────────────────────────────────────────────
BLOGGER_TOKEN_URL = "https://oauth2.googleapis.com/token"
BLOGGER_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
BLOGGER_POST_URL = "https://www.googleapis.com/blogger/v3/blogs/{blog_id}/posts/"
BLOGGER_SCOPE = "https://www.googleapis.com/auth/blogger"
_BLOGGER_CALLBACK_PORT = 8766
_blogger_auth_code: str | None = None


class _BloggerCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global _blogger_auth_code
        qs = parse_qs(urlparse(self.path).query)
        _blogger_auth_code = qs.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write("Google 로그인 완료! 창을 닫아주세요.".encode("utf-8"))

    def log_message(self, *args):
        pass


def blogger_get_access_token(client_id: str, client_secret: str) -> str:
    global _blogger_auth_code
    _blogger_auth_code = None
    redirect_uri = f"http://localhost:{_BLOGGER_CALLBACK_PORT}/callback"

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": BLOGGER_SCOPE,
        "access_type": "offline",
    }
    auth_url = BLOGGER_AUTH_URL + "?" + urlencode(params)

    server = HTTPServer(("localhost", _BLOGGER_CALLBACK_PORT), _BloggerCallbackHandler)
    t = Thread(target=server.handle_request, daemon=True)
    t.start()
    webbrowser.open(auth_url)
    t.join(timeout=120)
    server.server_close()

    if not _blogger_auth_code:
        raise RuntimeError("Google OAuth 코드를 받지 못했습니다.")

    resp = requests.post(BLOGGER_TOKEN_URL, data={
        "code": _blogger_auth_code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def post_to_blogger(access_token: str, blog_id: str, title: str, content_html: str) -> str:
    url = BLOGGER_POST_URL.format(blog_id=blog_id)
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    body = {"kind": "blogger#post", "title": title, "content": content_html}
    resp = requests.post(url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json().get("url", "게시 완료")


# ──────────────────────────────────────────────
# 워드프레스 REST API
# ──────────────────────────────────────────────
def post_to_wordpress(site_url: str, username: str, app_password: str,
                      title: str, content_html: str) -> str:
    """WordPress Application Password 방식으로 포스팅."""
    site_url = site_url.rstrip("/")
    api_url = f"{site_url}/wp-json/wp/v2/posts"
    cred = base64.b64encode(f"{username}:{app_password}".encode()).decode()
    headers = {
        "Authorization": f"Basic {cred}",
        "Content-Type": "application/json",
    }
    body = {"title": title, "content": content_html, "status": "publish"}
    resp = requests.post(api_url, headers=headers, json=body, timeout=15)
    resp.raise_for_status()
    return resp.json().get("link", "게시 완료")
