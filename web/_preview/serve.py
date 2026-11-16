"""预览服务器：把 /assets/* 映射到 web 根，模拟 hub.py 的路由，保证预览与线上同构。"""
import http.server, os, pathlib

WEB = pathlib.Path(__file__).resolve().parent.parent


class Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        p = path.split('?', 1)[0].split('#', 1)[0]
        if p.startswith('/assets/'):
            rel = p[len('/assets/'):]
        else:
            rel = p.lstrip('/')
        target = (WEB / rel).resolve()
        if WEB not in target.parents and target != WEB:
            return str(WEB / 'index.html')
        return str(target)

    def log_message(self, *args):
        pass


if __name__ == '__main__':
    os.chdir(WEB)
    http.server.ThreadingHTTPServer(('127.0.0.1', 8781), Handler).serve_forever()
