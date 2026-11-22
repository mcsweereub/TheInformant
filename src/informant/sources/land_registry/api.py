"""HM Land Registry's dataset API: what is published, and downloading it."""

from __future__ import annotations

import os
from pathlib import Path

import httpx

from ...errors import ConfigError, UpstreamError
from ..fetchutil import api_client
from .common import (
    _FILE_RE,
    API,
    UK,
    Notify,
    _date,
    _file_kind,
    api_key,
    file_month,
)

# --- the API --------------------------------------------------------------------------------


class LandRegistryApi:
    def __init__(self, key: str | None = None, http: httpx.Client | None = None):
        key = key or api_key()
        if not key:
            raise ConfigError("LAND_REGISTRY_API_KEY is not set: create a free account at "
                              "use-land-property-data.service.gov.uk, accept the licence for "
                              "the UK companies dataset and add the key to .env")
        self.http = http or api_client(timeout=60.0, headers={"Authorization": key,
                                                              "Accept": "application/json"})

    def close(self) -> None:
        self.http.close()

    def _get(self, path: str) -> dict:
        resp = self.http.get(API + path)
        if resp.status_code in (401, 403):
            raise ConfigError("HM Land Registry refused the API key (check LAND_REGISTRY_API_KEY "
                              "and that the dataset's licence is accepted on your account)")
        if resp.status_code != 200:
            raise UpstreamError(f"HM Land Registry API: HTTP {resp.status_code} for {path}")
        return resp.json()

    def latest(self, dataset: str = UK) -> dict:
        """{"full": file name, "change": file name, "month": "YYYY-MM", "published": date}."""
        result = self._get(f"/datasets/{dataset}").get("result") or {}
        out: dict = {"published": _date(result.get("last_updated"))}
        for res in result.get("resources") or []:
            name = res.get("file_name") or ""
            kind = _file_kind(name, dataset)
            if kind:
                out["full" if kind == "FULL" else "change"] = name
                out["month"] = file_month(name)
        if "full" not in out:
            raise UpstreamError("HM Land Registry API: no full file listed for the dataset")
        return out

    def history(self, dataset: str = UK) -> list[dict]:
        """Every published file: [{"file", "kind" (FULL/COU), "month", "published"}], oldest first."""
        data = self._get(f"/datasets/history/{dataset}")
        files = []
        for item in data.get("dataset_history") or []:
            name = item.get("filename") or ""
            kind = _file_kind(name, dataset)
            if kind:
                files.append({"file": name, "kind": kind, "month": file_month(name),
                              "published": _date(item.get("unsorted_date"))})
        return sorted(files, key=lambda f: (f["month"], f["kind"]))

    def download(self, file_name: str, dest: Path, notify: Notify | None = None) -> Path:
        """Fetch a file: the API gives a signed link valid for ~10 seconds, so the download
        starts straight away."""
        m = _FILE_RE.match(file_name or "")
        dataset = m.group(1).lower() if m else UK
        link = (self._get(f"/datasets/{dataset}/{file_name}").get("result") or {}).get("download_url")
        if not link:
            raise UpstreamError(f"HM Land Registry API: no download link for {file_name}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        # the signed link carries its own credentials: never send the API key to storage
        with httpx.stream("GET", link, timeout=httpx.Timeout(60.0, read=300.0),
                          follow_redirects=True) as resp:
            if resp.status_code != 200:
                raise UpstreamError(f"downloading {file_name}: HTTP {resp.status_code}")
            total = int(resp.headers.get("content-length") or 0)
            done, shown = 0, -1
            with open(tmp, "wb") as f:
                for chunk in resp.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    step = done * 10 // total if total else -1
                    if notify and total and step != shown:
                        shown = step
                        notify(f"downloading {file_name}: {done / 1_048_576:.0f} of "
                               f"{total / 1_048_576:.0f} MB")
        os.replace(tmp, dest)
        return dest


