from __future__ import annotations

import json
import urllib.error
import urllib.request


PUSHPLUS_SEND_URL = "https://www.pushplus.plus/send"


class NotificationError(RuntimeError):
    pass


def send_pushplus(token: str, title: str, content: str) -> dict:
    token = token.strip()
    if not token:
        raise NotificationError("PushPlus Token 未配置。")
    body = json.dumps(
        {
            "token": token,
            "title": title,
            "content": content,
            "template": "markdown",
            "channel": "wechat",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(PUSHPLUS_SEND_URL, data=body, method="POST")
    request.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
        raise NotificationError(f"PushPlus 请求失败：{exc}") from exc
    if int(payload.get("code") or 0) != 200:
        raise NotificationError(f"PushPlus 返回失败：{payload.get('msg') or payload}")
    return payload
