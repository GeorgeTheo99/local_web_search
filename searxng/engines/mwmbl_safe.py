"""Defensive SearXNG adapter for Mwmbl's keyless JSON search API."""

from __future__ import annotations

from urllib.parse import urlencode

about = {
    "website": "https://github.com/mwmbl/mwmbl",
    "use_official_api": True,
    "require_api_key": False,
    "results": "JSON",
}
paging = False
categories = ["general"]
api_url = "https://api.mwmbl.org/api/v1"


def request(query, params):
    params["url"] = f"{api_url}/search/?{urlencode({'s': query})}"
    return params


def response(resp):
    results = []
    payload = resp.json()
    if not isinstance(payload, list):
        return results
    for item in payload:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        title_parts = item.get("title") or []
        title = "".join(
            str(part.get("value") or "")
            for part in title_parts
            if isinstance(part, dict)
        ).strip()
        extracts = item.get("extract") or []
        content = ""
        if extracts and isinstance(extracts[0], dict):
            content = str(extracts[0].get("value") or "")
        results.append(
            {
                "url": str(item["url"]),
                "title": title or str(item["url"]),
                "content": content,
            }
        )
    return results
