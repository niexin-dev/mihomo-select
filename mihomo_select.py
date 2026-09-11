#!/usr/bin/env python3
"""Mihomo terminal UI: tree navigation, latency sorting, switching and env sync."""

import argparse
import curses
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import getpass
import json
import locale
import os
from pathlib import Path
import queue
import re
import shlex
import shutil
import sys
import tempfile
import unicodedata
from urllib import error, parse, request

GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance", "Smart"}
DEFAULT_API_URL = "http://127.0.0.1:9090"
DEFAULT_PROXY_URL = "http://127.0.0.1:7890"
DEFAULT_SOCKS_PROXY_URL = "socks5h://127.0.0.1:7890"
DEFAULT_TEST_URL = "https://www.gstatic.com/generate_204"
API_TIMEOUT_SECONDS = 10
DOWNLOAD_TIMEOUT_SECONDS = 30
ENV_FILE = Path.home() / ".config/mihomo/env.sh"


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def is_group(item):
    return isinstance(item, dict) and (
        item.get("type") in GROUP_TYPES or isinstance(item.get("all"), list)
    )


def sort_group(name):
    lower = name.casefold()
    return (0 if "节点选择" in name or "proxy" in lower else
            2 if lower == "global" or "全局" in name else 1, name)


def write_env(enabled, proxy_url=DEFAULT_PROXY_URL, socks_proxy_url=DEFAULT_SOCKS_PROXY_URL):
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        body = ("# Managed by mihomo_select.py; source this file in the current shell.\n"
                f"export http_proxy={shlex.quote(proxy_url)}\n"
                f"export https_proxy={shlex.quote(proxy_url)}\n"
                f"export all_proxy={shlex.quote(socks_proxy_url)}\n"
                "export no_proxy='localhost,127.0.0.1,::1'\n")
    else:
        body = ("# Managed by mihomo_select.py; proxy variables disabled.\n"
                "unset http_proxy https_proxy all_proxy no_proxy\n"
                "unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY\n")
    atomic_write_text(ENV_FILE, body, 0o600)


def proxy_enabled():
    if not ENV_FILE.exists():
        return False
    try:
        return "unset http_proxy" not in ENV_FILE.read_text()
    except OSError:
        return False


def print_exit_hint():
    if not ENV_FILE.exists():
        return
    print("代理环境变量：" + ("已开启" if proxy_enabled() else "已关闭"))
    print("在当前 shell 执行以生效：source ~/.config/mihomo/env.sh")


def shell_env(enabled, proxy_url, socks_proxy_url=DEFAULT_SOCKS_PROXY_URL):
    if enabled:
        return (f"export http_proxy={shlex.quote(proxy_url)}; export https_proxy={shlex.quote(proxy_url)}; "
                f"export all_proxy={shlex.quote(socks_proxy_url)}; "
                "export no_proxy='localhost,127.0.0.1,::1'")
    return "unset http_proxy https_proxy all_proxy no_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY"


