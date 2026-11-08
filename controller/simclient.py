"""Thin, typed HTTP client for the simulator. The only module that knows URLs.

Retry policy:
  * connection errors / timeouts / 5xx on *idempotent* calls (GETs, and POSTs whose body carries an
    idempotency key: approvals, auto-apply, apply) are retried with exponential backoff;
  * credit-spending calls (LLM, validation) are never retried blindly - a retry could double-spend;
  * `advance` is special: after a timeout we read /sim/state to learn whether it happened;
  * 4xx are mapped to typed exceptions and never retried.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import httpx

log = logging.getLogger(__name__)


class SimError(Exception):
    def __init__(self, status: int, code: str, detail: Any = None):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class StaleBase(SimError): ...
class DuplicateProposal(SimError): ...
class Lapsed(SimError): ...
class NotApproved(SimError): ...
class OutOfCredits(SimError): ...
class AutoApplyForbidden(SimError): ...
class Finished(SimError): ...
class InvalidRequest(SimError): ...
class Unavailable(SimError): ...


_BY_CODE = {
    "stale_base_version": StaleBase,
    "duplicate_proposal_id": DuplicateProposal,
    "approval_lapsed": Lapsed,
    "not_approved": NotApproved,
    "out_of_credits": OutOfCredits,
    "auto_apply_not_permitted": AutoApplyForbidden,
    "finished": Finished,
}


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    try:
        body = resp.json()
    except ValueError:
        body = {"error": resp.text}
    code = body.get("error") or body.get("detail") or "error"
    if not isinstance(code, str):
        code = "error"
    detail = body.get("detail")
    cls = _BY_CODE.get(code)
    if cls is None:
        cls = Unavailable if resp.status_code >= 500 else InvalidRequest
    raise cls(resp.status_code, code, detail)


class SimClient:
    def __init__(self, base_url: str, timeout: float = 600.0, retries: int = 6):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, timeout=timeout)
        self.retries = retries

    # ------------------------------------------------------------------ plumbing
    def _send(self, method: str, path: str, *, retry: bool = True, **kw) -> httpx.Response:
        attempt = 0
        while True:
            try:
                resp = self.http.request(method, path, **kw)
                if resp.status_code >= 500 and retry and attempt < self.retries:
                    raise httpx.TransportError(f"server error {resp.status_code}")
                _raise_for(resp)
                return resp
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if not retry or attempt >= self.retries:
                    raise Unavailable(503, "unavailable", str(e)) from e
                wait = min(2 ** attempt * 0.5, 20)
                log.warning("sim %s %s failed (%s); retry in %.1fs", method, path, e, wait)
                time.sleep(wait)
                attempt += 1

    def _get(self, path: str, **params) -> Any:
        return self._send("GET", path, params={k: v for k, v in params.items() if v is not None}).json()

    # ------------------------------------------------------------------ time + events
    def state(self) -> dict:
        return self._get("/sim/state")

    def advance(self, minutes: int) -> dict:
        before = self.state()
        try:
            return self._send("POST", "/sim/advance", json={"minutes": minutes}, retry=False).json()
        except Unavailable:
            # Did it happen? The world is authoritative; never advance twice by accident.
            for _ in range(self.retries):
                try:
                    now = self.state()
                    break
                except Unavailable:
                    time.sleep(2)
            else:
                raise
            if now["sim_minute"] > before["sim_minute"]:
                new = [c for c in self.list_chunks("events", after=before["chunks"] - 1)]
                return {"sim_minute": now["sim_minute"], "ts": now["ts"], "finished": now["finished"],
                        "new_chunks": new}
            return self._send("POST", "/sim/advance", json={"minutes": minutes}, retry=False).json()

    def list_chunks(self, kind: str, after: int = -1) -> list[dict]:
        return self._get("/events/chunks", kind=kind, after=after)

    def iter_chunk_lines(self, n: int) -> Iterator[bytes]:
        attempt = 0
        while True:
            try:
                with self.http.stream("GET", f"/events/chunks/{n}") as resp:
                    _raise_for(resp)
                    yield from resp.iter_lines()
                return
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if attempt >= self.retries:
                    raise Unavailable(503, "unavailable", str(e)) from e
                time.sleep(min(2 ** attempt * 0.5, 20))
                attempt += 1

    # ------------------------------------------------------------------ workflows
    def workflows(self) -> list[dict]:
        return self._get("/workflows")

    def config(self, wid: str) -> dict:
        return self._get(f"/workflows/{wid}/config")

    def config_history(self, wid: str) -> list[dict]:
        return self._get(f"/workflows/{wid}/config/history")

    # ------------------------------------------------------------------ governance
    def auto_apply(self, wid: str, body: dict) -> dict:
        return self._send("PUT", f"/workflows/{wid}/config", json=body).json()

    def request_approval(self, body: dict) -> dict:
        return self._send("POST", "/approvals", json=body).json()

    def approval(self, aid: str) -> dict:
        return self._get(f"/approvals/{aid}")

    def approvals(self, workflow_id: str | None = None, status: str | None = None) -> list[dict]:
        return self._get("/approvals", workflow_id=workflow_id, status=status)

    def apply(self, wid: str, approval_id: str) -> dict:
        return self._send("POST", f"/workflows/{wid}/apply", json={"approval_id": approval_id}).json()

    # ------------------------------------------------------------------ credits
    def suggest_fixes(self, wid: str, problem: str) -> dict:
        return self._send("POST", "/llm/suggest-fixes", json={"workflow_id": wid, "problem": problem},
                          retry=False).json()

    def validate(self, wid: str, prompt_version: str, n: int) -> dict:
        return self._send("POST", "/validation/runs",
                          json={"workflow_id": wid, "prompt_version": prompt_version, "n": n},
                          retry=False).json()
