"""Thin HTTP client. Retries transport errors and 5xx; returns API refusals to the caller as data."""
from __future__ import annotations

import time

import httpx


class Transient(Exception):
    pass


class SimClient:
    def __init__(self, url: str, timeout: float = 300, retries: int = 8):
        self.http = httpx.Client(base_url=url, timeout=timeout)
        self.retries = retries

    def _req(self, method: str, path: str, **kw) -> httpx.Response:
        delay = 0.25
        for attempt in range(self.retries):
            try:
                r = self.http.request(method, path, **kw)
                if r.status_code < 500:
                    return r
                err = f"{r.status_code}: {r.text[:200]}"
            except httpx.TransportError as e:
                err = repr(e)
            if attempt == self.retries - 1:
                raise Transient(f"{method} {path} failed after {self.retries} attempts: {err}")
            time.sleep(delay)
            delay = min(delay * 2, 10)
        raise AssertionError("unreachable")

    def _json(self, method: str, path: str, **kw):
        r = self._req(method, path, **kw)
        r.raise_for_status()
        return r.json()

    # ---- read
    def state(self) -> dict:
        return self._json("GET", "/sim/state")

    def workflows(self) -> list[dict]:
        return self._json("GET", "/workflows")

    def config(self, wid: str) -> dict:
        return self._json("GET", f"/workflows/{wid}/config")

    def history(self, wid: str) -> list[dict]:
        return self._json("GET", f"/workflows/{wid}/config/history")

    def chunks(self, kind: str, after: int) -> list[dict]:
        return self._json("GET", "/events/chunks", params={"kind": kind, "after": after})

    def chunk_text(self, n: int) -> str:
        r = self._req("GET", f"/events/chunks/{n}")
        r.raise_for_status()
        return r.text

    def approvals(self, wid: str) -> list[dict]:
        return self._json("GET", "/approvals", params={"workflow_id": wid})

    # ---- write: (status, body), so that governance can act on 403/409/410/422
    def _post(self, method: str, path: str, body: dict) -> tuple[int, dict]:
        r = self._req(method, path, json=body)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {"raw": r.text}

    def request_approval(self, body: dict):
        return self._post("POST", "/approvals", body)

    def apply(self, wid: str, approval_id: str):
        return self._post("POST", f"/workflows/{wid}/apply", {"approval_id": approval_id})

    def auto_apply(self, wid: str, body: dict):
        return self._post("PUT", f"/workflows/{wid}/config", body)

    def suggest_fixes(self, wid: str, problem: str):
        return self._post("POST", "/llm/suggest-fixes", {"workflow_id": wid, "problem": problem})

    def validate(self, wid: str, prompt: str, n: int):
        return self._post("POST", "/validation/runs", {"workflow_id": wid, "prompt_version": prompt, "n": n})

    def advance(self, minutes: int):
        return self._post("POST", "/sim/advance", {"minutes": minutes})