def atomic_write_text(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def char_width(char):
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def clip(text, width):
    limit = max(1, width - 1)
    out = []
    used = 0
    for char in text:
        size = char_width(char)
        if used + size > limit:
            break
        out.append(char)
        used += size
    return "".join(out)


def flatten(roots, group_names, proxies, delays, expanded, pages, limit):
    rows = []

    def visit(name, depth, path, ancestors, last):
        item = proxies[name]
        children = [x for x in item.get("all", []) if isinstance(x, str)]
        if children and all(x in delays for x in children):
            children.sort(key=lambda x: (delays[x], x))
        key = tuple(path)
        total = max(1, -(-len(children) // limit))
        page = min(pages.get(key, 0), total - 1)
        rows.append({"kind": "group", "name": name, "parent": path[-2] if len(path) > 1 else None,
                     "path": key, "depth": depth, "last": last, "expanded": key in expanded,
                     "current": item.get("now"), "children": children,
                     "page": page, "pages": total})
        if key not in expanded or name in ancestors:
            return
        visible = children[page * limit:(page + 1) * limit] if total > 1 else children
        for index, child in enumerate(visible):
            is_last = index == len(visible) - 1
            if child in group_names:
                visit(child, depth + 1, path + [child], ancestors | {name}, is_last)
            else:
                rows.append({"kind": "leaf", "name": child, "parent": name,
                             "path": key + (child,), "depth": depth + 1, "last": is_last,
                             "current": child == item.get("now"), "delay": delays.get(child)})

    for index, root in enumerate(roots):
        visit(root, 0, [root], set(), index == len(roots) - 1)
    return rows


def row_text(row, width):
    prefix = "  " * row["depth"]
    branch = "" if row["depth"] == 0 else ("└─ " if row["last"] else "├─ ")
    if row["kind"] == "group":
        arrow = "▼" if row["expanded"] else "▶"
        page = f"  [{row['page'] + 1}/{row['pages']}]" if row["expanded"] and row["pages"] > 1 else ""
        current = f"  [当前：{row['current']}]" if row["current"] else ""
        return clip(f"{prefix}{branch}{arrow} {row['name']}{page}{current}", width)
    delay = f"{row['delay']} ms" if isinstance(row["delay"], int) else "未测"
    current = "  [当前]" if row["current"] else ""
    return clip(f"{prefix}{branch}{row['name']}  {delay:>7}{current}", width)


PROFILES_DIR = Path("/etc/mihomo/profiles")
SUBS_FILE = PROFILES_DIR / "subscriptions.json"
CONFIG_LINK = PROFILES_DIR / "active.yaml"


def load_subs():
    if SUBS_FILE.exists():
        try:
            data = json.loads(SUBS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("subscriptions"), list):
                subscriptions = []
                for item in data["subscriptions"]:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name")
                    url = item.get("url")
                    if isinstance(name, str) and name.strip() and isinstance(url, str) and url.strip():
                        subscriptions.append({"name": name, "url": url})
                current = data.get("current")
                if not isinstance(current, str) or current not in {item["name"] for item in subscriptions}:
                    current = None
                return {"subscriptions": subscriptions, "current": current}
        except (OSError, UnicodeError, ValueError):
            pass
    return {"subscriptions": [], "current": None}


def save_subs(data):
    atomic_write_text(SUBS_FILE, json.dumps(data, ensure_ascii=False, indent=2), 0o600)


def profile_file(name):
    safe = "".join(c if c.isalnum() or c in "-_. " else "_" for c in name).strip() or "subscription"
    if safe != name:
        safe += "-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return PROFILES_DIR / (safe + ".yaml")


def write_profile(path, content):
    atomic_write_text(path, content, 0o640)


def update_config_link(path):
    if CONFIG_LINK is None:
        return
    tmp = CONFIG_LINK.with_name(CONFIG_LINK.name + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(path, tmp)
    os.replace(tmp, CONFIG_LINK)


def derive_name(url):
    return parse.urlsplit(url).hostname or "subscription"


def download_subscription(url, proxy_url=DEFAULT_PROXY_URL, timeout=DOWNLOAD_TIMEOUT_SECONDS):
    endpoint = parse.urlsplit(url)
    if endpoint.scheme != "https" or not endpoint.hostname:
        raise ValueError("订阅链接必须是 HTTPS 地址")
    def fetch(proxy):
        handlers = [request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
        opener = request.build_opener(*handlers)
        req = request.Request(url, headers={"User-Agent": "clash-verge/v1.0.0"})
        with opener.open(req, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    try:
        return fetch(None)
    except (OSError, error.URLError):
        return fetch(proxy_url)


def prompt_input(stdscr, label, height, width, default=""):
    buf = list(default)
    cancelled = False
    stdscr.timeout(-1)
    curses.curs_set(1)
    while True:
        stdscr.move(height - 2, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(height - 2, 0, clip(label + "".join(buf), width), width - 1)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (10, 13, curses.KEY_ENTER):
            break
        if key == 27:
            cancelled = True
            break
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if buf:
                buf.pop()
        elif 32 <= key and not (curses.KEY_MIN <= key <= curses.KEY_MAX):
            buf.append(chr(key))
    curses.curs_set(0)
    return None if cancelled else "".join(buf)


def subscription_ui(stdscr, api, proxy_url, request_timeout):
    data = load_subs()
    if CONFIG_LINK is not None and CONFIG_LINK.is_symlink():
        target = CONFIG_LINK.resolve()
        match = next((s["name"] for s in data["subscriptions"] if profile_file(s["name"]).resolve() == target), None)
        if match:
            data["current"] = match
    selected = 0
    status = "就绪"
    changed = False
    stdscr.timeout(-1)
    while True:
        items = data["subscriptions"]
        selected = min(selected, max(0, len(items) - 1))
        height, width = stdscr.getmaxyx()
        if height < 3 or width < 2:
            stdscr.erase()
            if height > 0 and width > 1:
                stdscr.addnstr(0, 0, "终端窗口过小，请放大后继续", width - 1)
            stdscr.refresh()
            stdscr.timeout(500)
            stdscr.getch()
            continue
        stdscr.erase()
        stdscr.addnstr(0, 0, clip("订阅管理", width), width - 1, curses.A_BOLD)
        stdscr.addnstr(1, 0, clip("状态：" + status, width), width - 1, curses.A_DIM)
        if items:
            for index, item in enumerate(items):
                screen = 3 + index
                if screen >= height - 2:
                    break
                mark = "  [当前]" if item["name"] == data.get("current") else ""
                attr = curses.A_REVERSE if index == selected else curses.A_NORMAL
                stdscr.addnstr(screen, 0, clip(item["name"] + mark, width), width - 1, attr)
            stdscr.addnstr(height - 2, 0, clip("链接：" + items[selected].get("url", ""), width), width - 1, curses.A_DIM)
        else:
            stdscr.addnstr(3, 0, clip("（暂无订阅，按 a 添加）", width), width - 1, curses.A_DIM)
        stdscr.addnstr(height - 1, 0, clip("a 添加  Enter 切换  u 更新  d 删除  Esc 返回", width), width - 1, curses.A_DIM)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (27, ord("q"), ord("Q")):
            break
        if not items and key != ord("a"):
            continue
        if key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(items) - 1, selected + 1)
        elif key == ord("a"):
            url = prompt_input(stdscr, "订阅链接：", height, width)
            if not url:
                status = "已取消"
                continue
            name = prompt_input(stdscr, "订阅名称：", height, width, default=derive_name(url))
            if not name:
                status = "已取消"
                continue
            try:
                path = profile_file(name)
                write_profile(path, download_subscription(url, proxy_url, request_timeout))
                data["subscriptions"] = [s for s in items if s["name"] != name] + [{"name": name, "url": url}]
                save_subs(data)
                selected = len(data["subscriptions"]) - 1
                status = f"已添加：{name}"
            except (OSError, ValueError, error.URLError) as exc:
                status = f"添加失败：{exc}"
        elif key in (10, 13, curses.KEY_ENTER):
            item = items[selected]
            try:
                path = profile_file(item["name"])
                if not path.exists():
                    write_profile(path, download_subscription(item["url"], proxy_url, request_timeout))
                api("/configs", {"payload": path.read_text()})
                data["current"] = item["name"]
                save_subs(data)
                changed = True
                try:
                    update_config_link(path)
                    status = f"已切换到：{item['name']}"
                except OSError as exc:
                    status = f"已切换，但更新配置文件链接失败：{exc}"
            except (OSError, ValueError, error.HTTPError) as exc:
                status = f"切换失败：{exc}"
        elif key == ord("u"):
            item = items[selected]
            try:
                path = profile_file(item["name"])
                write_profile(path, download_subscription(item["url"], proxy_url, request_timeout))
                status = f"已更新：{item['name']}"
            except (OSError, ValueError, error.URLError) as exc:
                status = f"更新失败：{exc}"
        elif key == ord("d"):
            item = items[selected]
            try:
                path = profile_file(item["name"])
                if path.exists():
                    path.unlink()
                if CONFIG_LINK is not None and CONFIG_LINK.is_symlink():
                    try:
                        if CONFIG_LINK.resolve() == path.resolve():
                            CONFIG_LINK.unlink()
                    except OSError:
                        pass
            except OSError:
                pass
            data["subscriptions"] = [s for s in items if s["name"] != item["name"]]
            if data.get("current") == item["name"]:
                data["current"] = None
            save_subs(data)
            status = f"已删除：{item['name']}"
    return changed


def tree_ui(roots, group_names, proxies, fetch_proxies, api, proxy_url, socks_proxy_url, request_timeout,
            test_node, test_group, test_all):
    expanded = {(roots[0],)} if roots else set()
    pages = {}
    delays = {}
    selected = 0
    status = "就绪"
    executor = ThreadPoolExecutor(max_workers=8)
    results = queue.Queue()
    testing = set()

    def start_test(label, func):
        testing.add(label)
        future = executor.submit(func)
        future.add_done_callback(lambda f: results.put((label, f)))

    def render(stdscr):
        nonlocal selected, status, roots, group_names
        curses.curs_set(0)
        stdscr.idlok(False)
        selected_path = None
        hard_redraw = False
        confirm_quit = False

        def expand_group(row):
            nonlocal selected_path
            for other in rows:
                if (other["kind"] == "group" and other["parent"] == row["parent"]
                        and other["path"] != row["path"]):
                    expanded.discard(other["path"])
            expanded.add(row["path"])
            current = row["current"]
            if current in row["children"]:
                pages[row["path"]] = row["children"].index(current) // limit
                selected_path = row["path"] + (current,)
                return True
            return False

        while True:
            updated = False
            while True:
                try:
                    label, future = results.get_nowait()
                except queue.Empty:
                    break
                testing.discard(label)
                try:
                    result = future.result()
                    if isinstance(result, tuple):
                        values, failures = result
                    else:
                        values, failures = result, 0
                    delays.update(values)
                    status = (f"测试完成：{label}"
                              if not failures else f"测试完成：{label}，{failures} 个节点失败")
                except (OSError, ValueError, error.HTTPError) as exc:
                    status = f"测试失败：{exc}"
                updated = True
            if updated and not testing:
                hard_redraw = True
            height, width = stdscr.getmaxyx()
            if height < 4 or width < 2:
                stdscr.erase()
                if height > 0 and width > 1:
                    stdscr.addnstr(0, 0, "终端窗口过小，请放大后继续", width - 1)
                stdscr.refresh()
                stdscr.timeout(500)
                stdscr.getch()
                continue
            body = max(1, height - 4)
            limit = max(1, body - len(roots) - 1)
            rows = flatten(roots, group_names, proxies, delays, expanded, pages, limit)
            if selected_path is None and rows:
                first = rows[0]
                current = first.get("current")
                if first["kind"] == "group" and first["expanded"] and current in first["children"]:
                    expand_group(first)
            if selected_path is not None:
                found = next((i for i, x in enumerate(rows) if x["path"] == selected_path), None)
                if found is None and len(selected_path) > 1:
                    parent = next((x for x in rows if x["kind"] == "group" and x["path"] == selected_path[:-1]), None)
                    if parent and selected_path[-1] in parent["children"]:
                        pages[selected_path[:-1]] = parent["children"].index(selected_path[-1]) // limit
                        rows = flatten(roots, group_names, proxies, delays, expanded, pages, limit)
                        found = next((i for i, x in enumerate(rows) if x["path"] == selected_path), None)
                if found is not None:
                    selected = found
            selected = min(selected, max(0, len(rows) - 1))
            top = max(0, min(selected - body + 1, selected))
            stdscr.erase()
            stdscr.addnstr(0, 0, clip("Mihomo 策略树", width), width - 1, curses.A_BOLD)
            stdscr.addnstr(1, 0, clip("状态：" + status, width), width - 1, curses.A_DIM)
            for screen, row in enumerate(rows[top:top + body], 2):
                attr = curses.A_REVERSE if top + screen - 2 == selected else curses.A_NORMAL
                stdscr.addnstr(screen, 0, row_text(row, width), width - 1, attr)
            focus = rows[selected]
            path = " → ".join(focus["path"])
            stdscr.addnstr(height - 2, 0, clip("路径：" + path, width), width - 1, curses.A_DIM)
            stdscr.addnstr(height - 1, 0, clip("j/k 移动  h/l 折叠展开  n/p 组内翻页  Ctrl-f/Ctrl-b 翻屏  g/G 首尾  Enter 切换/展开  r 测试当前  R 测试全部  s 订阅  e 环境  q 退出", width), width - 1, curses.A_DIM)
            if hard_redraw:
                stdscr.redrawwin()
                hard_redraw = False
            stdscr.refresh()
            stdscr.timeout(100 if testing else -1)
            key = stdscr.getch()
            if key == -1:
                selected_path = rows[selected]["path"] if rows else None
                continue
            jumped = False
            if confirm_quit and key not in (ord("q"), ord("Q")):
                confirm_quit = False
                status = "已取消退出"
            if key in (ord("q"), ord("Q")):
                if confirm_quit:
                    raise EOFError
                confirm_quit = True
                status = "再按一次 q 确认退出（按其他键取消）"
            elif key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(rows) - 1, selected + 1)
            elif key in (curses.KEY_NPAGE, 6):
                selected = min(len(rows) - 1, selected + body)
            elif key in (curses.KEY_PPAGE, 2):
                selected = max(0, selected - body)
            elif key == ord("g"):
                selected = 0
            elif key == ord("G"):
                selected = len(rows) - 1
            elif key in (ord("h"), curses.KEY_LEFT):
                row = rows[selected]
                if row["kind"] == "group" and row["expanded"]:
                    expanded.discard(row["path"])
                    hard_redraw = True
                elif row["kind"] == "leaf":
                    parent_path = row["path"][:-1]
                    expanded.discard(parent_path)
                    selected = next((i for i, x in enumerate(rows) if x["path"] == parent_path), selected)
                    hard_redraw = True
                elif row["kind"] == "group" and row["parent"]:
                    parent_path = row["path"][:-1]
                    selected = next((i for i, x in enumerate(rows) if x["path"] == parent_path), selected)
            elif key in (ord("l"), curses.KEY_RIGHT):
                row = rows[selected]
                if row["kind"] == "group" and row["children"]:
                    jumped = expand_group(row)
                    hard_redraw = True
            elif key in (ord("n"), ord("p")):
                row = rows[selected]
                group_path = row["path"] if row["kind"] == "group" else row["path"][:-1]
                group = next((x for x in rows if x["kind"] == "group" and x["path"] == group_path), None)
                if group and group["pages"] > 1:
                    page = pages.get(group_path, 0) + (1 if key == ord("n") else -1)
                    pages[group_path] = max(0, min(group["pages"] - 1, page))
                    selected = next((i for i, x in enumerate(rows)
                                     if x["kind"] == "group" and x["path"] == group_path), selected)
                    hard_redraw = True
            elif key == ord("r"):
                row = rows[selected]
                label = row["name"]
                if label not in testing:
                    func = (lambda name=label: test_group(name)) if row["kind"] == "group" else (lambda name=label: test_node(name))
                    start_test(label, func)
                status = f"正在测试：{label}"
            elif key == ord("R"):
                if "全部节点" not in testing:
                    start_test("全部节点", test_all)
                status = "正在测试全部节点"
            elif key == ord("s"):
                if testing:
                    status = "测速进行中，完成后才能管理订阅"
                elif subscription_ui(stdscr, api, proxy_url, request_timeout):
                    try:
                        new_roots, new_names, new_proxies = fetch_proxies()
                        roots = new_roots
                        group_names = new_names
                        proxies.clear()
                        proxies.update(new_proxies)
                        expanded.clear()
                        if roots:
                            expanded.add((roots[0],))
                        pages.clear()
                        delays.clear()
                        selected = 0
                        selected_path = None
                        status = "已切换订阅并重载"
                    except (OSError, ValueError, error.HTTPError) as exc:
                        status = f"重载失败：{exc}"
                    hard_redraw = True
                else:
                    status = "就绪"
            elif key in (ord("e"), ord("E")):
                enabled = not proxy_enabled()
                write_env(enabled, proxy_url)
                status = ("环境变量已开启，执行 source ~/.config/mihomo/env.sh 使当前 shell 生效"
                          if enabled else "环境变量已关闭，执行 source ~/.config/mihomo/env.sh 使当前 shell 生效")
            elif key in (10, 13, curses.KEY_ENTER):
                row = rows[selected]
                target = None
                if row["kind"] == "group":
                    if row["path"] in expanded:
                        expanded.discard(row["path"])
                        hard_redraw = True
                    elif row["children"]:
                        jumped = expand_group(row)
                        target = row["parent"]
                        hard_redraw = True
                else:
                    target = row["parent"]
                if target:
                    try:
                        api("/proxies/" + parse.quote(target, safe=""), {"name": row["name"]})
                        proxies[target]["now"] = row["name"]
                        status = f"已切换：{target} → {row['name']}"
                    except (OSError, ValueError, error.HTTPError) as exc:
                        status = f"切换失败：{exc}"
            if not jumped and rows:
                selected_path = rows[selected]["path"]

    try:
        return curses.wrapper(render)
    finally:
        executor.shutdown(wait=False)


def setup_locale():
    for name in ("C.UTF-8", "en_US.UTF-8", "zh_CN.UTF-8", ""):
        try:
            locale.setlocale(locale.LC_ALL, name)
            return
        except locale.Error:
            continue


ROOT_SCRIPT = r'''#!/usr/bin/env bash
# 由 mihomo_select.py --init 生成；幂等，可重复运行。
set -euo pipefail

TARGET_USER=__USER__
MIHOMO_URL=__MIHOMO_URL__
MIHOMO_SHA256=__MIHOMO_SHA256__
SUBSCRIPTION_URL=__SUBSCRIPTION_URL__

apt-get update
apt-get install -y ca-certificates curl gzip python3 openssl

if [ ! -x /usr/local/bin/mihomo ]; then
    if [ -z "$MIHOMO_URL" ]; then
        echo "未指定 mihomo 下载地址，正在查询 GitHub 最新稳定版..."
        mapfile -t RELEASE_INFO < <(python3 - <<'PY'
import json, platform, sys
from urllib.request import Request, urlopen

arch = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armv7"}.get(platform.machine())
if not arch:
    raise SystemExit("不支持自动识别的 CPU 架构，请使用 --mihomo-url 和 --mihomo-sha256")
request = Request("https://api.github.com/repos/MetaCubeX/mihomo/releases/latest",
                 headers={"Accept": "application/vnd.github+json", "User-Agent": "mihomo-select"})
with urlopen(request, timeout=30) as response:
    release = json.load(response)
assets = {item["name"]: item["browser_download_url"] for item in release.get("assets", [])}
prefix = f"mihomo-linux-{arch}"
packages = sorted(name for name in assets if name.startswith(prefix) and name.endswith(".gz"))
if not packages:
    raise SystemExit(f"最新版本没有 {prefix} 安装包，请手动指定下载地址")
package = packages[-1]
checksum_url = assets.get(package + ".sha256") or assets.get(package + ".sha256sum")
if not checksum_url:
    raise SystemExit(f"找不到 {package} 的校验文件，请手动指定 SHA-256")
with urlopen(Request(checksum_url, headers={"User-Agent": "mihomo-select"}), timeout=30) as response:
    checksum = response.read().decode("utf-8").split()[0]
if len(checksum) != 64:
    raise SystemExit("GitHub 返回的 SHA-256 格式无效")
print(assets[package])
print(checksum)
PY
        )
        MIHOMO_URL="${RELEASE_INFO[0]:-}"
        MIHOMO_SHA256="${RELEASE_INFO[1]:-}"
        if [ -z "$MIHOMO_URL" ] || [ -z "$MIHOMO_SHA256" ]; then
            echo "无法自动获取 mihomo 下载信息，请重新生成并指定 --mihomo-url 与 --mihomo-sha256" >&2
            exit 1
        fi
    fi
    case "$MIHOMO_URL" in
        https://*) ;;
        *) echo "mihomo 下载链接必须使用 HTTPS" >&2; exit 1 ;;
    esac
    if [ -z "$MIHOMO_SHA256" ]; then
        echo "未提供 mihomo SHA-256：用 --mihomo-sha256 重新生成" >&2
        exit 1
    fi
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    curl -fL --retry 3 --proto '=https' --tlsv1.2 "$MIHOMO_URL" -o "$tmp/mihomo.gz"
    echo "$MIHOMO_SHA256  $tmp/mihomo.gz" | sha256sum -c -
    gzip -dc "$tmp/mihomo.gz" > "$tmp/mihomo"
    install -m 755 "$tmp/mihomo" /usr/local/bin/mihomo
    trap - EXIT
    rm -rf "$tmp"
fi

if ! id mihomo >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/mihomo --shell /usr/sbin/nologin mihomo
fi
install -d -o root -g mihomo -m 750 /etc/mihomo
install -d -o mihomo -g mihomo -m 700 /var/lib/mihomo
chmod o+x /etc/mihomo
install -d -o "$TARGET_USER" -g mihomo -m 2750 /etc/mihomo/profiles

if [ -e /etc/mihomo/profiles/active.yaml ]; then
    echo "已存在 /etc/mihomo/profiles/active.yaml，跳过初始配置设置"
elif [ -n "$SUBSCRIPTION_URL" ]; then
    PROFILE_NAME="$(python3 -c 'import sys, urllib.parse as u; print(u.urlsplit(sys.argv[1]).hostname or "subscription")' "$SUBSCRIPTION_URL")"
    PROFILE_PATH="/etc/mihomo/profiles/${PROFILE_NAME}.yaml"
    PROFILE_TMP="$(mktemp /etc/mihomo/profiles/.profile.XXXXXX)"
    trap 'rm -f "${PROFILE_TMP:-}" "${SUBS_TMP:-}"' EXIT
    curl -fL --retry 3 --proto '=https' --tlsv1.2 "$SUBSCRIPTION_URL" -o "$PROFILE_TMP"
    mv -f "$PROFILE_TMP" "$PROFILE_PATH"
    SUBS_PATH=/etc/mihomo/profiles/subscriptions.json
    SUBS_TMP="$(mktemp /etc/mihomo/profiles/.subscriptions.XXXXXX)"
    python3 - "$SUBSCRIPTION_URL" "$PROFILE_NAME" "$SUBS_TMP" <<'PY'
import json, sys
url, name, path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "w", encoding="utf-8") as fh:
    json.dump({"subscriptions": [{"name": name, "url": url}], "current": name}, fh, ensure_ascii=False, indent=2)
PY
    mv -f "$SUBS_TMP" "$SUBS_PATH"
    chown "$TARGET_USER":mihomo "$SUBS_PATH"
    chmod 600 "$SUBS_PATH"
    chown "$TARGET_USER":mihomo "$PROFILE_PATH"
    chmod 640 "$PROFILE_PATH"
    ln -s "$PROFILE_PATH" /etc/mihomo/profiles/active.yaml
    chown -h "$TARGET_USER":mihomo /etc/mihomo/profiles/active.yaml
else
    PROFILE_NAME="default"
    install -m 640 /dev/null "/etc/mihomo/profiles/${PROFILE_NAME}.yaml"
    chown "$TARGET_USER":mihomo "/etc/mihomo/profiles/${PROFILE_NAME}.yaml"
    chmod 640 "/etc/mihomo/profiles/${PROFILE_NAME}.yaml"
    ln -s "/etc/mihomo/profiles/${PROFILE_NAME}.yaml" /etc/mihomo/profiles/active.yaml
    chown -h "$TARGET_USER":mihomo /etc/mihomo/profiles/active.yaml
    echo "警告：未提供 --subscription-url；/etc/mihomo/profiles/default.yaml 为空，请先添加并切换订阅再启动服务" >&2
fi

# Make the local REST API available for mihomo-select without exposing it publicly.
CONFIG_PATH=/etc/mihomo/profiles/active.yaml
TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
if [ -z "$TARGET_HOME" ] || [ ! -d "$TARGET_HOME" ]; then
    echo "无法确定目标用户的 home 目录：$TARGET_USER" >&2
    exit 1
fi
API_SECRET_FILE="$TARGET_HOME/.config/mihomo/api-secret"
install -d -o "$TARGET_USER" -g "$TARGET_USER" -m 700 "$TARGET_HOME/.config/mihomo"
if ! grep -Eq '^[[:space:]]*external-controller[[:space:]]*:' "$CONFIG_PATH"; then
    printf '\n# Added by mihomo-select bootstrap.\nexternal-controller: 127.0.0.1:9090\n' >> "$CONFIG_PATH"
fi
if ! grep -Eq '^[[:space:]]*secret[[:space:]]*:' "$CONFIG_PATH"; then
    API_SECRET="$(openssl rand -hex 24)"
    printf 'secret: %s\n' "$API_SECRET" >> "$CONFIG_PATH"
    printf '%s\n' "$API_SECRET" > "$API_SECRET_FILE"
    chown "$TARGET_USER:$TARGET_USER" "$API_SECRET_FILE"
    chmod 600 "$API_SECRET_FILE"
else
    echo "配置已有 secret；请自行提供该密钥给 mihomo_select.py" >&2
fi
chown "$TARGET_USER:mihomo" "$CONFIG_PATH"
chmod 640 "$CONFIG_PATH"

cat > /etc/systemd/system/mihomo.service <<'UNIT'
[Unit]
Description=Mihomo local proxy
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=mihomo
Group=mihomo
WorkingDirectory=/var/lib/mihomo
UMask=0077
ExecStart=/usr/local/bin/mihomo -d /var/lib/mihomo -f /etc/mihomo/profiles/active.yaml
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
if /usr/local/bin/mihomo -t -d /var/lib/mihomo -f /etc/mihomo/profiles/active.yaml; then
    systemctl enable --now mihomo
    systemctl --no-pager status mihomo || true
else
    echo "配置校验失败，未启动服务；修正 /etc/mihomo/profiles/active.yaml 后执行 systemctl restart mihomo" >&2
fi
'''


def root_script(user, mihomo_url, subscription_url, mihomo_sha256=""):
    return (ROOT_SCRIPT
            .replace("__USER__", shlex.quote(user))
            .replace("__MIHOMO_URL__", shlex.quote(mihomo_url or ""))
            .replace("__MIHOMO_SHA256__", shlex.quote(mihomo_sha256 or ""))
            .replace("__SUBSCRIPTION_URL__", shlex.quote(subscription_url or "")))


def run_init(mihomo_url, subscription_url, mihomo_sha256):
    home = Path.home()
    (home / ".config/mihomo").mkdir(parents=True, exist_ok=True)
    bin_dir = home / ".local/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "mihomo_select.py"
    source = Path(__file__).resolve()
    if source != target.resolve():
        shutil.copy2(source, target)
    target.chmod(0o755)
    script_path = home / ".config/mihomo/bootstrap.sh"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    body = root_script(getpass.getuser(), mihomo_url, subscription_url, mihomo_sha256)
    atomic_write_text(script_path, body, 0o700)
    script_path.chmod(0o700)
    print("[用户级]")
    print("  已创建：" + str(home / ".config/mihomo"))
    print("  已安装：" + str(target))
    print("  已生成 root 脚本：" + str(script_path))
    print()
    print("[下一步]")
    print("  1) 确保 ~/.local/bin 在 PATH：export PATH=\"$HOME/.local/bin:$PATH\"")
    print("  2) 审阅 root 脚本后，以 root 执行：")
    print("     sudo bash " + str(script_path))
    print("  3) 运行：mihomo_select.py")
    print()
    print("  注意：脚本可能包含订阅 URL，仅当前用户可读；执行前请审阅文件内容。")


def main():
    global PROFILES_DIR, SUBS_FILE, CONFIG_LINK
    setup_locale()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API_URL)
    parser.add_argument("--proxy-url", default=DEFAULT_PROXY_URL,
                        help="订阅下载和环境变量使用的 HTTP 代理地址")
    parser.add_argument("--socks-proxy-url", default=DEFAULT_SOCKS_PROXY_URL,
                        help="环境变量 all_proxy 使用的 SOCKS5 地址")
    parser.add_argument("--secret-file", type=Path)
    parser.add_argument("--test-url", default=DEFAULT_TEST_URL)
    parser.add_argument("--timeout", type=int, default=5000)
    parser.add_argument("--request-timeout", type=float, default=API_TIMEOUT_SECONDS,
                        help="API 和订阅下载请求超时（秒）")
    parser.add_argument("--env", choices=("on", "off", "status"))
    parser.add_argument("--shell", action="store_true", help="只输出可 eval 的环境变量命令")
    parser.add_argument("--profiles-dir", type=Path, default=PROFILES_DIR, help="订阅文件存放目录")
    parser.add_argument("--config-link", type=Path, default=CONFIG_LINK, help="指向当前订阅的配置文件路径")
    parser.add_argument("--no-link", action="store_true", help="不更新配置文件链接")
    parser.add_argument("--init", action="store_true", help="初始化：安装脚本并生成 root provisioning 脚本")
    parser.add_argument("--mihomo-url", default="", help="--init 用：mihomo 二进制下载链接")
    parser.add_argument("--mihomo-sha256", default="", help="--init 用：mihomo 压缩包 SHA-256")
    parser.add_argument("--subscription-url", default="", help="--init 用：初始订阅链接")
    args = parser.parse_args()
    if args.shell and not args.env:
        parser.error("--shell 必须与 --env 一起使用")
    PROFILES_DIR = args.profiles_dir
    SUBS_FILE = PROFILES_DIR / "subscriptions.json"
    CONFIG_LINK = None if args.no_link else args.config_link
    proxy_url = args.proxy_url
    socks_proxy_url = args.socks_proxy_url
    if args.init:
        if args.mihomo_url and not args.mihomo_url.startswith("https://"):
            parser.error("--mihomo-url 必须使用 HTTPS")
        if args.mihomo_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", args.mihomo_sha256):
            parser.error("--mihomo-sha256 必须是 64 位十六进制字符串")
        if args.mihomo_url and not args.mihomo_sha256:
            parser.error("提供 --mihomo-url 时必须同时提供 --mihomo-sha256")
        if args.mihomo_sha256 and not args.mihomo_url:
            parser.error("--mihomo-sha256 必须与 --mihomo-url 一起使用")
        run_init(args.mihomo_url, args.subscription_url, args.mihomo_sha256)
        return 0
    if args.env:
        if args.env == "status":
            enabled = proxy_enabled()
        else:
            enabled = args.env == "on"
            write_env(enabled, proxy_url, socks_proxy_url)
        if args.shell:
            print(shell_env(enabled, proxy_url, socks_proxy_url))
        else:
            print("代理环境变量：" + ("开启" if enabled else "关闭"))
            print("配置文件：" + str(ENV_FILE))
            print("当前 shell 执行：source ~/.config/mihomo/env.sh")
        return 0
    if not 100 <= args.timeout <= 60000:
        parser.error("--timeout 必须在 100 到 60000 毫秒之间")
    if args.request_timeout <= 0:
        parser.error("--request-timeout 必须大于 0 秒")
    endpoint = parse.urlsplit(args.api)
    if endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        parser.error("--api 必须是没有凭据和查询参数的 HTTP(S) 地址")
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def needs_secret():
        try:
            with opener.open(args.api.rstrip("/") + "/version", timeout=args.request_timeout) as response:
                response.read()
            return False
        except error.HTTPError as exc:
            if exc.code == 401:
                return True
            raise

    if args.secret_file:
        secret = args.secret_file.read_text().strip()
    elif needs_secret():
        if sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        secret = getpass.getpass("Mihomo API secret：")
    else:
        secret = ""
    if any(ord(char) < 33 or ord(char) > 126 for char in secret):
        raise ValueError("secret 必须为无空白的 ASCII 字符串")

    def api(path, payload=None):
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["Authorization"] = "Bearer " + secret
        body = None if payload is None else json.dumps(payload).encode()
        req = request.Request(args.api.rstrip("/") + path, data=body, headers=headers,
                              method="GET" if payload is None else "PUT")
        with opener.open(req, timeout=args.request_timeout) as response:
            if payload is not None:
                if not 200 <= response.status < 300:
                    raise ValueError("接口未返回成功状态")
                return None
            result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError("API 返回不是 JSON 对象")
            return result

    def fetch_proxies():
        proxies = api("/proxies").get("proxies")
        if not isinstance(proxies, dict):
            raise ValueError("API 响应缺少 proxies")
        groups = sorted([name for name, item in proxies.items() if is_group(item)], key=sort_group)
        if not groups:
            raise ValueError("未找到策略组")
        return groups, set(groups), proxies

    groups, group_names, proxies = fetch_proxies()

    def delay_query():
        return parse.urlencode({"url": args.test_url, "timeout": args.timeout, "expected": "204"})

    def test_node(name):
        result = api("/proxies/" + parse.quote(name, safe="") + "/delay?" + delay_query())
        value = result.get("delay")
        return ({name: value}, 0) if isinstance(value, int) else ({}, 1)

    def test_group(name):
        result = api("/group/" + parse.quote(name, safe="") + "/delay?" + delay_query())
        values = result.get("delay") if isinstance(result.get("delay"), dict) else result
        if not isinstance(values, dict):
            return {}, 1
        result = {name: value for name, value in values.items()
                  if isinstance(name, str) and isinstance(value, int)}
        failures = sum(1 for name, value in values.items()
                       if not isinstance(name, str) or not isinstance(value, int))
        return result, failures

    def test_all():
        nodes = [name for name, item in proxies.items() if not is_group(item)]
        result = {}
        failures = 0
        with ThreadPoolExecutor(max_workers=min(8, len(nodes) or 1)) as pool:
            futures = [pool.submit(test_node, name) for name in nodes]
            for future in as_completed(futures):
                try:
                    values, node_failures = future.result()
                    result.update(values)
                    failures += node_failures
                except (OSError, ValueError, error.HTTPError):
                    failures += 1
        return result, failures

    return tree_ui(groups, group_names, proxies, fetch_proxies, api, proxy_url,
                   socks_proxy_url, args.request_timeout,
                   test_node, test_group, test_all) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\n已退出。")
        print_exit_hint()
        sys.stdout.flush()
        os._exit(0)
    except error.HTTPError as exc:
        print(f"API 请求失败：HTTP {exc.code}", file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, error.URLError) as exc:
        print(f"操作失败：{exc}", file=sys.stderr)
        sys.exit(1)
