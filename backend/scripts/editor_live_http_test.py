"""Live HTTP check of /undo and /redo against the container image (port 8001)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8001"
PROJECT = "prj_20261019_cd4a57"
CLIP = 0
checks: list[tuple[bool, str]] = []


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def check(ok: bool, label: str) -> None:
    checks.append((ok, label))
    print(("  ok   " if ok else "  FAIL ") + label)


shots_path = f"/api/editor/shots/{PROJECT}?clip_index={CLIP}"
post = f"/api/editor/shots/{PROJECT}"

# fresh state: drop any stored list so history starts empty
call("DELETE", shots_path)
status, draft = call("GET", shots_path)
check(status == 200 and draft["origin"] == "draft", "GET returns the draft with empty history")
check(not draft["can_undo"] and not draft["can_redo"], "no undo/redo on a fresh draft")

status, empty = call("POST", post + "/undo", {"clip_index": CLIP})
check(status == 409, f"undo on empty history is 409 (got {status})")
status, empty = call("POST", post + "/redo", {"clip_index": CLIP})
check(status == 409, f"redo on empty history is 409 (got {status})")

def kind_of(payload: dict, index: int) -> str:
    return next(s["kind"] for s in payload["shots"] if s["index"] == index)


# one real edit through the per-edit endpoint (index is 1-based; #1 is already whole_frame)
status, patched = call("POST", post + "/patch", {"clip_index": CLIP, "index": 2, "kind": "whole_frame"})
check(status == 200, f"patch #2 to whole_frame is 200 (got {status})")
check(patched["can_undo"] and patched["undo_depth"] == 1, "patch reports undo depth 1")
check(kind_of(patched, 2) == "whole_frame", "patch changed the list")

status, undone = call("POST", post + "/undo", {"clip_index": CLIP})
check(status == 200 and undone["can_redo"], "undo is 200 and enables redo")
check(kind_of(undone, 2) == "follow_speaker", "undo restored the previous kind")

status, redone = call("POST", post + "/redo", {"clip_index": CLIP})
check(status == 200 and kind_of(redone, 2) == "whole_frame", "redo re-applies the patch")

status, listed = call("GET", shots_path)
check(listed["origin"] == "stored" and listed["undo_depth"] == 1, "history survives a separate GET (persisted)")

failed = [label for ok, label in checks if not ok]
print(f"\n{len(checks) - len(failed)} checks passed, {len(failed)} failed")
raise SystemExit(1 if failed else 0)
